"""`ai ship` size gate: the real number of hand-written files in a change, against Nestor's file budget.

Generated paths (`shipSize.generated` in config.json, `**` matches across folders) don't count. Over
`warnOver` the ship prints a warning; over `refuseOver` it stops unless ship.json gives a `sizeReason`,
which goes into the PR's Notes and the tracker notes. Like the cost gate, it is its own module so
`ship.py` only calls it.
"""

import html
import re

from .common import AiError, config

DEFAULTS = {"warnOver": 10, "refuseOver": 15, "generated": []}
ESTIMATE_RE = re.compile(r"Estimated files:\s*(\d+)", re.I)


def settings():
    return {**DEFAULTS, **config().get("shipSize", {})}


def _glob(pattern):
    """A path glob as a regex: `**` crosses folders, `*` and `?` stay inside one."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + "$")


def hand_written(files, generated):
    """The files that aren't generated."""
    rx = [_glob(p) for p in generated]
    return [f for f in files if not any(r.match(f.replace("\\", "/")) for r in rx)]


def estimate_from(description):
    """N from an "Estimated files: N" line in the issue description (HTML or text), else None."""
    m = ESTIMATE_RE.search(re.sub(r"<[^>]+>", "", description or ""))
    return int(m.group(1)) if m else None


def check(files, size_reason=None, estimate=None, cfg=None):
    """Returns {count, line, warning, reason}; raises AiError over the refuse limit without a sizeReason."""
    cfg = cfg or settings()
    count = len(hand_written(files, cfg["generated"]))
    skipped = len(files) - count
    line = f"Size: {count} hand-written file(s)" + (f" ({skipped} generated excluded)" if skipped else "")
    if estimate is not None:
        line += f", estimated {estimate}"
    reason = (size_reason or "").strip() or None
    warning = None
    if count > cfg["refuseOver"]:
        if not reason:
            raise AiError(f"{line}: over {cfg['refuseOver']} files. Split the issue, or add `sizeReason` to "
                          "ship.json to say why it can't be split")
        warning = f"{line}: over {cfg['refuseOver']} files; allowed because: {reason}"
    elif count > cfg["warnOver"]:
        warning = f"{line}: over {cfg['warnOver']} files, consider splitting"
    return {"count": count, "line": line, "warning": warning, "reason": reason}


def pr_note(result):
    """Markdown for the PR's Notes section (empty when there is nothing to say)."""
    if not result["reason"]:
        return ""
    return f"## Notes\n{result['line']}. Why it is this large: {result['reason']}"


def tracker_note(result):
    text = result["line"] + (f". Why it is this large: {result['reason']}" if result["reason"] else "")
    return f"<p>{html.escape(text)}</p>"
