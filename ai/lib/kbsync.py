"""Pulls tracker issues and their comments into the knowledge base, incrementally.

Gaia doesn't touch an issue's dateModified when a comment is added (comments hang off the issue's conversation
room), so each sync takes the issues modified since the last sync plus the issues whose room got new or edited
comments, and re-reads all comments of those issues. Cursors are the newest dateModified seen (the server's
clock, not ours), compared with >= so a boundary second is read twice rather than missed.

Everything is read first and written in one transaction, so a failed sync (even `--full`) leaves the index as it
was. Deleted or no-longer-visible issues, and deleted comments, stay in the index until `sync --full` rebuilds it
(a deletion changes no dateModified we can see).
"""

import re
import time

from .common import strip_html

ISSUE_RELS = "issuestatus,issuetype,assignedTo,issuemilestone,project"
COMMENT_FIELDS = "Comment.id,Comment.relatedId,Comment.comment,Comment.createdUserName,Comment.dateCreated,Comment.dateModified"
PAGE = 100          # issues per request; paged (no hasMany rels, which would make Gaia's limit count joined rows)
ROOM_BATCH = 40     # conversation rooms per comment request

COMPLEXITY = re.compile(r"Complexity:\s*(simple|complex)\b", re.IGNORECASE)


def issue_record(row):
    """A Gaia issue (flattened JSON:API, with ISSUE_RELS) as a kb row."""
    text = strip_html(row.get("description"))
    found = COMPLEXITY.findall(text)
    return {
        "number": int(row["issueNumber"]),
        "id": row.get("id"),
        "project": (row.get("project") or {}).get("name"),
        "project_code": row.get("projectShortcode"),
        "subject": row.get("subject"),
        "status": (row.get("issuestatus") or {}).get("name") or row.get("status"),
        "type": (row.get("issuetype") or {}).get("name"),
        "priority": row.get("priority"),
        "milestone": (row.get("issuemilestone") or {}).get("name"),
        "parent_id": row.get("parentId"),
        "assignee": (row.get("assignedTo") or {}).get("name"),
        "complexity": found[-1].lower() if found else None,
        "start_date": row.get("startDate"),
        "end_date": row.get("endDate"),
        "date_created": row.get("dateCreated"),
        "date_modified": row.get("dateModified"),
        "room_id": row.get("conversationRoomId"),
        "description": COMPLEXITY.sub("", text).strip(),
    }


def comment_record(row):
    return {"id": row["id"], "author": row.get("createdUserName"), "date": row.get("dateCreated"),
            "body": strip_html(row.get("comment"))}


def quoted(ids):
    """CONTAINS with several values needs each one in single quotes (Gaia's Clause parser)."""
    return ",".join(f"'{i}'" for i in ids)


def newest(*values):
    present = [v for v in values if v]
    return max(present) if present else None


class Sync:
    """One sync run: `gaia` is the gaia-mcp client (anything with its `find`), `store` a kbstore.Store."""

    def __init__(self, gaia, store):
        self.g = gaia
        self.store = store

    def run(self, full=False):
        started = time.monotonic()
        issue_cursor = None if full else self.store.meta("issue_cursor")
        # No comment seen yet: every comment of every indexed issue was read when it was indexed, so comments
        # older than the newest issue are already in.
        comment_cursor = None if full else (self.store.meta("comment_cursor") or issue_cursor)

        issues = {r["issueNumber"]: r for r in self._issues(
            f"(Issue.dateModified >: {issue_cursor})" if issue_cursor else None)}
        commented_rooms = set()
        if comment_cursor:
            changed = self.g.find("comment", f"((Comment.relatedTo : conversationrooms) AND "
                                             f"(Comment.dateModified >: {comment_cursor}))",
                                  fields="Comment.relatedId,Comment.dateModified", limit=-1)
            comment_cursor = newest(comment_cursor, *(c.get("dateModified") for c in changed))
            known = {r.get("conversationRoomId") for r in issues.values()}
            commented_rooms = {c["relatedId"] for c in changed if c.get("relatedId")} - known
        via_comments = 0
        for batch in _chunks(sorted(commented_rooms), ROOM_BATCH):
            for r in self._issues(f"(Issue.conversationRoomId CONTAINS {quoted(batch)})"):
                issues[r["issueNumber"]] = r
                via_comments += 1

        comments = self._comments([r["conversationRoomId"] for r in issues.values() if r.get("conversationRoomId")])

        with self.store.transaction():
            if full:
                self.store.clear()
            for row in issues.values():
                self.store.upsert(issue_record(row),
                                  [comment_record(c) for c in comments.get(row.get("conversationRoomId"), [])])
            self.store.set_meta("issue_cursor",
                                newest(issue_cursor, *(r.get("dateModified") for r in issues.values())))
            self.store.set_meta("comment_cursor", newest(
                comment_cursor, *(c.get("dateModified") for cs in comments.values() for c in cs)))
            self.store.set_meta("last_sync", time.strftime("%Y-%m-%d %H:%M"))
        return {"issues": len(issues), "via_comments": via_comments,
                "seconds": round(time.monotonic() - started, 1)}

    def _issues(self, query):
        """All issues matching `query` (None = all visible), a page at a time in a stable order."""
        rows, page = [], 1
        while True:
            batch = self.g.find("issue", query, rels=ISSUE_RELS, sort="Issue.issueNumber", order="ASC",
                                limit=PAGE, page=page)
            rows += batch
            if len(batch) < PAGE:
                return rows
            page += 1

    def _comments(self, rooms):
        """Conversation room id -> its comments."""
        by_room = {}
        for batch in _chunks(sorted(set(rooms)), ROOM_BATCH):
            for c in self.g.find("comment", f"(Comment.relatedId CONTAINS {quoted(batch)})",
                                 fields=COMMENT_FIELDS, limit=-1):
                by_room.setdefault(c.get("relatedId"), []).append(c)
        return by_room


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]
