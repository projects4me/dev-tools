"""The incident packet: everything Agamemnon is given about one incident, as JSON-ready data. The sources (Gaia, git,
Docker, `ai mergecheck`, talos.db) are passed in, so the assembly here is pure."""

AGENT_NAME = "Agent Agamemnon"
LOG_TAIL_LINES = 80


def tail(text, lines=LOG_TAIL_LINES):
    return "\n".join(str(text or "").splitlines()[-lines:])


def others_in_flight(in_flight, issue):
    """The other in-flight issues with their changed files: [{"issue": n, "files": [...]}], without `issue` itself."""
    return [{"issue": int(n), "files": sorted(files or [])} for n, files in sorted(in_flight.items())
            if int(n) != int(issue)]


def own_comments(comments, author=AGENT_NAME):
    """Agamemnon's earlier comments on the issue, oldest first, as {"at", "text"}."""
    mine = [{"at": c.get("at"), "text": c.get("text")} for c in comments or [] if c.get("author") == author]
    return sorted(mine, key=lambda c: str(c["at"] or ""))


def build(issue, pr, handback, run, mergecheck, git_state, docker_state, in_flight, comments, incident=None):
    """The packet. handback is {"reason", "detail"}; run is {"summary", "log"} (the log is cut to its tail);
    mergecheck is `ai mergecheck`'s output (or None without a PR); git_state and docker_state are the workspace's git
    status and Docker's state as the caller read them; in_flight is {issue: changed files}; comments are the issue's
    comments ({"author", "at", "text"})."""
    handback, run = handback or {}, run or {}
    return {
        "issue": int(issue),
        "pr": pr,
        "incident": incident,
        "handback": {"reason": handback.get("reason"), "detail": handback.get("detail")},
        "run": {"summary": run.get("summary"), "logTail": tail(run.get("log"))},
        "mergecheck": mergecheck,
        "git": git_state,
        "docker": docker_state,
        "inFlight": others_in_flight(in_flight or {}, issue),
        "earlierComments": own_comments(comments),
    }
