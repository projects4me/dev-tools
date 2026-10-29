"""The CodeRabbit loop after a deferred ship (adt-4190): wait for CodeRabbit on the PR, run bot rounds while it has
findings, then hand the issue to the reviewer with `ai handoff`.

A waiting issue holds no run slot: its record lives in the `bot_reviews` table, so a restart picks it up, and the
Watcher polls `ai rabbit` for it in the background. Nothing about the loop is posted to the tracker.
"""

import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta

from . import db
from .config import DEV_TOOLS, WORKSPACE, TalosError, log

AI = DEV_TOOLS / "ai" / "ai.py"
WAITING = "waiting"     # polling `ai rabbit` for the PR's head commit
ROUND = "round"         # a bot-round run is queued or running

HAND_OFF, RUN_ROUND, WAIT = "handoff", "round", "wait"


def _now():
    return datetime.now().astimezone()


# -- store ------------------------------------------------------------------------------------

def get(agent, issue):
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM bot_reviews WHERE agent = ? AND issue = ?", (agent, issue)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _write(sql, params):
    conn = db.connect()
    try:
        with conn:
            return conn.execute(sql, params).rowcount
    finally:
        conn.close()


def wait(agent, issue, repo, pr, status, deadline):
    """Start (or go back to) waiting for CodeRabbit on the PR; the rounds already run are kept. A record is dropped
    whenever the issue leaves the loop, so a later ship of the same issue starts counting at 0 again."""
    _write("INSERT INTO bot_reviews (agent, issue, repo, pr, status, phase, rounds, deadline) "
           "VALUES (?,?,?,?,?,?,0,?) ON CONFLICT (agent, issue) DO UPDATE SET repo = excluded.repo, "
           "pr = excluded.pr, status = excluded.status, phase = excluded.phase, deadline = excluded.deadline",
           (agent, issue, repo, pr, status, WAITING, deadline.isoformat(timespec="seconds")))


def start_round(agent, issue):
    """Count a round and mark it queued; False when the record left the waiting phase (dropped meanwhile)."""
    return _write("UPDATE bot_reviews SET phase = ?, rounds = rounds + 1 WHERE agent = ? AND issue = ? AND phase = ?",
                  (ROUND, agent, issue, WAITING)) > 0


def undo_round(agent, issue, deadline, uncount=True):
    """Back to waiting from a round: uncounted when it never ran (not queued, or cut off by a restart before it
    started); counted when it broke inside Talos, so the round cap still ends a round that keeps failing."""
    _write("UPDATE bot_reviews SET phase = ?, rounds = MAX(rounds - ?, 0), deadline = ? "
           "WHERE agent = ? AND issue = ? AND phase = ?",
           (WAITING, 1 if uncount else 0, deadline.isoformat(timespec="seconds"), agent, issue, ROUND))


def drop(agent, issue):
    _write("DELETE FROM bot_reviews WHERE agent = ? AND issue = ?", (agent, issue))


def entries(agent, phase=None):
    conn = db.connect()
    try:
        sql, params = "SELECT * FROM bot_reviews WHERE agent = ?", [agent]
        if phase:
            sql, params = sql + " AND phase = ?", params + [phase]
        return [dict(r) for r in conn.execute(sql + " ORDER BY issue", params)]
    finally:
        conn.close()


# -- decisions ----------------------------------------------------------------------------------

def still_waiting(record, issue, agent_user_id):
    """False once someone reassigned the issue or moved its status while it waited: then the wait is dropped."""
    return issue["assignee"] == agent_user_id and issue["status"] == record["status"]


def next_step(state, rounds_done, cap, timed_out):
    """What to do with one `ai rabbit` state: HAND_OFF, RUN_ROUND or WAIT."""
    if state == "findings":
        return RUN_ROUND if rounds_done < cap else HAND_OFF
    if state == "pending":
        return HAND_OFF if timed_out else WAIT
    return HAND_OFF                                 # clean, paused, absent


# -- the ai commands ------------------------------------------------------------------------------

