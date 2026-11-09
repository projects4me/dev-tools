"""`ai gaia-dev <args>`: Gaia's gaia-dev generators, checks and queries on this workspace's Gaia checkout.

Headless runs have no PHP and the shared gaia-dev container mounts another checkout, so the command runs gaia-dev
through `gaiapack.gaia_dev` (local php when the checkout has its vendor folder, else a throwaway container of the
gaia image with the checkout mounted and no network). Only an allowlist of file-only commands runs: nothing that
touches a database (`migration migrate` dropped another branch's columns from pr4m, adt-4150). The container path
also has no network; the local-php path (a checkout with its own vendor folder) relies on the allowlist alone.
"""

import re

from . import gaiapack
from .common import AiError, repo_dir, require_matching_workspace

# (command words) -> (min, max positional arguments, {--option: takes a value}). Everything else is refused.
# Value options take the `--opt=value` form only, as gaia-dev parses them (`cases ... --method=GET`).
ALLOWED = {
    ("graph", "export"): (0, 0, {"--check": False}),
    ("docs", "generate"): (0, 0, {"--check": False}),
    ("check", "api-cases"): (0, 2, {}),
    ("check", "resource"): (1, 1, {}),
    ("model",): (1, 1, {}),
    ("route",): (1, 2, {}),
    ("acl",): (1, 2, {}),
    ("cases",): (1, 1, {"--method": True, "--profile": True, "--all": False}),
    ("assertions",): (0, 0, {}),
    ("profiles",): (0, 0, {}),
    ("fixtures",): (0, 0, {}),
}
POSITIONAL = re.compile(r"[A-Za-z0-9_/][A-Za-z0-9_./{}: -]*")
OPTION_VALUE = re.compile(r"[A-Za-z0-9_.-]+")
TIMEOUT = 600


def usage():
    return "; ".join(" ".join(words) for words in ALLOWED)


def validate(args):
    """Raise AiError unless args is an allowlisted gaia-dev command with acceptable arguments."""
    args = list(args)
    if not args:
        raise AiError(f"usage: ai gaia-dev <command> [args]. Allowed: {usage()}")
    words = next((w for w in sorted(ALLOWED, key=len, reverse=True) if tuple(args[:len(w)]) == w), None)
    if words is None:
        raise AiError(f"'gaia-dev {' '.join(args)}' is not allowed (it may write a database or isn't a generator, "
                      f"check or query). Allowed: {usage()}")
    low, high, options = ALLOWED[words]
    rest = args[len(words):]
    positional = [a for a in rest if not a.startswith("-")]
    for a in rest:
        if a.startswith("-"):
            name, _, value = a.partition("=")
            takes_value = options.get(name)
            if takes_value is None or bool(value) != takes_value or (value and not OPTION_VALUE.fullmatch(value)):
                raise AiError(f"option '{a}' is not allowed for 'gaia-dev {' '.join(words)}'"
                              f"{' (allowed: ' + ', '.join(sorted(options)) + ')' if options else ''}")
        elif not POSITIONAL.fullmatch(a) or ".." in a:
            raise AiError(f"argument '{a}' is not allowed for 'gaia-dev {' '.join(words)}'")
    if not low <= len(positional) <= high:
        raise AiError(f"'gaia-dev {' '.join(words)}' takes {low}-{high} arguments, got {len(positional)}")
    return args


def allowed(args):
    """True when args (a list of words) is a command `ai gaia-dev` runs."""
    try:
        validate(args)
    except AiError:
        return False
    return True


def gaia_root():
    """This workspace's Gaia checkout (the run's worktree under P4M_WORKSPACE), refusing one without gaia-dev."""
    require_matching_workspace()
    root = repo_dir("gaia")
    if not (root / gaiapack.GAIA_DEV).is_file():
        raise AiError(f"no gaia-dev at {root / gaiapack.GAIA_DEV}: ai gaia-dev runs on this workspace's gaia "
                      f"checkout (set P4M_WORKSPACE to the run folder that holds it)")
    return root


def cmd_gaia_dev(args, run=gaiapack.gaia_dev):
    argv = validate(args.args)
    root = gaia_root()
    print(f"checkout: {root}", flush=True)
    code, out = run(root, argv, timeout=TIMEOUT)
    if code is None:
        raise AiError(f"gaia-dev could not run: {out}")
    if out:
        print(out)
    return code
