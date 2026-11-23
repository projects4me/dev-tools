"""An issue's links (blocked by, blocks, parent, children, split, relates) as lines for `ai context` and `ai kb show`.

The links come from gaia-mcp's `links_of`; this module only formats them. Issues created before links existed carry
their dependencies as text ("depends on #N"); those lines are kept as the legacy "Blocked by (line)" group.
"""

import re

from .common import tracker

CAP = 10
CLOSED = ("done", "wontfix", "closed")
GROUPS = (("blockedBy", "Blocked by"), ("blocks", "Blocks"), ("parent", "Parent"), ("children", "Children"),
          ("splitFrom", "Split from"), ("splitInto", "Split into"), ("relates", "Relates"))
_LEGACY = re.compile(r"(?:depends on|blocked by|after)[^#\n]{0,40}#(\d{3,5})", re.I)


def fetch(number):
    """The links of an issue, or the reason they are unavailable (never a stack trace): (links, None) or (None, why)."""
    try:
        return tracker().g.links_of(number), None
    except Exception as e:
        return None, str(e) or e.__class__.__name__


def is_open(end):
    return re.sub(r"[\s_-]", "", str(end.get("status") or "")).lower() not in CLOSED


def legacy_blockers(text, own, linked=()):
    """Issue numbers named by 'depends on / blocked by / after #N' in the text, minus the issue and real links."""
    return sorted({int(n) for n in _LEGACY.findall(text or "")} - {own} - set(linked))


def _end(end, mark=False):
    code = end.get("projectShortcode") or ""
    ref = f"{code}-{end.get('issueNumber')}" if code else f"#{end.get('issueNumber')}"
    return f"{'! ' if mark and is_open(end) else ''}{ref} [{end.get('status') or '?'}] {end.get('subject') or ''}"


def _order(key, ends):
    return sorted(ends, key=lambda e: not is_open(e)) if key == "blockedBy" else list(ends)


def lines(links, legacy=()):
    """One line per group: 'Blocked by: ! adt-1 [new] subject; ...', open blockers first and marked '!'."""
    out = []
    for key, label in GROUPS:
        ends = links.get(key) or []
        ends = [ends] if isinstance(ends, dict) else ends
        items = [_end(e, key == "blockedBy") for e in _order(key, ends)]
        if key == "blockedBy" and legacy:
            items += [f"#{n} (line)" for n in legacy]
        if not items:
            continue
        more = f" … {len(items) - CAP} more" if len(items) > CAP else ""
        out.append(f"{label}: " + "; ".join(items[:CAP]) + more)
    return out


def report(number, legacy=(), fetcher=fetch):
    """The links lines of an issue; a single 'unavailable' line when Gaia can't be reached, 'none' when empty."""
    links, why = fetcher(number)
    if links is None:
        return [f"links unavailable: {why}"]
    found = lines(links, legacy)
    return found or ["no links"]