def _ai(env, *args, timeout=300):
    try:
        return subprocess.run([sys.executable, str(AI), *args], cwd=WORKSPACE, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TalosError(f"ai {args[0]} failed: {e}")


def ai_rabbit(env, repo, pr):
    """CodeRabbit's state for the PR head (one snapshot, no --wait): clean/findings/pending/paused/absent."""
    r = _ai(env, "rabbit", repo, str(pr), "--json")
    lines = (r.stdout or "").strip().splitlines()
    try:
        state = json.loads(lines[-1])["state"]
    except (IndexError, ValueError, KeyError, TypeError):
        raise TalosError(f"ai rabbit {repo} {pr}: exit {r.returncode}: {(r.stderr or r.stdout or '')[-500:]}")
    return state


def ai_handoff(env, issue):
    """`ai handoff <issue>`: in_review, assigned to the reviewer, pending record cleared."""
    r = _ai(env, "handoff", str(issue))
    if r.returncode != 0:
        raise TalosError(f"ai handoff {issue}: exit {r.returncode}: {(r.stderr or r.stdout or '')[-500:]}")


# -- the watcher ----------------------------------------------------------------------------------

class Watcher:
    """Polls the agent's waiting issues: hands off, queues a bot round, or keeps waiting."""

    def __init__(self, agent, user_id, tracker, submit, rabbit=None, handoff=None, now=_now):
        self.agent = agent
        self.user_id = user_id
        self.tracker = tracker
        self.submit = submit            # submit(number, source) -> False when it is already queued
        self.rabbit = rabbit or (lambda repo, pr: ai_rabbit(tracker.child_env(), repo, pr))
        self.handoff = handoff or (lambda issue: ai_handoff(tracker.child_env(), issue))
        self.now = now
        self._stop = threading.Event()

    @property
    def cap(self):
        """Bot rounds per issue; none without a bot-round prompt to run them with."""
        return self.agent.bot_review_rounds if self.agent.bot_round_prompt else 0

    def deadline(self):
        return self.now() + timedelta(minutes=self.agent.bot_review_wait_minutes)

    def recover(self):
        """After a restart: a round that was queued but never started waits again (a run cut off mid-way was
        already handed back by recover_interrupted, which drops its record)."""
        for record in entries(self.agent.name, ROUND):
            undo_round(self.agent.name, record["issue"], self.deadline())
            log(f"#{record['issue']} waiting for CodeRabbit again after a restart (PR {record['pr']})",
                self.agent.name)

    def start(self):
        threading.Thread(target=self._loop, name=f"bot-review-{self.agent.name}", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.agent.bot_review_poll_seconds):
            try:
                self.check()
            except Exception as e:      # one bad pass never stops the watcher
                log(f"bot review check failed: {e}", self.agent.name)

    def check(self):
        for record in entries(self.agent.name, WAITING):
            try:
                self.check_one(record)
            except TalosError as e:     # Gaia, GitHub or ai failed: try again on the next pass
                log(f"#{record['issue']} bot review check failed: {e}", self.agent.name)

    def check_one(self, record):
        number, name = record["issue"], self.agent.name
        issue = self.tracker.issue(number)
        if not still_waiting(record, issue, self.user_id):
            drop(name, number)
            log(f"#{number} no longer waiting for CodeRabbit: status '{issue['status']}', assignee changed or "
                f"moved by someone else", name)
            return "dropped"
        state = self.rabbit(record["repo"], record["pr"])
        timed_out = self.now() >= datetime.fromisoformat(record["deadline"])
        step = next_step(state, record["rounds"], self.cap, timed_out)
        if step == WAIT:
            return step
        if step == RUN_ROUND:
            if not start_round(name, number):
                return WAIT
            if not self.submit(number, f"CodeRabbit round {record['rounds'] + 1}/{self.cap} on PR {record['pr']}"):
                # still queued or running: try again on the next pass, with the deadline unchanged
                undo_round(name, number, datetime.fromisoformat(record["deadline"]))
            return step
        why = ("round cap reached" if state == "findings" else "timed out" if state == "pending" else state)
        self.handoff(number)
        drop(name, number)
        log(f"#{number} CodeRabbit {why} on PR {record['pr']} after {record['rounds']} round(s): handed to the "
            f"reviewer", name)
        return step
