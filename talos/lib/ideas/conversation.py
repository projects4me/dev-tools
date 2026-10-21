"""One turn of an idea's conversation: take in the engineer's new messages, run the agent (its progress published
live), and post the reply, and any plan version it proposes, back to the idea."""

import time
import uuid
from datetime import date, datetime

from .. import runs as run_log
from ..config import log
from . import plan as plans
from . import policy, state

FIX_PLAN = ("The nestor-plan block in your last reply could not be used: {error}. Reply again with the same answer "
            "and a corrected nestor-plan block.")
HISTORY = 20                 # earlier messages replayed when there is no session to resume
FLUSH_SECONDS = 0.3          # text is published in small batches, not per token


class Conversation:
    def __init__(self, agent, client, progress, launcher, kb, project_codes, map_path):
        self.agent, self.client, self.progress, self.launcher, self.kb = agent, client, progress, launcher, kb
        self.project_codes, self.map_path = project_codes, map_path

    def system_prompt(self):
        projects = "\n".join(f"- {name} (short code {code})" for name, code in self.project_codes().items())
        return (f"{self.agent.role_prompt}\n\n## This session\n- Today is {date.today().isoformat()}. You work for "
                f"{self.agent.reviewer_name}; they approve your plans, and the issues are created assigned to them.\n"
                f"- Projects you can plan issues in:\n{projects}\n"
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
        state.update(self.agent.name, idea_id, answered_upto=max(m["id"] for m in new),
                     **({"note": ""} if answered and st.get("note") else {}))
        return True

    def _prompt(self, idea, messages, new, st):
        said = "\n\n".join(m["body"] for m in new)
        note = f"[Note from Talos] {st['note']}\n\n" if st.get("note") else ""
        if st.get("session_id"):
            return f"{note}{said}"
        earlier = [m for m in messages if m["id"] < new[0]["id"] and m["role"] in ("user", "agent")][-HISTORY:]
        history = ""
        if earlier:
            lines = [f"{'Engineer' if m['role'] == 'user' else 'You'}: {m['body'][:1500]}" for m in earlier]
            history = "Earlier in this conversation (your previous session is gone):\n\n" + "\n\n".join(lines) + "\n\n"
        return f"{note}Idea #{idea['id']}: {idea['title']}\n\n{history}The engineer says:\n\n{said}"

    # -- one run --------------------------------------------------------------------------------------------------

    def _converse(self, idea, prompt, session_id, session_cost, on_run, attempt=1):
        """Run the agent once (plus one correction round for an invalid plan). True if it answered; either way a
        reply or an error has been posted."""
        run = self.launcher.start(prompt, self.agent.model_default, self.system_prompt(), session_id=session_id,
                                  new_session_id=None if session_id else str(uuid.uuid4()),
                                  label=f"{self.agent.name}-idea{idea['id']}")
        on_run(run)
        steps, result = self._stream(idea, run)
        session, cost = self._account(idea, result, session_id, session_cost)
        if result["is_error"]:
            self.client.post(idea["id"], "error", self._error_text(result), steps=steps, cost=cost)
            log(f"idea #{idea['id']}: {result.get('subtype')}", self.agent.name)
            return False
        reply, raw = plans.extract(result["text"])
        plan_id, plan_error = None, None
        if raw is not None:
            try:
                plan_id, _ = self.client.add_plan(idea["id"], plans.parse(raw, self.project_codes()))
            except ValueError as e:
                plan_error = str(e)
        if plan_error and attempt == 1:
            self.progress.publish(idea, {"phase": "step", "text": "The plan block had problems; correcting it"})
            return self._converse(idea, FIX_PLAN.format(error=plan_error), session,
                                  result.get("cost") or session_cost, on_run, attempt=2)
        if plan_error:
            reply += f"\n\n> The plan in this reply could not be used: {plan_error}"
        self.client.post(idea["id"], "agent", reply or "(no reply)", steps=steps, plan_id=plan_id, cost=cost)
        log(f"idea #{idea['id']}: replied (${cost:.2f})" + (" with a plan" if plan_id else ""), self.agent.name)
        return True

    def _stream(self, idea, run):
        """Consume the run: publish text (batched), turns and steps as progress. Returns (steps, result)."""
        steps, result, buffer, flushed = [], None, [], time.monotonic()

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
            elif event["type"] == "result":
                result = event
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
