"""What Agamemnon reads from lint output: the one place for the patterns the classifier and lint-base share."""
import re

# a handback text that names a lint failure (`ai test`'s "lint: N errors", a lint rule or the todo files)
FAILURE = (r"\blint: \d+ errors?\b", r"\b\d+ lint (errors?|failures?|violations?)\b",
           r"\blint(:js|:hbs)? (fails?|failed|failing|errors?|reports?)\b", r"\.lint-todo\b",
           r"invalid-todo-violation-rule", r"eslint-suppressions")
# a failing lint error in `ai test`'s verdict lines ("  FAIL <file:line:col message (rule)>  <-- unexpected")
ERROR = re.compile(r"^\s+FAIL (.+?)(?:\s{2,}(?:\(known\)|<-- unexpected))?\s*$", re.M)
# the worker's errors include a recorded todo its diff made stale (`ai test`'s lint verdict says so in these words)
STALE_TODO = re.compile(r"invalid-todo-violation-rule|recorded todo for .{0,80} stale", re.I)


def names_failure(text):
    """Whether text names a lint failure."""
    return any(re.search(p, str(text or "").lower()) for p in FAILURE)


def errors(output):
    """The failing lint errors in `ai test --suite lint` output."""
    return ERROR.findall(output or "")


def stale_todo(text):
    """Whether text says a recorded lint todo went stale."""
    return bool(STALE_TODO.search(text or ""))
