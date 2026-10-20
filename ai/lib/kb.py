"""`ai kb`: a local, incrementally synced knowledge base of tracker issues, so agents can find related issues
without listing or reading every issue (see kbsync for how a sync works, kbstore for the index)."""

from .common import AiError, say, tracker
from .kbstore import DB_FILE, Store
from .kbsync import Sync


CLOSED_STATUSES = ("done", "wont_fix")


def _store():
    return Store(DB_FILE)


def _totals(counts):
    """(issues, open) across the per-project status counts from Store.stats()."""
    issues = sum(n for by_status in counts.values() for n in by_status.values())
    closed = sum(n for by_status in counts.values() for s, n in by_status.items() if s in CLOSED_STATUSES)
    return issues, issues - closed


def _synced(store):
    last = store.meta("last_sync")
    return f"index synced {last}" if last else "index empty: run `ai kb sync`"


def cmd_sync(args):
    store = _store()
    try:
        try:
            result = Sync(tracker().g, store).run(full=args.full)
        except Exception as e:  # GaiaError and HTTP errors carry a readable message
            raise AiError(f"sync failed, index unchanged: {e}")
        counts, comments = store.stats()
    finally:
        store.close()
    total, _ = _totals(counts)
    extra = f", {result['via_comments']} of them for new comments" if result["via_comments"] else ""
    say(f"{'rebuilt' if args.full else 'synced'}: {result['issues']} issue(s) updated{extra} "
        f"in {result['seconds']}s; index holds {total} issues, {comments} comments")


def cmd_search(args):
    store = _store()
    try:
        hits = store.search(" ".join(args.terms), project=args.project, status=args.status, type_=args.type,
                            limit=args.limit)
        synced = _synced(store)
    finally:
        store.close()
    for h in hits:
        say(f"#{h['number']} {h['project_code'] or '?'} [{h['status']}] {h['type'] or ''}: {h['subject']}")
        snippet = " ".join((h["snippet"] or "").split())
        if snippet and snippet != h["subject"]:
            say(f"    {snippet}")
    say(f"{len(hits)} hit(s) · {synced}")


def cmd_show(args):
    store = _store()
    try:
        i = store.issue(args.issue)
        synced = _synced(store)
    finally:
        store.close()
    if not i:
        raise AiError(f"#{args.issue} is not in the index ({synced}); try `ai issue {args.issue}`")
    say(f"#{i['number']} [{i['status']}] {i['subject']}")
    facts = [i["project"], i["type"], i["priority"], i["complexity"] and f"complexity {i['complexity']}",
             i["milestone"] and f"milestone {i['milestone']}", i["assignee"] and f"assignee {i['assignee']}",
             i["parent_number"] and f"parent #{i['parent_number']}",
             i["children"] and "children " + ", ".join(f"#{n}" for n in i["children"])]
    say("  " + " · ".join(f for f in facts if f))
    say(f"  created {(i['date_created'] or '')[:10]} · modified {(i['date_modified'] or '')[:16]} · {synced}")
    say()
    say(_clip(i["description"], None if args.full else 2000) or "(no description)")
    comments = i["comments"] if args.full else i["comments"][-args.comments:] if args.comments > 0 else []
    if i["comments"]:
        say()
        say(f"Comments ({len(i['comments'])}{'' if args.full or len(comments) == len(i['comments']) else f', latest {len(comments)}'}):")
        for c in comments:
            say(f"- {(c['date'] or '')[:16]} {c['author']}: {_clip(c['body'], None if args.full else 600)}")


def cmd_stats(args):
    store = _store()
    try:
        counts, comments = store.stats()
        synced = _synced(store)
    finally:
        store.close()
    for (project, code), by_status in sorted(counts.items(), key=lambda kv: -sum(kv[1].values())):
        issues, open_ = _totals({project: by_status})
        say(f"{project} ({code}): {issues} issues, {open_} open")
    issues, open_ = _totals(counts)
    say(f"{issues} issues ({open_} open), {comments} comments · {synced} · {DB_FILE}")


def _clip(text, limit):
    text = (text or "").strip()
    return text if not limit or len(text) <= limit else text[:limit].rstrip() + " …"


def add_parser(commands):
    """Registers `ai kb <sync|search|show|stats>` on ai.py's top-level subparsers."""
    help_text = "local issue knowledge base: sync, search, show, stats (for finding related issues)"
    kb = commands.add_parser("kb", help=help_text, description=help_text)
    sub = kb.add_subparsers(dest="kb_command", required=True)

    sp = sub.add_parser("sync", help="pull issues changed since the last sync (and issues with new comments)")
    sp.add_argument("--full", action="store_true", help="drop the index and rebuild it from scratch")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("search", help="ranked full-text search over subjects, descriptions and comments")
    sp.add_argument("terms", nargs="+")
    sp.add_argument("--project", help="project name or short code")
    sp.add_argument("--status")
    sp.add_argument("--type")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("show", help="one issue from the index (no API call)")
    sp.add_argument("issue", type=int)
    sp.add_argument("--comments", type=int, default=5, help="latest comments to show (default 5)")
    sp.add_argument("--full", action="store_true", help="whole description and every comment, untruncated")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("stats", help="issue counts per project and the last sync time")
    sp.set_defaults(func=cmd_stats)
