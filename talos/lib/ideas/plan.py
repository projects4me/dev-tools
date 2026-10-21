"""Implementation plans: how Nestor writes one, how it is checked, and the order its issues are created in.

Nestor ends a reply with one fenced block

    ```nestor-plan
    {"summary": "...", "issues": [{"key": "api", "project": "A Gaia", "subject": "...", "description": "<p>..</p>",
      "type": "Task", "priority": "medium", "complexity": "simple", "dependsOn": []}, ...]}
    ```

The block is data: approval creates exactly what it says, so it is validated strictly here.
"""

import json
import re

from .sanitize import clean_html

MAX_SUBJECT = 200
MAX_DESCRIPTION = 20_000
MAX_ISSUES = 30
BLOCK = re.compile(r"```nestor-plan[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
PRIORITIES = ("blocker", "critical", "high", "medium", "low")
COMPLEXITIES = ("simple", "complex")
TYPES = ("Task", "Bug", "Story", "Epic", "Sub Task", "Research", "Change Request")
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
EXISTING = re.compile(r"^[a-z]+-\d+$")      # an issue that already exists, e.g. "adt-4061"


def internal(depends, keys):
    """The dependsOn entries that are keys of this plan (the rest are existing issues)."""
    return [d for d in depends if d in keys]


def extract(reply):
    """(reply text without the plan block, raw plan JSON text or None). Only the last block counts."""
    blocks = list(BLOCK.finditer(reply or ""))
    if not blocks:
        return (reply or "").strip(), None
    last = blocks[-1]
    text = (reply[:last.start()] + reply[last.end():]).strip()
    return text, last.group(1)


def parse(raw, projects):
    """The plan as a normalised dict, or raise ValueError listing every problem.

    `projects`: project name -> short code, of the projects issues may go to."""
    try:
        plan = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"not valid JSON ({e})")
    if not isinstance(plan, dict):
        raise ValueError("the block must be a JSON object with summary and issues")
    errors = []
    summary = plan.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary: required text")
    issues = plan.get("issues")
    if not isinstance(issues, list) or not issues:
        errors.append("issues: required, a non-empty list")
        issues = []
    elif len(issues) > MAX_ISSUES:
        errors.append(f"issues: at most {MAX_ISSUES} in one plan; split the idea")
    by_code = {code.lower(): name for name, code in projects.items()}
    by_name = {name.lower(): name for name in projects}
    out, keys = [], set()
    for n, issue in enumerate(issues, 1):
        where = f"issues[{n}]"
        if not isinstance(issue, dict):
            errors.append(f"{where}: must be an object")
            continue
        key = issue.get("key")
        if not isinstance(key, str) or not KEY.match(key):
            errors.append(f"{where}.key: required, lowercase letters, digits and dashes")
        elif key in keys:
            errors.append(f"{where}.key: '{key}' is used twice")
        keys.add(key)
        where = f"issue '{key}'" if isinstance(key, str) else where
        project = str(issue.get("project") or "").strip().lower()
        name = by_name.get(project) or by_code.get(project)
        if not name:
            errors.append(f"{where}.project: one of {', '.join(projects)}")
        for field, limit in (("subject", MAX_SUBJECT), ("description", MAX_DESCRIPTION)):
            if not isinstance(issue.get(field), str) or not issue[field].strip():
                errors.append(f"{where}.{field}: required text")
            elif len(issue[field]) > limit:
                errors.append(f"{where}.{field}: at most {limit} characters")
        description = clean_html(issue.get("description")) if isinstance(issue.get("description"), str) else ""
        if isinstance(issue.get("description"), str) and issue["description"].strip() and not description:
            errors.append(f"{where}.description: no text left after removing unsupported HTML")
        type_ = issue.get("type") or "Task"
        if type_ not in TYPES:
            errors.append(f"{where}.type: one of {', '.join(TYPES)}")
        priority = str(issue.get("priority") or "medium").lower()
        if priority not in PRIORITIES:
            errors.append(f"{where}.priority: one of {', '.join(PRIORITIES)}")
        complexity = str(issue.get("complexity") or "").lower()
        if complexity not in COMPLEXITIES:
            errors.append(f"{where}.complexity: required, simple or complex")
        depends = issue.get("dependsOn") or []
        if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
            errors.append(f"{where}.dependsOn: a list of keys")
            depends = []
        subject = " ".join(issue["subject"].split()) if isinstance(issue.get("subject"), str) else None
        out.append({"key": key, "project": name, "projectCode": projects.get(name), "subject": subject,
                    "description": description, "type": type_, "priority": priority,
                    "complexity": complexity, "dependsOn": depends})
    for issue in out:
        for d in issue["dependsOn"]:
            if d not in keys and not EXISTING.match(d):
                errors.append(f"issue '{issue['key']}'.dependsOn: '{d}' is neither a key in this plan nor an "
                              f"existing issue like adt-4061")
            elif d == issue["key"]:
                errors.append(f"issue '{issue['key']}'.dependsOn: an issue can't depend on itself")
    if not errors:
        try:
            ordered(out)
        except ValueError as e:
            errors.append(str(e))
    if errors:
        raise ValueError("; ".join(errors))
    return {"summary": summary.strip(), "issues": out}


def ordered(issues):
    """Issues in creation order: every issue after the ones it depends on, otherwise as Nestor listed them."""
    keys = {i["key"] for i in issues}
    pending, done, out = list(issues), set(), []
    while pending:
        ready = [i for i in pending if all(d in done for d in internal(i["dependsOn"], keys))]
        if not ready:
            raise ValueError("dependsOn: circular dependency between " + ", ".join(i["key"] for i in pending))
        nxt = ready[0]
        out.append(nxt)
        done.add(nxt["key"])
        pending.remove(nxt)
    return out
