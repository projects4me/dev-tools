"""One turn of an idea's conversation: take in the engineer's new messages, run the agent (its progress published
live), and post the reply, and any plan version it proposes, back to the idea."""

import time
import uuid
from datetime import date, datetime

from .. import runner
from .. import runs as run_log
from ..config import STATE_DIR, log
from . import plan as plans
from . import policy, screens, state
from .images import Images

FIX_PLAN = ("The nestor-plan block in your last reply could not be used: {error}. Reply with only the corrected "
            "nestor-plan block (your answer and screens were kept).")
HISTORY = 20                 # earlier messages replayed when there is no session to resume
FLUSH_SECONDS = 0.3          # text is published in small batches, not per token


class Conversation:
    def __init__(self, agent, client, progress, launcher, kb, project_codes, map_path, milestones=None,
                 renderer=None, project_ids=None, images=None):
        self.agent, self.client, self.progress, self.launcher, self.kb = agent, client, progress, launcher, kb
        self.project_codes, self.map_path = project_codes, map_path
        self.milestones = milestones          # project name -> its milestone names (None: not checked)
        self.project_ids = project_ids        # () -> project name -> id (None: home project not named)
        self.renderer = renderer              # render.Renderer: screens -> PNG
        self.images = images or Images(client, STATE_DIR / "ideas", agent.name)   # image references -> files

    def home_line(self, idea):
        """Which project the idea belongs to, or "" when unknown (an idea without a project, e.g. in the sim)."""
        pid = (idea or {}).get("projectId")
        if not pid or not self.project_ids:
            return ""
        codes = self.project_codes()
        name = next((n for n, i in self.project_ids().items() if str(i) == str(pid)), None)
        return f"- This idea belongs to {name} (short code {codes[name]}).\n" if name in codes else ""

    def system_prompt(self, idea=None):
        projects = "\n".join(f"- {name} (short code {code})" for name, code in self.project_codes().items())
        return (f"{self.agent.role_prompt}\n\n## This session\n- Today is {date.today().isoformat()}. You work for "
                f"{self.agent.reviewer_name}; they approve your plans, and the issues are created assigned to them.\n"
                f"{self.home_line(idea)}- Projects you can plan issues in:\n{projects}\n"
                f"- Your knowledge map: {self.map_path.as_posix()}")

    def has_unanswered(self, idea_id):
        data = self.client.idea(idea_id)
        return bool(policy.unanswered(data["messages"], state.get(self.agent.name, idea_id).get("answered_upto")))

    def turn(self, idea_id, on_run):
        """Answer what the engineer wrote since the last turn. `on_run(run)` hands out the running process so Stop
        can reach it. The messages count as answered only once a reply or an error is posted, so a turn cut off
        (Talos stopped, Gaia unreachable) is taken up again by the next reconcile. False: nothing to answer."""
        data = self.client.idea(idea_id)
        idea, messages = data["idea"], data["messages"]
        st = state.get(self.agent.name, idea_id)
        new = policy.unanswered(messages, st.get("answered_upto"))
        if not new:
            return False
        prompt = self._prompt(idea, messages, new, st)
        self.kb.ensure_fresh(lambda text: self.progress.publish(idea, {"phase": "step", "text": text}))
        answered = self._converse(idea, prompt, st.get("session_id"), st.get("session_cost_usd") or 0, on_run)
        state.update(self.agent.name, idea_id, answered_upto=new[-1]["id"],
                     **({"note": ""} if answered and st.get("note") else {}))
        return True

    def _prompt(self, idea, messages, new, st):
        said = "\n\n".join(self.images.resolve(idea["id"], m["body"]) for m in new)
        note = f"[Note from Talos] {st['note']}\n\n" if st.get("note") else ""
        if st.get("session_id"):
            return f"{note}{said}"
        before = messages[:next(i for i, m in enumerate(messages) if m["id"] == new[0]["id"])]
        earlier = [m for m in before if m["role"] in ("user", "agent")][-HISTORY:]
        history = ""
        if earlier:
            lines = [f"{'Engineer' if m['role'] == 'user' else 'You'}: {self.images.resolve(idea['id'], m['body'])[:1500]}" for m in earlier]
            history = "Earlier in this conversation (your previous session is gone):\n\n" + "\n\n".join(lines) + "\n\n"
        return f"{note}Idea #{idea['id']}: {idea['title']}\n\n{history}The engineer says:\n\n{said}"

    # -- one run --------------------------------------------------------------------------------------------------

    def _converse(self, idea, prompt, session_id, session_cost, on_run):
        """Run the agent, render the screens it designed, store the plan it proposed, post the reply. An invalid
        plan gets one correction round (asking only for the plan block). True if it answered; either way a reply or
        an error has been posted."""
        result, steps, session, cost = self._run(idea, prompt, session_id, session_cost, on_run)
        if result["is_error"]:
            self.client.post(idea["id"], "error", self._error_text(result), steps=steps, cost=cost)
            log(f"idea #{idea['id']}: {result.get('subtype')}", self.agent.name)
            return False
        text, raw = plans.extract(reply_text(result))
        text, designed, problems = screens.extract(text)
        screen_refs, rendered, failed = self._render(idea, designed, problems)
        plan_id = None
        if raw is not None:
            plan_id, error = self._add_plan(idea, raw, failed, problems, rendered)
            if error:
                self.progress.publish(idea, {"phase": "step", "text": "The plan block had problems; correcting it"})
                fix, fix_steps, session, fix_cost = self._run(idea, FIX_PLAN.format(error=error), session,
                                                              result.get("cost") or session_cost, on_run)
                steps, cost = steps + fix_steps, cost + fix_cost
                fixed = None if fix["is_error"] else plans.extract(reply_text(fix))[1]
                if fixed is not None:
                    plan_id, error = self._add_plan(idea, fixed, failed, problems, rendered)
                if error:
                    problems.append(f"The plan in this reply could not be used: {error}")
        body = (text or "(no reply)") + "".join(f"\n\n> {p}" for p in problems)
        self.client.post(idea["id"], "agent", body, steps=steps, plan_id=plan_id, cost=cost, screens=screen_refs)
        log(f"idea #{idea['id']}: replied (${cost:.2f})" + (" with a plan" if plan_id else "")
            + (f", {len(screen_refs)} screen(s)" if screen_refs else ""), self.agent.name)
        return True

    def _run(self, idea, prompt, session_id, session_cost, on_run):
        """One `claude -p` run, streamed and recorded in talos.db's runs (what `talos status` and `talos runs`
        show). Returns (result, steps, session, this run's cost)."""
        model, live_id = self.agent.model_default, session_id or str(uuid.uuid4())
        started = datetime.now().astimezone()
        run_id = run_log.start(self.agent.name, str(idea["id"]), None, model, live_id, bool(session_id),
                               run_log.IDEA, started.isoformat(timespec="seconds"), summary=idea.get("title"))
        result, session, cost, run = None, session_id, None, None
        try:
            run = self.launcher.start(prompt, model, self.system_prompt(idea), session_id=session_id,
                                      new_session_id=None if session_id else live_id,
                                      label=f"{self.agent.name}-idea{idea['id']}")
            on_run(run)
            steps, result = self._stream(idea, run)
            session, cost = self._account(idea, result, session_id, session_cost)
            return result, steps, session, cost
        finally:
            ended = datetime.now().astimezone()
            outcome = ("failed" if result is None else result.get("subtype") if result["is_error"] else "replied")
            log_path = getattr(run, "log_path", None)
            run_log.finish(run_id, ended.isoformat(timespec="seconds"), round((ended - started).total_seconds(), 1),
                           outcome, idea.get("title"), cost, (result or {}).get("cost"),
                           runner.usage_since(runner.transcript(session or live_id), started),
                           log_path.name if log_path else None)

    def _render(self, idea, designed, problems):
        """Render each screen to PNG and store it on the idea; failures become notes in the reply.
        Returns (the stored screens as the reply refers to them, keys rendered, keys of the screens that failed)."""
        refs, rendered, failed = [], set(), set()
        for screen in designed:
            self.progress.publish(idea, {"phase": "step", "text": f"Rendering the screen \u201c{screen['title']}\u201d"})
            try:
                png = self.renderer.render(screen)
            except Exception as e:
                problems.append(f"The screen \u201c{screen['title']}\u201d could not be rendered: {e}")
                failed.add(screen["key"])
                continue
            refs.append(self.client.add_screen(idea["id"], screen, png))
            rendered.add(screen["key"])
        return refs, rendered, failed

    def _add_plan(self, idea, raw, failed=frozenset(), problems=None, rendered=frozenset()):
        """(plan id, None) or (None, the problems). A plan may list the idea's screens and those rendered this turn
        (not on the idea before the reply is posted, in Gaia). Screens that failed to render this turn are accepted
        and then left out of the plan (with a note), so a failed render doesn't cost a correction round."""
        keys = {s["key"] for s in self.client.idea(idea["id"]).get("screens") or []} | set(rendered)
        try:
            body = plans.parse(raw, self.project_codes(), self._milestones_or_none, keys | set(failed))
        except ValueError as e:
            return None, str(e)
        dropped = sorted({k for i in body["issues"] for k in i["screens"] if k in failed and k not in keys})
        for issue in body["issues"]:
            issue["screens"] = [k for k in issue["screens"] if k not in dropped]
        if dropped and problems is not None:
            problems.append(f"The plan's issues won't get {', '.join(dropped)} attached (not rendered).")
        return self.client.add_plan(idea["id"], body)[0], None

    def _milestones_or_none(self, project):
        """The project's milestones, or None (not checked) when they can't be read right now."""
        if self.milestones is None:
            return None
        try:
            return self.milestones(project)
        except Exception as e:
            log(f"milestones of {project} not readable ({e}); not checked", self.agent.name)
            return None

    def _stream(self, idea, run):
        """Consume the run: publish text (batched), turns and steps as progress. Returns (steps, result)."""
        steps, result, buffer, flushed, said = [], None, [], time.monotonic(), {}

        def flush():
            if buffer:
                self.progress.publish(idea, {"phase": "text", "text": "".join(buffer)})
                buffer.clear()

        for event in run:
            if event["type"] == "text":
                buffer.append(event["text"])
                if time.monotonic() - flushed >= FLUSH_SECONDS:
                    flush()
                    flushed = time.monotonic()
                continue
            flush()
            if event["type"] == "turn":
                self.progress.publish(idea, {"phase": "turn"})
            elif event["type"] == "step":
                steps.append(event["text"])
                self.progress.publish(idea, {"phase": "step", "text": event["text"]})
            elif event["type"] == "said":
                said[event["message"]] = (said.get(event["message"], "") + "\n\n" + event["text"]).strip()
            elif event["type"] == "result":
                result = dict(event, said=list(said.values()))
        flush()
        return steps, result

    def _account(self, idea, result, session_id, session_cost):
        """Keep the session to resume and its cumulative cost; returns (session, this run's own cost)."""
        session = result.get("session_id") or session_id
        resumed = session_id is not None and session == session_id
        cost = run_log.incremental_cost(result.get("cost"), session_cost, resumed) or 0
        if result.get("cost") is not None and session:
            state.update(self.agent.name, idea["id"], session_id=session, session_cost_usd=result["cost"],
                         last_ended_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        return session, cost

    @staticmethod
    def _error_text(result):
        if result.get("subtype") == "stopped":
            return "Stopped."
        if result.get("subtype") == "error_max_turns":
            return "The agent ran out of turns before answering. Ask it to continue, or narrow the question."
        return f"The agent's run failed ({result.get('subtype')}): {result.get('error') or result.get('text')}"


def reply_text(result):
    """The reply: the run's final message. When the plan or screens aren't in it but in an earlier message of the
    run (the agent wrote its answer, then used a tool, then summed up), that message comes first: only blocks
    the reply holds are rendered and stored."""
    final = result.get("text") or ""
    if plans.BLOCK.search(final) or screens.BLOCK.search(final):
        return final
    earlier = [t for t in result.get("said") or [] if t.strip() != final.strip()]
    answer = next((t for t in reversed(earlier) if plans.BLOCK.search(t) or screens.BLOCK.search(t)), None)
    return f"{answer}\n\n{final}" if answer else final
