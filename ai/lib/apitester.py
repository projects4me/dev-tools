"""Gaia api-tester helpers: append catalog cases without reformatting, and call the API as a profile."""

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .common import AiError, LOCAL_DIR, WORKSPACE, say

CATALOGS = WORKSPACE / "gaia" / "tools" / "api-tester" / "apis"
FIXTURES = WORKSPACE / "gaia" / "tools" / "api-tester" / "fixtures" / "default.json"
INLINE_KEYS = ("query", "expects", "store")  # acl.json keeps these on one line


def _render_acl(case):
    lines = ["    {"]
    items = list(case.items())
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        if k in INLINE_KEYS and isinstance(v, dict):
            inner = ", ".join(f'"{kk}": {json.dumps(vv, ensure_ascii=False)}' for kk, vv in v.items())
            lines.append(f'      "{k}": {{ {inner} }}{comma}')
        elif k == "body":
            body = json.dumps(v, indent=2, ensure_ascii=False).replace("\n", "\n      ")
            lines.append(f'      "body": {body}{comma}')
        else:
            lines.append(f'      "{k}": {json.dumps(v, ensure_ascii=False)}{comma}')
    lines.append("    }")
    return "\n".join(lines)


def _render_indented(case):
    return "\n".join("    " + l for l in json.dumps(case, indent=2, ensure_ascii=False).split("\n"))


def cmd_api_cases(args):
    """Append cases (JSON list in a file) to apis/<catalog>.json, matching that file's style."""
    path = CATALOGS / f"{args.catalog}.json"
    if args.catalog == "backend":
        raise AiError("backend.json is generated (bin/generate-backend-apis.php); add cases to client or acl")
    cases = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if isinstance(cases, dict):
        cases = [cases]
    text = path.read_text(encoding="utf-8")
    existing = set(re.findall(r'"id": "([^"]+)"', text))
    dupes = [c["id"] for c in cases if c["id"] in existing]
    if dupes:
        raise AiError(f"case ids already exist in {path.name}: {', '.join(dupes)}")
    tail = "\n    }\n  ]\n}\n"
    if not text.endswith(tail):
        raise AiError(f"{path.name} doesn't end with the expected '    }}\\n  ]\\n}}' layout")
    render = _render_acl if args.catalog == "acl" else _render_indented
    text = text[: -len(tail)] + "\n    },\n" + ",\n".join(render(c) for c in cases) + "\n  ]\n}\n"
    json.loads(text)
    path.write_text(text, encoding="utf-8", newline="\n")
    say(f"appended {len(cases)} case(s) to {path.relative_to(WORKSPACE)}")


# --- calling the API ------------------------------------------------------------------------

def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    return _request("POST", url, data, {"Content-Type": "application/x-www-form-urlencoded"})


def _request(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # local self-signed proxy only
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _credentials(target, profile):
    if target == "dev":
        env = {}
        for line in (LOCAL_DIR / "gaia-mcp" / "gaia-mcp.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
        base = env.get("GAIA_URL", "https://api.local-projects4.me")
        return base, env["GAIA_CLIENT_ID"], env["GAIA_CLIENT_SECRET"], env["GAIA_EMAIL"], env["GAIA_PASSWORD"]
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    auth = dict(fixtures["auth"])
    if profile and profile != "default":
        if profile not in fixtures["authProfiles"]:
            raise AiError(f"unknown profile '{profile}' (one of: default, {', '.join(fixtures['authProfiles'])})")
        auth.update(fixtures["authProfiles"][profile])
    return "https://api-test.local-projects4.me", auth["clientId"], auth["clientSecret"], auth["email"], auth["password"]


def cmd_call(args):
    """METHOD path [json] against the test API as an api-tester profile, or dev as Agent Odysseus."""
    base, client_id, secret, email, password = _credentials(args.target, args.profile)
    status, body = _post_form(base + "/api/v1/token", {"grant_type": "password", "client_id": client_id,
                                                       "client_secret": secret, "email": email,
                                                       "password": password})
    if status != 200:
        raise AiError(f"login failed ({status}): {body[:300]}")
    token = json.loads(body)["access_token"]
    path = args.path if args.path.startswith("/") else "/api/v1/" + args.path
    path = urllib.parse.quote(path, safe="/?&=:,()[]*@$+%!'")
    data = args.json.encode() if args.json else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status, body = _request(args.method.upper(), base + path, data, headers)
    say(f"HTTP {status}")
    if args.jq:
        try:
            doc = json.loads(body)
            for key in args.jq.split("."):
                doc = doc[int(key)] if key.isdigit() else doc[key]
            body = json.dumps(doc, indent=1, ensure_ascii=False)
        except (ValueError, KeyError, IndexError, TypeError) as e:
            say(f"(path '{args.jq}' not found: {e})")
    say(body if len(body) <= args.max else body[: args.max] + f"\n… ({len(body)} chars, raise --max to see more)")
