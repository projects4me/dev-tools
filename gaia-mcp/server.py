#!/usr/bin/env python3
"""
gaia-mcp — MCP server that lets Claude Code work with Projects4Me issues through Gaia's REST API.

Pure Python standard library (like p4m). Speaks MCP over stdio (newline-delimited JSON-RPC 2.0).
It signs in to Gaia with OAuth2 password grant as a dedicated user and only calls /api/v1/*,
so Gaia's ACL applies to everything it does.

Settings come from the environment, or from gaia-mcp.env next to this file (see gaia-mcp.env.example).
"""

import base64
import json
import mimetypes
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
# This machine's settings and state live in the dev-tools checkout, also when the code runs from the runtime copy
# (<workspace>/.dev-tools-runtime, see `p4m runtime`), so their paths don't depend on which copy is running.
LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "dev-tools", "gaia-mcp")
SERVER_NAME = "gaia"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PRIORITIES = ("blocker", "critical", "high", "medium", "low")
# Recommended model for the agent that works the issue (simple -> Sonnet, complex -> Opus). Gaia has no
# field for it, so it lives in the description as a "Complexity: <value>" line; Talos reads it back.
COMPLEXITIES = ("simple", "complex")
COMPLEXITY_LINE = re.compile(r"<p>\s*Complexity:\s*(?:simple|complex)\s*</p>", re.IGNORECASE)


def read_complexity(description):
    """The issue's complexity marker ('simple' or 'complex'), or None. The last marker wins."""
    text = re.sub(r"<[^>]+>", " ", description or "")
    found = re.findall(r"Complexity:\s*(simple|complex)\b", text, re.IGNORECASE)
    return found[-1].lower() if found else None


def with_complexity(description, complexity):
    """The description with exactly one complexity marker, at the end."""
    value = str(complexity).strip().lower()
    if value not in COMPLEXITIES:
        raise GaiaError(f"Unknown complexity '{complexity}'. Options: {', '.join(COMPLEXITIES)}.")
    body = COMPLEXITY_LINE.sub("", description or "").rstrip()
    return f"{body}<p>Complexity: {value}</p>"


def log(msg):
    """stdout carries the protocol, so diagnostics go to stderr."""
    print(f"[gaia-mcp] {msg}", file=sys.stderr, flush=True)


def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class GaiaError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Gaia client
# ---------------------------------------------------------------------------------------------

