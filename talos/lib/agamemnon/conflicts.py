"""The guard on a merge conflict Agamemnon resolves itself (`aga resolve-conflict`): code decides, never the model.

A file qualifies only when every conflicting hunk is additive on both sides: in git's diff3 form (ours, the merge
base, theirs) the base part is empty, so both sides only added lines at the same place (two new scenarios, steps,
routes or registry entries). The resolution keeps every line both sides added, ours first, and changes nothing
else. Any other hunk (a line changed or removed on either side) is not resolved here."""

import re
from dataclasses import dataclass, field

OURS, BASE, THEIRS, END = "<<<<<<<", "|||||||", "=======", ">>>>>>>"
_MARKER = re.compile(r"^(<{7}|\|{7}|={7}|>{7})(?:\s|$)")


@dataclass
class Resolution:
    path: str
    text: str = None                            # the resolved file; None when it is not additive
    hunks: int = 0                              # conflicting hunks found
    problems: list = field(default_factory=list)

    @property
    def additive(self):
        return not self.problems and self.text is not None


def _marker(line):
    m = _MARKER.match(line)
    return m.group(1) if m else None


def resolve(path, text):
    """The Resolution of one file holding diff3 conflict markers (`git checkout --conflict=diff3`)."""
    out, problems, hunks = [], [], 0
    lines = text.splitlines(keepends=True)
    i, n = 0, len(lines)
    while i < n:
        if _marker(lines[i]) != OURS:
            if _marker(lines[i]) is not None:
                problems.append(f"line {i + 1}: a stray conflict marker")
            out.append(lines[i])
            i += 1
            continue
        start, hunks = i + 1, hunks + 1
        parts, current, i = {OURS: [], BASE: None, THEIRS: None}, OURS, i + 1
        while i < n and _marker(lines[i]) != END:
            mark = _marker(lines[i])
            if mark == BASE and current == OURS:
                parts[BASE], current = [], BASE
            elif mark == THEIRS and current in (OURS, BASE):
                parts[THEIRS], current = [], THEIRS
            elif mark is not None:
                problems.append(f"line {i + 1}: a conflict marker out of order")
                break
            else:
                parts[current].append(lines[i])
            i += 1
        if i >= n or _marker(lines[i]) != END:
            problems.append(f"line {start}: a conflict hunk that does not end")
            break
        i += 1
        if parts[BASE] is None or parts[THEIRS] is None:
            problems.append(f"line {start}: not in diff3 form (no base part)")
        elif parts[BASE]:
            problems.append(f"line {start}: both sides changed lines of the base ({len(parts[BASE])} line(s)), "
                            f"not only added lines")
        elif not parts[OURS] or not parts[THEIRS]:
            problems.append(f"line {start}: one side added nothing")
        else:
            out.extend(parts[OURS] + parts[THEIRS])
    if not hunks:
        problems.append("no conflict hunk found")
    return Resolution(path, None if problems else "".join(out), hunks, problems)
