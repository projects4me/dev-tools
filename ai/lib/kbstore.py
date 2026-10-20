"""Issue knowledge base storage: a local SQLite index of tracker issues and comments, with full-text search.

Lives in state/kb.db (git-ignored: tracker content never goes into this public repo). Issue numbers are unique
across projects, so the number is the key and the FTS rowid.
"""

import re
import sqlite3

from .common import STATE_DIR

DB_FILE = STATE_DIR / "kb.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    number INTEGER PRIMARY KEY, id TEXT, project TEXT, project_code TEXT, subject TEXT, status TEXT, type TEXT,
    priority TEXT, milestone TEXT, parent_id TEXT, assignee TEXT, complexity TEXT, start_date TEXT,
    end_date TEXT, date_created TEXT, date_modified TEXT, room_id TEXT, description TEXT);
CREATE INDEX IF NOT EXISTS issues_by_id ON issues(id);
CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, issue INTEGER, author TEXT, date TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS comments_by_issue ON comments(issue);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(subject, description, comments,
    tokenize='porter unicode61');
"""

ISSUE_COLUMNS = ("number", "id", "project", "project_code", "subject", "status", "type", "priority", "milestone",
                 "parent_id", "assignee", "complexity", "start_date", "end_date", "date_created",
                 "date_modified", "room_id", "description")

# bm25 column weights: a hit in the subject counts most, then the description, then comments.
RANK = "bm25(issues_fts, 10.0, 3.0, 1.0)"


def fts_query(text):
    """Plain search words as an FTS5 query: each word quoted (so operators and punctuation are just text), longer
    words also match as a prefix, any word may match (bm25 ranks issues matching more words higher)."""
    words = re.findall(r"\w+", text or "")
    if not words:
        return None
    return " OR ".join(f'"{w}"*' if len(w) >= 4 else f'"{w}"' for w in words)


class Store:
    def __init__(self, path=DB_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # -- meta (sync cursors, last sync time) --------------------------------------------------------------------

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # -- writes: call inside `with store.transaction():` (all of them commit together, or none) -----------------

    def transaction(self):
        return self.db

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def clear(self):
        for table in ("issues", "comments", "meta", "issues_fts"):
            self.db.execute(f"DELETE FROM {table}")

    def upsert(self, issue, comments):
        """Replace one issue, all its comments and its search row."""
        n = issue["number"]
        self.db.execute(f"INSERT OR REPLACE INTO issues ({', '.join(ISSUE_COLUMNS)}) "
                        f"VALUES ({', '.join('?' for _ in ISSUE_COLUMNS)})",
                        [issue.get(c) for c in ISSUE_COLUMNS])
        self.db.execute("DELETE FROM comments WHERE issue = ?", (n,))
        self.db.executemany("INSERT OR REPLACE INTO comments (id, issue, author, date, body) VALUES (?, ?, ?, ?, ?)",
                            [(c["id"], n, c.get("author"), c.get("date"), c.get("body")) for c in comments])
        self.db.execute("DELETE FROM issues_fts WHERE rowid = ?", (n,))
        self.db.execute("INSERT INTO issues_fts (rowid, subject, description, comments) VALUES (?, ?, ?, ?)",
                        (n, issue.get("subject") or "", issue.get("description") or "",
                         "\n".join(c.get("body") or "" for c in comments)))

    # -- reads --------------------------------------------------------------------------------------------------

    def search(self, text, project=None, status=None, type_=None, limit=15):
        query = fts_query(text)
        if not query:
            return []
        sql = (f"SELECT i.number, i.project_code, i.status, i.type, i.subject, "
               f"snippet(issues_fts, -1, '[', ']', '…', 12) AS snippet "
               f"FROM issues_fts JOIN issues i ON i.number = issues_fts.rowid WHERE issues_fts MATCH ?")
        params = [query]
        if project:
            sql += " AND (lower(i.project) = ? OR lower(i.project_code) = ?)"
            params += [project.lower(), project.lower()]
        if status:
            sql += " AND lower(i.status) = ?"
            params.append(status.lower())
        if type_:
            sql += " AND lower(i.type) = ?"
            params.append(type_.lower())
        sql += f" ORDER BY {RANK} LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self.db.execute(sql, params)]

    def issue(self, number):
        """One issue with its comments (oldest first) and its parent's number, or None."""
        row = self.db.execute("SELECT * FROM issues WHERE number = ?", (int(number),)).fetchone()
        if not row:
            return None
        out = dict(row)
        parent = self.db.execute("SELECT number FROM issues WHERE id = ?", (out["parent_id"],)).fetchone() \
            if out["parent_id"] else None
        out["parent_number"] = parent["number"] if parent else None
        out["children"] = [r["number"] for r in self.db.execute(
            "SELECT number FROM issues WHERE parent_id = ? ORDER BY number", (out["id"],))]
        out["comments"] = [dict(r) for r in self.db.execute(
            "SELECT author, date, body FROM comments WHERE issue = ? ORDER BY date, id", (int(number),))]
        return out

    def stats(self):
        """Issue counts per project and status ({(project, code): {status: n}}) and the total comment count."""
        counts = {}
        for r in self.db.execute("SELECT project, project_code, status, count(*) AS n FROM issues "
                                 "GROUP BY project, project_code, status"):
            counts.setdefault((r["project"], r["project_code"]), {})[r["status"]] = r["n"]
        comments = self.db.execute("SELECT count(*) AS n FROM comments").fetchone()["n"]
        return counts, comments