class Gaia:
    def __init__(self):
        self.base = os.environ.get("GAIA_URL", "https://api.local-projects4.me").rstrip("/")
        self.email = os.environ.get("GAIA_EMAIL", "")
        self.password = os.environ.get("GAIA_PASSWORD", "")
        self.client_id = os.environ.get("GAIA_CLIENT_ID", "projects4me")
        self.client_secret = os.environ.get("GAIA_CLIENT_SECRET", "")
        self.timeout = float(os.environ.get("GAIA_TIMEOUT", "30"))
        ca_file = os.environ.get("GAIA_CA_FILE") or None
        # On Windows the default context also loads the system root store, where `p4m trust` puts its CA.
        self.ssl = ssl.create_default_context(cafile=ca_file)
        self.access_token = None
        self.refresh_token = None
        self.expires_at = 0
        self.user = None
        self._projects = None
        self._type_labels = None

    # -- HTTP --------------------------------------------------------------------------------

    def _http(self, method, path, params=None, body=None, form=None, auth=True):
        url = f"{self.base}/api/v1/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/vnd.api+json"
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        except urllib.error.URLError as e:
            raise GaiaError(f"Cannot reach Gaia at {self.base}: {e.reason}")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = {"raw": raw[:2000]}
        return status, payload

    def upload(self, name, content, fields):
        """Multipart POST to /upload, the same request Prometheus' file drop zone sends. Returns Gaia's JSON reply."""
        boundary = f"----gaia-mcp-{os.urandom(8).hex()}"
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                     f'Content-Type: {mime}\r\n\r\n'.encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        for attempt in (1, 2):
            req = urllib.request.Request(f"{self.base}/api/v1/upload", data=body, method="POST", headers={
                "Accept": "application/json", "Authorization": f"Bearer {self.token()}",
                "Content-Type": f"multipart/form-data; boundary={boundary}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self.access_token = None
                    continue
                raise GaiaError(f"Gaia upload returned {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
            except urllib.error.URLError as e:
                raise GaiaError(f"Cannot reach Gaia at {self.base}: {e.reason}")

    def download(self, upload_id):
        """The bytes of an upload (GET /upload/{id} streams the file itself)."""
        for attempt in (1, 2):
            req = urllib.request.Request(f"{self.base}/api/v1/upload/{urllib.parse.quote(str(upload_id))}",
                                         method="GET", headers={"Authorization": f"Bearer {self.token()}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self.access_token = None
                    continue
                raise GaiaError(f"Gaia download of upload {upload_id} returned {e.code}: "
                                f"{e.read().decode('utf-8', 'replace')[:500]}")
            except urllib.error.URLError as e:
                raise GaiaError(f"Cannot reach Gaia at {self.base}: {e.reason}")

    def request(self, method, path, params=None, body=None):
        status, payload = self._http(method, path, params=params, body=body)
        if status == 401:
            # Token expired or revoked: sign in again once and retry.
            self.access_token = None
            status, payload = self._http(method, path, params=params, body=body)
        if status >= 400:
            raise GaiaError(f"Gaia {method} {path} returned {status}: {json.dumps(payload)[:1500]}")
        return payload

    # -- Auth --------------------------------------------------------------------------------

    def token(self):
        if self.access_token and time.time() < self.expires_at - 60:
            return self.access_token
        if self.refresh_token and self._grant({"grant_type": "refresh_token",
                                               "refresh_token": self.refresh_token}, quiet=True):
            return self.access_token
        if not self.email or not self.password:
            raise GaiaError("GAIA_EMAIL and GAIA_PASSWORD are not set. Put them in dev-tools/gaia-mcp/gaia-mcp.env.")
        if not self.client_secret:
            raise GaiaError("GAIA_CLIENT_SECRET is not set. Put it in dev-tools/gaia-mcp/gaia-mcp.env.")
        self._grant({"grant_type": "password", "email": self.email, "password": self.password})
        return self.access_token

    def _grant(self, form, quiet=False):
        form = dict(form, client_id=self.client_id, client_secret=self.client_secret)
        status, payload = self._http("POST", "token", form=form, auth=False)
        if status != 200 or "access_token" not in payload:
            self.refresh_token = None
            if quiet:
                return False
            # Never echo the request; it holds the password.
            raise GaiaError(f"Gaia sign-in failed ({status}): {payload.get('error_description') or payload.get('error') or payload}")
        self.access_token = payload["access_token"]
        self.refresh_token = payload.get("refresh_token")
        self.expires_at = time.time() + int(payload.get("expires_in", 3600))
        return True

    def me(self):
        if self.user is None:
            if not re.fullmatch(r"[\w.+-]+@[\w.-]+", self.email):
                raise GaiaError(f"GAIA_EMAIL '{self.email}' does not look like an email address.")
            users =self.find("user", f"(User.email : {self.email})", fields=None, limit=1)
            if not users:
                raise GaiaError(f"Signed in, but no user record was found for {self.email}.")
            self.user = users[0]
        return self.user

    # -- JSON:API helpers ----------------------------------------------------------------------

    @staticmethod
    def flatten(payload):
        """Turn a JSON:API document into plain dicts, with relationships resolved from `included`."""
        included = {(i.get("type"), i.get("id")): i for i in payload.get("included", []) or []}

        def one(res):
            out = {"id": res.get("id")}
            out.update(res.get("attributes") or {})
            for rel, val in (res.get("relationships") or {}).items():
                data = val.get("data") if isinstance(val, dict) else None
                if data is None:
                    continue
                refs = data if isinstance(data, list) else [data]
                resolved = []
                for ref in refs:
                    inc = included.get((ref.get("type"), ref.get("id")))
                    item = {"id": ref.get("id")}
                    if inc:
                        item.update(inc.get("attributes") or {})
                    resolved.append(item)
                out[rel] = resolved if isinstance(data, list) else (resolved[0] if resolved else None)
            return out

        data = payload.get("data")
        if data is None:
            return []
        return [one(r) for r in data] if isinstance(data, list) else [one(data)]

    def find(self, module, query=None, rels=None, fields=None, sort=None, order=None, limit=None, page=None):
        params = {"query": query, "rels": rels, "fields": fields, "sort": sort, "order": order,
                  "limit": limit, "page": page}
        return self.flatten(self.request("GET", module, params=params))

    # -- Domain lookups ------------------------------------------------------------------------

    def projects(self):
        if self._projects is None:
            self._projects = self.find("project", limit=-1)
        return self._projects

    def project(self, name_or_code, rels=None):
        key = (name_or_code or "").strip().lower()
        matches = [p for p in self.projects()
                   if (p.get("name") or "").lower() == key or (p.get("shortCode") or "").lower() == key]
        if not matches:
            raise GaiaError(f"No project named or short-coded '{name_or_code}' is visible to {self.email}.")
        if len(matches) > 1:
            names = ", ".join(f"{p.get('name')} ({p.get('shortCode')})" for p in matches)
            raise GaiaError(f"'{name_or_code}' matches more than one project: {names}. Use the short code.")
        if not rels:
            return matches[0]
        found = self.find("project", f"(Project.id : {matches[0]['id']})", rels=rels, limit=-1)
        return found[0] if found else matches[0]

    def project_options(self, project):
        p = self.project(project, rels="members,milestones,issuetypes,issuestatuses")
        statuses = p.get("issuestatuses") or []
        if not statuses:
            statuses = self.find("issuestatus", "(Issuestatus.system : 1)", limit=-1)
        return p, statuses

    def type_labels(self):
        """Issue type value -> en-us label. Gaia adds `translations` only when issue types are read directly, not
        to an issuetype included with a project or issue. Empty when Gaia has no labels or won't list the types."""
        if self._type_labels is None:
            try:
                types = self.find("issuetype", limit=-1)
            except GaiaError:
                types = []
            self._type_labels = {t["name"]: t["translations"]["en-us"] for t in types
                                 if t.get("name") and isinstance(t.get("translations"), dict)
                                 and t["translations"].get("en-us")}
        return self._type_labels

    @staticmethod
    def pick(items, value, label, key="name", alias=None):
        """The item whose `key`, id or alias (e.g. a translated label) is `value`, ignoring case."""
        if value is None:
            return None
        alias = alias or (lambda item: None)
        v = str(value).strip().lower()
        for item in items:
            if v in (str(item.get(key) or "").lower(), str(item.get("id") or "").lower(),
                     str(alias(item) or "").lower()):
                return item
        options = ", ".join(sorted(str(alias(i) or i.get(key)) for i in items if i.get(key)))
        raise GaiaError(f"Unknown {label} '{value}'. Options: {options or 'none'}.")

    def member(self, project, who):
        if who is None:
            return None
        if str(who).strip().lower() in ("me", "self"):
            return self.me()
        members = project.get("members") or []
        v = str(who).strip().lower()
        for m in members:
            if v in (str(m.get("email") or "").lower(), str(m.get("username") or "").lower(),
                     str(m.get("name") or "").lower(), str(m.get("id") or "").lower()):
                return m
        raise GaiaError(f"'{who}' is not a member of {project.get('name')}.")

    def issue_by_number(self, number, rels="issuestatus,issuetype,assignedTo,issuemilestone,project"):
        found = self.find("issue", f"(Issue.issueNumber : {int(number)})", rels=rels, limit=1)
        if not found:
            raise GaiaError(f"Issue #{number} was not found or is not visible to {self.email}.")
        return found[0]


# ---------------------------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------------------------

def type_label(issuetype, labels):
    """An issue type's en-us label, falling back to its value."""
    name = (issuetype or {}).get("name")
    return labels.get(name) or name


def summarize_issue(i, base_ui=None, type_labels=None):
    out = {
        "issueNumber": i.get("issueNumber"),
        "id": i.get("id"),
        "subject": i.get("subject"),
        "status": (i.get("issuestatus") or {}).get("name") or i.get("status"),
        "type": type_label(i.get("issuetype"), type_labels or {}),
        "priority": i.get("priority"),
        "project": (i.get("project") or {}).get("name"),
        "projectShortcode": i.get("projectShortcode"),
        "assignee": (i.get("assignedTo") or {}).get("name") or (i.get("assignedTo") or {}).get("email"),
        "milestone": (i.get("issuemilestone") or {}).get("name"),
        "startDate": i.get("startDate"),
        "endDate": i.get("endDate"),
        "dateCreated": i.get("dateCreated"),
    }
    if base_ui and i.get("projectShortcode") and i.get("issueNumber"):
        out["url"] = f"{base_ui}/app/project/{i['projectShortcode']}/issue/{i['issueNumber']}"
    return {k: v for k, v in out.items() if v not in (None, "")}


def log_minutes(log):
    """Prometheus counts a day as 8 hours."""
    return int(log.get("days") or 0) * 480 + int(log.get("hours") or 0) * 60 + int(log.get("minutes") or 0)


def format_minutes(total):
    d, rest = divmod(total, 480)
    h, m = divmod(rest, 60)
    return " ".join(f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m")) if v) or "0m"


class Tools:
    def __init__(self, gaia):
        self.g = gaia
        self.ui = os.environ.get("GAIA_UI_URL", "https://app.local-projects4.me").rstrip("/") or None

    def whoami(self, args):
        me = self.g.me()
        member_of = []
        try:
            rows = self.g.find("membership", f"(Membership.userId : {me['id']})", rels="project", limit=-1)
            member_of = sorted({(r.get("project") or {}).get("name") for r in rows} - {None})
        except GaiaError as e:
            member_of = [f"(could not read memberships: {e})"]
        return {"gaia": self.g.base, "email": me.get("email"), "id": me.get("id"),
                "name": me.get("name"), "memberOf": member_of}

    def list_projects(self, args):
        search = (args.get("search") or "").lower()
        rows = [{"name": p.get("name"), "shortCode": p.get("shortCode"), "id": p.get("id")}
                for p in self.g.projects()
                if not search or search in (p.get("name") or "").lower()
                or search in (p.get("shortCode") or "").lower()]
        return {"count": len(rows), "projects": rows[:200]}

    def project_options(self, args):
        p, statuses = self.g.project_options(args["project"])
        return {
            "project": {"name": p.get("name"), "shortCode": p.get("shortCode"), "id": p.get("id")},
            "types": sorted(type_label(t, self.g.type_labels()) for t in p.get("issuetypes") or []),
            "statuses": sorted(s.get("name") for s in statuses),
            "priorities": list(PRIORITIES),
            "milestones": [{"name": m.get("name"), "id": m.get("id"), "endDate": m.get("endDate")}
                           for m in p.get("milestones") or []],
            "members": [{"name": m.get("name"), "email": m.get("email"), "id": m.get("id")}
                        for m in p.get("members") or []],
        }

    def list_issues(self, args):
        p, statuses = self.g.project_options(args["project"])
        clauses = [f"(Issue.projectId : {p['id']})"]
        if args.get("status"):
            s = self.g.pick(statuses, args["status"], "status")
            clauses.append(f"(Issue.statusId : {s['id']})")
        if args.get("assigned_to_me"):
            clauses.append(f"(Issue.assignee : {self.g.me()['id']})")
        if args.get("search"):
            # Gaia's Clause parser pastes values into quoted PHQL, so only let plain text through.
            term = re.sub(r"[^\w\s.,:#/-]", " ", str(args["search"])).strip()
            if term:
                clauses.append(f"(Issue.subject CONTAINS {term})")
        query = "(" + " AND ".join(clauses) + ")"
        limit = max(1, min(int(args.get("limit") or 25), 100))
        rows = self.g.find("issue", query, rels="issuestatus,issuetype,assignedTo,issuemilestone",
                           sort="Issue.issueNumber", order="DESC", limit=limit)
        return {"project": p.get("name"), "count": len(rows),
                "issues": [summarize_issue(r, self.ui, self.g.type_labels()) for r in rows]}

    COMMENTS_DEFAULT, COMMENTS_MAX = 10, 100
    TIMELOG_LIMIT = 1000

    def get_issue(self, args):
        # Gaia's limit counts joined rows, so hasMany rels on this limit=1 read would cut the comments and time
        # logs short. Load the issue alone, then each child list in its own query with its own limit.
        i = self.g.issue_by_number(args["issue_number"])
        out = summarize_issue(i, self.ui, self.g.type_labels())
        out["complexity"] = read_complexity(i.get("description"))
        out["description"] = i.get("description")
        out["parentId"] = i.get("parentId")
        want = max(1, min(int(args.get("comments") or self.COMMENTS_DEFAULT), self.COMMENTS_MAX))
        comments = []
        if i.get("conversationRoomId"):
            comments = self.g.find("comment", f"((Comment.relatedId : {i['conversationRoomId']}) AND "
                                              "(Comment.relatedTo : conversationrooms))",
                                   sort="Comment.dateCreated", order="DESC", limit=want + 1)
        out["olderComments"] = len(comments) > want
        out["comments"] = [{"date": c.get("dateCreated"), "by": c.get("createdUserName"), "comment": c.get("comment")}
                           for c in reversed(comments[:want])]
        timelogs = self.g.find("timelog", f"((Timelog.issueId : {i['id']}) AND (Timelog.deleted : 0))",
                               sort="Timelog.dateCreated", order="ASC", limit=self.TIMELOG_LIMIT)
        for rel, context in (("spent", "spent"), ("estimated", "est")):
            logs = [l for l in timelogs if l.get("context") == context]
            out[rel] = {"total": format_minutes(sum(log_minutes(l) for l in logs)),
                        "entries": [{"on": (l.get("spentOn") or l.get("dateCreated") or "")[:10],
                                     "by": l.get("createdUserName"), "time": format_minutes(log_minutes(l)),
                                     "description": l.get("description")} for l in logs]}
        return out

    def add_comment(self, args):
        issue = self.g.issue_by_number(args["issue_number"])
        comment = (args.get("comment") or "").strip()
        if not comment:
            raise GaiaError("comment is empty.")
        room_id = issue.get("conversationRoomId")
        if not room_id:
            # Same as Prometheus: an issue gets its conversation room on its first comment.
            # Gaia's Conversationroom model links the room back to the issue via issueId.
            self.g.request("POST", "conversationroom", body={"data": {"type": "conversationroom", "attributes": {
                "subject": f"Issue #{issue['issueNumber']}", "description": issue.get("subject"),
                "roomType": "discussion", "projectId": issue.get("projectId"),
                "projectName": (issue.get("project") or {}).get("name"), "issueId": issue["id"],
                "projectShortcode": issue.get("projectShortcode"), "issueNumber": issue["issueNumber"]}}})
            room_id = self.g.issue_by_number(args["issue_number"]).get("conversationRoomId")
            if not room_id:
                raise GaiaError("Created a conversation room, but the issue is still not linked to it.")
        self.g.request("POST", "comment", body={"data": {"type": "comment", "attributes": {
            "relatedId": room_id, "relatedTo": "conversationrooms", "comment": comment}}})
        return {"commented": True, "issueNumber": issue["issueNumber"],
                "url": summarize_issue(issue, self.ui).get("url")}

    ATTACHABLE = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".txt", ".md", ".log", ".csv", ".json", ".html")

    def attach_file(self, args):
        issue = self.g.issue_by_number(args["issue_number"])
        path = os.path.abspath(os.path.expanduser(args["file_path"]))
        if not os.path.isfile(path):
            raise GaiaError(f"File not found: {path}")
        if not path.lower().endswith(self.ATTACHABLE):
            raise GaiaError(f"Only these file types can be attached: {', '.join(self.ATTACHABLE)}")
        if os.path.getsize(path) > 10 * 1024 * 1024:
            raise GaiaError("File is larger than 10 MB.")
        with open(path, "rb") as f:
            self.g.upload(os.path.basename(path), f.read(), {"relatedId": issue["id"], "relatedTo": "issue"})
        return {"attached": os.path.basename(path), "issueNumber": issue["issueNumber"]}

    def log_time(self, args):
        issue = self.g.issue_by_number(args["issue_number"])
        days, hours, minutes = (int(args.get(k) or 0) for k in ("days", "hours", "minutes"))
        if min(days, hours, minutes) < 0 or days * 480 + hours * 60 + minutes == 0:
            raise GaiaError("Log a positive amount of time (days, hours and/or minutes).")
        kind = args.get("kind") or "spent"
        if kind not in ("spent", "estimate"):
            raise GaiaError("kind must be 'spent' or 'estimate'.")
        description = (args.get("description") or "").strip()
        if not description:
            raise GaiaError("description is required: say what the time was spent on.")
        attrs = {"issueId": issue["id"], "projectId": issue.get("projectId"),
                 "projectShortcode": issue.get("projectShortcode"), "days": days, "hours": hours,
                 "minutes": minutes, "description": description,
                 "context": "spent" if kind == "spent" else "est"}
        if kind == "spent":
            spent_on = args.get("spent_on") or date.today().isoformat()
            date.fromisoformat(spent_on)
            attrs["spentOn"] = spent_on
        self.g.request("POST", "timelog", body={"data": {"type": "timelog", "attributes": attrs}})
        return {"logged": format_minutes(days * 480 + hours * 60 + minutes), "kind": kind,
                "issueNumber": issue["issueNumber"], "on": attrs.get("spentOn")}

    def _resolve_fields(self, p, statuses, args, creating):
        """Map friendly tool arguments to Gaia issue attributes."""
        attrs = {}
        if args.get("subject") is not None:
            attrs["subject"] = args["subject"]
        if args.get("description") is not None:
            attrs["description"] = args["description"]
        if args.get("type") is not None or creating:
            labels = self.g.type_labels()
            attrs["typeId"] = self.g.pick(p.get("issuetypes") or [], args.get("type") or "Task", "type",
                                          alias=lambda t: labels.get(t.get("name")))["id"]
        if args.get("status") is not None or creating:
            status = self.g.pick(statuses, args.get("status") or "new", "status")
            # Send the name too, as the UI does: Gaia's status notification and activity entry look for a
            # change of `status` in the audit; a statusId-only change is copied by a DB trigger after the audit.
            attrs["statusId"], attrs["status"] = status["id"], status["name"]
        if args.get("priority") is not None or creating:
            prio = str(args.get("priority") or "medium").lower()
            if prio not in PRIORITIES:
                raise GaiaError(f"Unknown priority '{prio}'. Options: {', '.join(PRIORITIES)}.")
            attrs["priority"] = prio
        if args.get("milestone") is not None:
            attrs["milestoneId"] = ("" if args["milestone"] == "" else
                                    self.g.pick(p.get("milestones") or [], args["milestone"], "milestone")["id"])
        if args.get("assignee") is not None or creating:
            attrs["assignee"] = self.g.member(p, args.get("assignee") or "me")["id"]
        for src, dst in (("start_date", "startDate"), ("end_date", "endDate")):
            if args.get(src) is not None:
                date.fromisoformat(args[src])  # reject anything that isn't YYYY-MM-DD
                attrs[dst] = args[src]
        if args.get("parent_issue_number") is not None:
            attrs["parentId"] = self.g.issue_by_number(args["parent_issue_number"])["id"]
        return attrs

    def _ensure_milestone(self, p, name):
        """(the project's milestone `name` or with that id, whether it was just created). A missing one is created:
        in progress, from today for 30 days."""
        key = str(name).strip().lower()
        for m in p.get("milestones") or []:
            if key in ((m.get("name") or "").strip().lower(), str(m.get("id") or "").lower()):
                return m, False
        today = date.today()
        attrs = {"name": str(name).strip(), "description": f"Created by {self.g.me().get('name') or 'an agent'} "
                                                           f"for new issues.",
                 "startDate": today.isoformat(), "endDate": (today + timedelta(days=30)).isoformat(),
                 "status": "in_progress", "milestoneType": "milestone", "projectId": p["id"]}
        payload = self.g.request("POST", "milestone", body={"data": {"type": "milestone", "attributes": attrs}})
        created = self.g.flatten(payload)
        if not created or not created[0].get("id"):
            raise GaiaError(f"Gaia did not confirm the new milestone: {json.dumps(payload)[:1000]}")
        return created[0], True

    def create_milestone(self, args):
        p = self.g.project(args["project"], rels="milestones")
        name = str(args.get("name") or "").strip()
        if not name:
            raise GaiaError("A milestone needs a name.")
        milestone, created = self._ensure_milestone(p, name)
        if created and (args.get("start_date") or args.get("end_date") or args.get("description")):
            changes = {}
            for src, dst in (("start_date", "startDate"), ("end_date", "endDate")):
                if args.get(src):
                    date.fromisoformat(args[src])  # reject anything that isn't YYYY-MM-DD
                    changes[dst] = args[src]
            if args.get("description"):
                changes["description"] = args["description"]
            self.g.request("PATCH", f"milestone/{milestone['id']}",
                           body={"data": {"type": "milestone", "id": milestone["id"], "attributes": changes}})
            milestone.update(changes)
        return {"milestone": {"name": milestone.get("name"), "id": milestone.get("id"),
                              "startDate": milestone.get("startDate"), "endDate": milestone.get("endDate")},
                "created": created, "project": p.get("name")}

    def create_issue(self, args):
        p, statuses = self.g.project_options(args["project"])
        me = self.g.me()
        # House rule: Claude's issues go in the project's Claude milestone unless told otherwise. A milestone that
        # doesn't exist yet (the default or a named one) is created; "" means the backlog.
        wanted = args.get("milestone")
        if wanted is None:
            wanted = os.environ.get("GAIA_DEFAULT_MILESTONE", "Claude-v1")
        milestone, made = (None, False) if wanted == "" else self._ensure_milestone(p, wanted)
        attrs = self._resolve_fields(p, statuses, {k: v for k, v in args.items() if k != "milestone"}, creating=True)
        attrs["milestoneId"] = milestone["id"] if milestone else ""
        attrs["description"] = with_complexity(attrs.get("description"), args.get("complexity"))
        attrs.setdefault("startDate", date.today().isoformat())
        # Prometheus requires an end date and shows "Invalid date" without one.
        attrs.setdefault("endDate", (date.fromisoformat(attrs["startDate"]) + timedelta(days=7)).isoformat())
        attrs.update({"projectId": p["id"], "projectShortcode": p.get("shortCode"),
                      "owner": me["id"], "reportedUser": me["id"]})
        payload = self.g.request("POST", "issue", body={"data": {"type": "issue", "attributes": attrs}})
        created = self.g.flatten(payload)
        if not created or not created[0].get("issueNumber"):
            raise GaiaError(f"Gaia did not confirm the new issue: {json.dumps(payload)[:1500]}")
        issue = self.g.issue_by_number(created[0]["issueNumber"])
        out = {"created": summarize_issue(issue, self.ui, self.g.type_labels())}
        if made:
            out["milestoneCreated"] = milestone.get("name")
        return out

    def update_issue(self, args):
        issue = self.g.issue_by_number(args["issue_number"])
        p, statuses = self.g.project_options((issue.get("project") or {}).get("shortCode")
                                             or issue.get("projectShortcode"))
        attrs = self._resolve_fields(p, statuses, args, creating=False)
        if args.get("complexity") is not None:
            attrs["description"] = with_complexity(attrs.get("description", issue.get("description")),
                                                   args["complexity"])
        elif "description" in attrs and not read_complexity(attrs["description"]):
            # A new description keeps the issue's complexity unless it states one itself.
            kept = read_complexity(issue.get("description"))
            if kept:
                attrs["description"] = with_complexity(attrs["description"], kept)
        if not attrs:
            raise GaiaError("Nothing to update: pass at least one field to change.")
        self.g.request("PATCH", f"issue/{issue['id']}",
                       body={"data": {"type": "issue", "id": issue["id"], "attributes": attrs}})
        updated = self.g.issue_by_number(args["issue_number"])
        return {"updated": summarize_issue(updated, self.ui, self.g.type_labels()), "changedFields": sorted(attrs)}

    # -- Ideas ---------------------------------------------------------------------------------

    @staticmethod
    def _json_text(value, label):
        """A JSON column's text: strings pass through, lists and dicts are encoded."""
        if value is None or isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            raise GaiaError(f"{label} must be JSON.")

    def _created(self, payload, what):
        created = self.g.flatten(payload)
        if not created or not created[0].get("id"):
            raise GaiaError(f"Gaia did not confirm the new {what}: {json.dumps(payload)[:1000]}")
        return created[0]

    def _idea(self, idea_id, rels=None):
        found = self.g.find("idea", f"(Idea.id : {idea_id})", rels=rels, limit=1)
        if not found:
            raise GaiaError(f"Idea '{idea_id}' was not found or is not visible to {self.g.email}.")
        return found[0]

    def get_idea(self, args):
        # messages and plans have their own unlimited queries: through `rels` on this limit=1 read, Gaia caps the
        # related records too, and a thread would be cut short
        idea = self._idea(args["idea"], rels="project")
        messages = self.g.find("ideamessage", f"(Ideamessage.ideaId : {idea['id']})", limit=-1)
        plans = self.g.find("ideaplan", f"(Ideaplan.ideaId : {idea['id']})", limit=-1)
        plan_issues = self.g.find("ideaplanissue", f"(Ideaplanissue.ideaId : {idea['id']})", rels="issue", limit=-1)
        return {
            "id": idea["id"], "title": idea.get("title"), "description": idea.get("description"),
            "status": idea.get("status"), "projectId": idea.get("projectId"), "createdUser": idea.get("createdUser"),
            "projectShortCode": (idea.get("project") or {}).get("shortCode"),
            "messages": sorted(messages, key=lambda r: r.get("dateCreated") or ""),
            "plans": sorted(plans, key=lambda r: int(r.get("version") or 0)),
            "planIssues": [{"id": r.get("id"), "ideaplanId": r.get("ideaplanId"), "key": r.get("key"),
                            "issueId": r.get("issueId"),
                            "issueNumber": (r.get("issue") or {}).get("issueNumber"),
                            "projectShortcode": (r.get("issue") or {}).get("projectShortcode")} for r in plan_issues],
        }

    def list_ideas(self, args):
        p = self.g.project(args["project"])
        clauses = [f"(Idea.projectId : {p['id']})"]
        if args.get("status"):
            # Gaia's Clause parser pastes values into quoted PHQL, so only let plain text through.
            status = re.sub(r"[^\w.-]", "", str(args["status"]))
            clauses.append(f"(Idea.status : {status})")
        rows = self.g.find("idea", "(" + " AND ".join(clauses) + ")", sort="Idea.dateCreated", order="DESC", limit=-1)
        return {"project": p.get("name"), "count": len(rows),
                "ideas": [{"id": r.get("id"), "title": r.get("title"), "status": r.get("status"),
                           "dateCreated": r.get("dateCreated")} for r in rows]}

    def add_idea_message(self, args):
        body = (args.get("body") or "").strip()
        if not body:
            raise GaiaError("body is empty.")
        attrs = {"ideaId": args["idea"], "body": body, "kind": args.get("kind") or "message"}
        for key in ("steps", "screens"):
            if args.get(key) is not None:
                attrs[key] = self._json_text(args[key], key)
        if args.get("ideaplanId"):
            attrs["ideaplanId"] = args["ideaplanId"]
        if args.get("costUsd") is not None:
            attrs["costUsd"] = float(args["costUsd"])
        payload = self.g.request("POST", "ideamessage", body={"data": {"type": "ideamessage", "attributes": attrs}})
        return {"id": self._created(payload, "idea message")["id"], "ideaId": args["idea"]}

    def add_idea_plan(self, args):
        body = self._json_text(args.get("body"), "body")
        if not body:
            raise GaiaError("body is empty.")
        payload = self.g.request("POST", "ideaplan", body={"data": {"type": "ideaplan", "attributes": {
            "ideaId": args["idea"], "body": body}}})
        plan = self._created(payload, "idea plan")
        if plan.get("version") is None:  # Gaia numbers the plan when it saves it
            found = self.g.find("ideaplan", f"(Ideaplan.id : {plan['id']})", limit=1)
            plan = found[0] if found else plan
        return {"id": plan["id"], "version": plan.get("version"), "status": plan.get("status")}

    def update_idea_plan(self, args):
        attrs = {k: args[k] for k in ("status", "error") if args.get(k) is not None}
        if not attrs:
            raise GaiaError("Nothing to update: pass a status and/or an error.")
        self.g.request("PATCH", f"ideaplan/{args['plan']}",
                       body={"data": {"type": "ideaplan", "id": args["plan"], "attributes": attrs}})
        return {"updated": args["plan"], "changedFields": sorted(attrs)}

    def add_idea_plan_issue(self, args):
        p = self.g.project(args["project"])
        issue = self.g.issue_by_number(args["issue_number"])
        if issue.get("projectId") not in (None, p["id"]):
            raise GaiaError(f"Issue #{args['issue_number']} is not in project {p.get('name')}.")
        plan = self.g.find("ideaplan", f"(Ideaplan.id : {args['plan']})", limit=1)
        if not plan:
            raise GaiaError(f"Idea plan '{args['plan']}' was not found or is not visible to {self.g.email}.")
        payload = self.g.request("POST", "ideaplanissue", body={"data": {"type": "ideaplanissue", "attributes": {
            "ideaId": plan[0]["ideaId"], "ideaplanId": plan[0]["id"], "key": args["key"], "issueId": issue["id"]}}})
        return {"id": self._created(payload, "idea plan issue")["id"], "key": args["key"],
                "issueNumber": issue.get("issueNumber")}

    def upload_idea_screen(self, args):
        key = str(args.get("key") or "").strip()
        if not key:
            raise GaiaError("key is required.")
        idea = self._idea(args["idea"])
        name = re.sub(r"[^\w.-]", "_", key)
        if not name.lower().endswith(".png"):
            name += ".png"
        if args.get("path"):
            path = os.path.abspath(os.path.expanduser(args["path"]))
            if not os.path.isfile(path):
                raise GaiaError(f"File not found: {path}")
            with open(path, "rb") as f:
                content = f.read()
        elif args.get("png_base64"):
            try:
                content = base64.b64decode(args["png_base64"], validate=True)
            except ValueError:
                raise GaiaError("png_base64 is not valid base64.")
        else:
            raise GaiaError("Pass the PNG as path or png_base64.")
        if not content.startswith(b"\x89PNG"):
            raise GaiaError("The screen must be a PNG.")
        if len(content) > 10 * 1024 * 1024:
            raise GaiaError("File is larger than 10 MB.")
        reply = self.g.upload(name, content, {"relatedId": idea["id"], "relatedTo": "idea"})
        return {"uploadId": self._created(reply, "upload")["id"], "key": key, "name": name, "ideaId": idea["id"]}


IDEA_ARG = {"type": "string", "description": "The idea's id."}
PROJECT_ARG = {"type": "string",
               "description": "Project name or short code. Gaia work goes in 'A Gaia', Prometheus work in 'A Prometheus'."}
DESCRIPTION_ARG = {"type": "string",
                   "description": "Issue description as HTML (Prometheus renders HTML), e.g. <p>…</p><ul><li>…</li></ul>."}
COMPLEXITY_ARG = {"type": "string", "enum": list(COMPLEXITIES),
                  "description": "Recommended model for the agent that works it. complex (Opus) only when the work "
                                 "itself needs judgement: an open design decision, a contract or cross-repo change, a "
                                 "migration, or an unclear root cause. simple (Sonnet) when the issue already fixes "
                                 "the design and contract, even if the change is large or touches several files "
                                 "(size is not complexity). Stored as a 'Complexity: <value>' line at the end of the "
                                 "description."}
FIELD_ARGS = {
    "type": {"type": "string", "description": "Issue type label or value, any case, e.g. Task, Bug, Story, Epic, Sub Task (sub_task), Research, "
                                       "Change Request (change_request)."},
    "status": {"type": "string", "description": "Status name, e.g. new, in_progress, in_review, feedback, pending, deferred, done, wont_fix."},
    "priority": {"type": "string", "enum": list(PRIORITIES)},
    "milestone": {"type": "string", "description": "Milestone name or id. Empty string means backlog."},
    "assignee": {"type": "string", "description": "Project member email, name or id, or 'me'."},
    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
    "parent_issue_number": {"type": "integer", "description": "Make this a child of another issue."},
}

TOOLS = [
    ("gaia_whoami", "whoami", "Show which Gaia user this server signs in as and which projects it belongs to.",
     {"type": "object", "properties": {}}),
    ("gaia_list_projects", "list_projects", "List projects visible to the Claude user, optionally filtered by name or short code.",
     {"type": "object", "properties": {"search": {"type": "string"}}}),
    ("gaia_project_options", "project_options",
     "Show a project's issue types, statuses, priorities, milestones and members. Use before creating or updating issues.",
     {"type": "object", "properties": {"project": PROJECT_ARG}, "required": ["project"]}),
    ("gaia_list_issues", "list_issues", "List a project's issues, newest first, with optional filters.",
     {"type": "object", "properties": {
         "project": PROJECT_ARG,
         "status": {"type": "string"},
         "search": {"type": "string", "description": "Text the subject contains."},
         "assigned_to_me": {"type": "boolean"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
      "required": ["project"]}),
    ("gaia_add_comment", "add_comment",
     "Add a comment to an issue's thread (HTML). Use it to record what was done, findings, decisions and next steps.",
     {"type": "object", "properties": {"issue_number": {"type": "integer"},
                                       "comment": {"type": "string", "description": "HTML, e.g. <p>…</p><ul><li>…</li></ul>."}},
      "required": ["issue_number", "comment"]}),
    ("gaia_attach_file", "attach_file",
     "Attach a local file (screenshot, PDF, text, log) to an issue; it shows in the issue's Files box. Max 10 MB.",
     {"type": "object", "properties": {"issue_number": {"type": "integer"},
                                       "file_path": {"type": "string", "description": "Absolute path to the local file."}},
      "required": ["issue_number", "file_path"]}),
    ("gaia_log_time", "log_time",
     "Log time against an issue: time spent on work (default), or an estimate. A day is 8 hours.",
     {"type": "object", "properties": {
         "issue_number": {"type": "integer"},
         "hours": {"type": "integer", "minimum": 0},
         "minutes": {"type": "integer", "minimum": 0},
         "days": {"type": "integer", "minimum": 0},
         "description": {"type": "string", "description": "What the time was spent on."},
         "spent_on": {"type": "string", "description": "YYYY-MM-DD, defaults to today. Ignored for estimates."},
         "kind": {"type": "string", "enum": ["spent", "estimate"]}},
      "required": ["issue_number", "description"]}),
    ("gaia_get_issue", "get_issue",
     "Get one issue by its issue number, with description, its latest comments (oldest first; olderComments says "
     "whether there are more) and logged/estimated time.",
     {"type": "object", "properties": {
         "issue_number": {"type": "integer"},
         "comments": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Latest comments to return (default 10)."}},
      "required": ["issue_number"]}),
    ("gaia_create_issue", "create_issue",
     "Create an issue in a Projects4Me project. Defaults: type Task, status new, priority medium, assignee me, "
     "start date today, end date a week later, milestone Claude-v1. A milestone the project doesn't have yet "
     "(Claude-v1 or the one you name) is created; an empty milestone means the backlog. "
     "Always judge its complexity: it picks the model the agent works it with.",
     {"type": "object", "properties": dict({"project": PROJECT_ARG, "subject": {"type": "string"},
                                            "description": DESCRIPTION_ARG, "complexity": COMPLEXITY_ARG},
                                           **FIELD_ARGS),
      "required": ["project", "subject", "complexity"]}),
    ("gaia_create_milestone", "create_milestone",
     "Create a milestone in a project (or return the existing one with that name). Defaults: in progress, from today "
     "for 30 days.",
     {"type": "object", "properties": {"project": PROJECT_ARG, "name": {"type": "string"},
                                       "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                                       "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                                       "description": {"type": "string"}},
      "required": ["project", "name"]}),
    ("gaia_update_issue", "update_issue",
     "Update an existing issue by number: status, priority, type, subject, description, complexity, assignee, "
     "milestone, dates or parent.",
     {"type": "object", "properties": dict({"issue_number": {"type": "integer"}, "subject": {"type": "string"},
                                            "description": DESCRIPTION_ARG, "complexity": COMPLEXITY_ARG},
                                           **FIELD_ARGS),
      "required": ["issue_number"]}),
    ("gaia_get_idea", "get_idea",
     "Get an idea with its messages, plans and plan issues, and the project's short code.",
     {"type": "object", "properties": {"idea": IDEA_ARG}, "required": ["idea"]}),
    ("gaia_list_ideas", "list_ideas", "List a project's ideas, newest first, optionally by status.",
     {"type": "object", "properties": {"project": PROJECT_ARG, "status": {"type": "string"}}, "required": ["project"]}),
    ("gaia_add_idea_message", "add_idea_message",
     "Post a message to an idea's thread. screens: list of {uploadId, key, title, version, width, height} from "
     "gaia_upload_idea_screen.",
     {"type": "object", "properties": {
         "idea": IDEA_ARG, "body": {"type": "string"}, "kind": {"type": "string", "description": "Default 'message'."},
         "steps": {"type": ["array", "string"], "description": "Progress steps (JSON)."},
         "ideaplanId": {"type": "string"}, "costUsd": {"type": "number"},
         "screens": {"type": ["array", "string"]}},
      "required": ["idea", "body"]}),
    ("gaia_add_idea_plan", "add_idea_plan", "Propose a plan for an idea. Returns the plan's id and version.",
     {"type": "object", "properties": {"idea": IDEA_ARG,
                                       "body": {"type": ["object", "array", "string"], "description": "The plan, as JSON."}},
      "required": ["idea", "body"]}),
    ("gaia_update_idea_plan", "update_idea_plan", "Set an idea plan's status and/or error.",
     {"type": "object", "properties": {"plan": {"type": "string", "description": "The plan's id."},
                                       "status": {"type": "string"}, "error": {"type": "string"}},
      "required": ["plan"]}),
    ("gaia_add_idea_plan_issue", "add_idea_plan_issue",
     "Record the issue created for a plan's key. Resolves the issue by number.",
     {"type": "object", "properties": {"plan": {"type": "string", "description": "The plan's id."},
                                       "key": {"type": "string"}, "issue_number": {"type": "integer"},
                                       "project": PROJECT_ARG},
      "required": ["plan", "key", "issue_number", "project"]}),
    ("gaia_upload_idea_screen", "upload_idea_screen",
     "Upload a PNG screen to an idea. Returns the upload id to use in a message's screens.",
     {"type": "object", "properties": {"idea": IDEA_ARG, "key": {"type": "string"},
                                       "path": {"type": "string", "description": "Absolute path to a PNG."},
                                       "png_base64": {"type": "string", "description": "PNG bytes, base64 (instead of path)."}},
      "required": ["idea", "key"]}),
]


# ---------------------------------------------------------------------------------------------
# MCP over stdio
# ---------------------------------------------------------------------------------------------

class Server:
    def __init__(self):
        self.tools = Tools(Gaia())
        self.by_name = {name: method for name, method, _, _ in TOOLS}

    def handle(self, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {"protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": ("Projects4Me issue tracker (Gaia). Every piece of work needs an issue: "
                                     "Gaia work goes in project 'A Gaia', Prometheus work in 'A Prometheus'. "
                                     "Search with gaia_list_issues before creating to avoid duplicates. "
                                     "Issues go in the Claude-v1 milestone. While working, log time with "
                                     "gaia_log_time and record details with gaia_add_comment.")}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, _, d, s in TOOLS]}
        if method == "tools/call":
            name = params.get("name")
            if name not in self.by_name:
                raise LookupError(f"Unknown tool: {name}")
            try:
                result = getattr(self.tools, self.by_name[name])(params.get("arguments") or {})
                return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
            except (GaiaError, ValueError, KeyError) as e:
                msg = f"Missing argument: {e}" if isinstance(e, KeyError) else str(e)
                return {"content": [{"type": "text", "text": msg}], "isError": True}
        raise LookupError(f"Method not found: {method}")

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if "id" not in msg:
                continue  # notification (e.g. notifications/initialized)
            try:
                self.send({"jsonrpc": "2.0", "id": msg["id"], "result": self.handle(msg)})
            except LookupError as e:
                self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(e)}})
            except Exception as e:  # keep the server alive on unexpected errors
                log(f"error handling {msg.get('method')}: {e!r}")
                self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})

    @staticmethod
    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    load_env_file(os.environ.get("GAIA_MCP_ENV", os.path.join(LOCAL_DIR, "gaia-mcp.env")))
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    Server().run()
