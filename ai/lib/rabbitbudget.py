"""The daily CodeRabbit budget: decides whether an agent PR goes to CodeRabbit or to the human reviewer, posts the
review trigger as Agent Odysseus and counts it. Only these agent triggers are counted; reviews people request on
GitHub are neither counted nor limited."""

import json
import os
from datetime import datetime, timedelta, timezone

from . import common
from .common import AiError, config, ody, say
from . import coderabbit

LEDGER = "rabbitbudget.json"
WINDOW = timedelta(hours=24)
KEEP = timedelta(hours=48)
DEFAULT_TRIGGER = "@coderabbitai full review"
SOURCES = ["pr-opened", "bot-round", "reenter", "update-branch", "conflict", "backlog"]


# ---- policy (pure) ----

def tier(used, settings):
    """The tier `used` falls in: `every` (every PR), `large` (large PRs and re-reviews), `full` (human only);
    `open` when there is no budget."""
    if not settings:
        return "open"
    if used < settings["everyPrUntil"]:
        return "every"
    if used < settings["limit"]:
        return "large"
    return "full"


def decide(used, files, rereview, settings):
    """coderabbit or human, from the sends in the last 24 hours, the hand-written file count, the re-review flag
    and the `codeRabbitBudget` settings (None = no limit)."""
    t = tier(used, settings)
    if t in ("open", "every"):
        return "coderabbit"
    if t == "large" and (rereview or files >= settings["largePrFiles"]):
        return "coderabbit"
    return "human"


def settings():
    """`codeRabbitBudget` from config.json, or None (no limit) when absent."""
    budget = config().get("codeRabbitBudget")
    return dict(budget) if budget else None


# ---- ledger ----

def _utc_now():
    return datetime.now(timezone.utc)


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _path():
    return common.STATE_DIR / LEDGER


def _read():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"sent": list(data.get("sent", [])), "deferred": list(data.get("deferred", []))}


def _write(ledger):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _prune(ledger, now):
    cutoff = now - KEEP
    ledger["sent"] = [s for s in ledger["sent"] if _parse(s["at"]) >= cutoff]


def _window(ledger, now):
    cutoff = now - WINDOW
    return [s for s in ledger["sent"] if _parse(s["at"]) > cutoff]


def used_now(ledger=None, now=None):
    now = now or _utc_now()
    return len(_window(ledger or _read(), now))


def _next_slot(ledger, now):
    """When the oldest send in the window leaves it, or None when nothing is counted."""
    recent = _window(ledger, now)
    if not recent:
        return None
    return min(_parse(s["at"]) for s in recent) + WINDOW


# ---- commands ----

def request(repo, pr, source, files=0, rereview=False, issue=None):
    """Apply the policy for the PR. On coderabbit post the trigger and count it; on human note it as deferred
    (with `issue`). Returns {decision, used, limit, reason}."""
    if source not in SOURCES:
        raise AiError(f"unknown --source {source} (one of {', '.join(SOURCES)})")
    if not coderabbit.enabled(repo):
        return {"decision": "absent", "used": 0, "limit": None, "reason": f"CodeRabbit not enabled for {repo}"}
    conf = settings()
    limit = conf["limit"] if conf else None
    with common.state_locked("rabbitbudget"):
        now = _utc_now()
        ledger = _read()
        _prune(ledger, now)
        used = used_now(ledger, now)
        choice = decide(used, files, rereview, conf)
        reason = f"{tier(used, conf)} tier: {used} of {limit if limit is not None else 'unlimited'} used in 24h, " \
                 f"{files} files{', re-review' if rereview else ''}"
        if choice == "coderabbit":
            try:
                sha = coderabbit.head_sha(repo, pr)
                ody(repo, "gh", "pr", "comment", str(pr), "--body", config().get("codeRabbitTrigger", DEFAULT_TRIGGER))
            except Exception as e:  # a failed comment means the human reviews; nothing is counted
                choice, reason = "human", f"trigger comment failed: {e}"
            else:
                ledger["sent"].append({"at": _stamp(now), "repo": repo, "pr": pr, "sha": sha, "source": source})
                ledger["deferred"] = [d for d in ledger["deferred"] if not (d["repo"] == repo and d["pr"] == pr)]
                used += 1
        if choice == "human" and issue is not None and not any(
                d["repo"] == repo and d["pr"] == pr for d in ledger["deferred"]):
            ledger["deferred"].append({"at": _stamp(now), "repo": repo, "pr": pr, "issue": issue, "files": files})
        _write(ledger)
    return {"decision": choice, "used": used, "limit": limit, "reason": reason}


def known_decision(repo, pr):
    """`coderabbit` when the ledger counted a trigger for the PR, `human` when it is deferred, else None."""
    ledger = _read()
    if any(s["repo"] == repo and s["pr"] == pr for s in ledger["sent"]):
        return "coderabbit"
    if any(d["repo"] == repo and d["pr"] == pr for d in ledger["deferred"]):
        return "human"
    return None


def budget():
    """{used, limit, tier, nextSlot, deferred} for the rolling last 24 hours."""
    conf = settings()
    now = _utc_now()
    ledger = _read()
    used = used_now(ledger, now)
    slot = _next_slot(ledger, now)
    return {"used": used, "limit": conf["limit"] if conf else None, "tier": tier(used, conf),
            "nextSlot": _stamp(slot) if slot else None, "deferred": ledger["deferred"]}


def cmd_request(args):
    result = request(args.repo, args.pr, args.source, args.files, args.rereview, args.issue)
    if args.json:
        say(json.dumps(result))
    else:
        say(f"{result['decision']} - {result['reason']}")
    return 0


def cmd_budget(args):
    info = budget()
    if args.json:
        say(json.dumps(info))
        return 0
    say(f"used {info['used']} of {info['limit'] if info['limit'] is not None else 'unlimited'} in the last 24h "
        f"(tier: {info['tier']}); next slot frees: {info['nextSlot'] or 'n/a'}")
    for d in info["deferred"]:
        say(f"  deferred: {d['repo']}#{d['pr']} (issue {d['issue']}, {d['files']} files, since {d['at']})")
    return 0
