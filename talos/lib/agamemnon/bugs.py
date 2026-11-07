"""The Bug `aga file-bug` files for a CI failure that is already on the base branch: which failing job a signature
names, whether a filed bug is still open, and the bug's description. Pure: the caller reads Gaia, GitHub and the
journal and passes them in."""

import html
import re

FILE_BUG = "file-bug"
BUG_TYPE = "Bug"
BUG_PRIORITY = "high"
# a CI failure on the base branch has no known root cause yet, so the worker gets it on the stronger model
BUG_COMPLEXITY = "complex"
WONT_FIX = "wont_fix"


def normalize(signature):
    """The signature as it is compared and stored: trimmed, whitespace collapsed."""
    return " ".join(str(signature or "").split())


def job_of(signature, failing_on_base):
    """The job of the PR's failed jobs that also fail on the base branch that the signature names ("<job>" or
    "<job>: <test>"), the longest when several match; None when it names none of them."""
    names = sorted(failing_on_base, key=len, reverse=True)
    return next((j for j in names if signature == j or signature.startswith(f"{j}:")), None)


def is_closed(status, done_statuses):
    """Whether a bug in the status needs no more work: done (the worker's done statuses) or won't fix."""
    s = re.sub(r"[\s']+", "_", str(status or "").strip().lower()).replace("won_t", "wont")
    return s == WONT_FIX or s in {str(d).lower() for d in done_statuses}


def candidates(filed, signature, project):
    """The bugs filed earlier for the signature in the project (journal actions of file-bug that created one and are
    not undone), newest first; the caller asks Gaia which of them is still open."""
    out = []
    for action in reversed(filed):
        after = action["state_after"] or {}
        if (after.get("bug") and after.get("created") and not action["undone_at"]
                and normalize((action["args"] or {}).get("signature")) == signature
                and str(after.get("project") or "") == str(project or "")):
            out.append(int(after["bug"]))
    return out


def filed_on(filed, day):
    """How many bugs the journal actions filed on the day (YYYY-MM-DD); a link to an open one does not count."""
    return len([a for a in filed if str(a["at"] or "").startswith(day) and (a["state_after"] or {}).get("created")])


def _paragraphs(text):
    return "".join(f"<p>{html.escape(p.strip()).replace(chr(10), '<br>')}</p>"
                   for p in re.split(r"\n\s*\n", str(text or "")) if p.strip())


def description(display_name, blocked, signature, job, body, base, base_sha):
    """The bug's HTML description: who filed it and what it blocks, the failure, and when it is done."""
    e = html.escape
    return (f"<p>Filed by {e(display_name)}: blocks #{int(blocked)}</p>"
            f"<p><strong>Signature:</strong> <code>{e(signature)}</code></p>"
            f"{_paragraphs(body)}"
            f"<p><strong>Also fails on:</strong> <code>{e(base)}</code> at <code>{e(str(base_sha or '-'))}</code></p>"
            f"<p><strong>Acceptance:</strong> the <code>{e(job)}</code> job passes on <code>{e(base)}</code>, so "
            f"#{int(blocked)} can merge <code>{e(base)}</code> and carry on.</p>")
