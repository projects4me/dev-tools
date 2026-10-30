"""The orchestrator's GitHub identity (Agent Agamemnon through dev-tools/agamemnon): who it is and where it can merge."""

import json

from .. import workspaces
from ..config import TalosError


def known_repos():
    """Every repo the tooling knows: name -> owner/name."""
    return dict(workspaces.ai_common().REPOS)


def worker_repos(worker):
    """The GitHub repos (name -> owner/name) of the worker's projects that agent PRs are opened in."""
    known = known_repos()
    return {repo: known[repo] for repo in sorted(set(worker.projects.values())) if repo in known}


def check_identity(worker, gh=None):
    """'<login>, write access to …' when the token works and can merge in every worker repo; TalosError naming the
    repos it can't write to otherwise. gh(*args) runs `gh` as Agamemnon and returns stdout."""
    gh = gh or agamemnon_gh
    login = gh("api", "user", "--jq", ".login").strip()
    repos = worker_repos(worker)
    without = [repo for repo, slug in repos.items()
               if not (json.loads(gh("api", f"repos/{slug}", "--jq", ".permissions") or "{}") or {}).get("push")]
    if without:
        raise TalosError(f"{login} has no write access to {', '.join(without)} (needs Contents and Pull requests "
                         f"read and write; see agamemnon/README.md)")
    return f"{login}, write access to {', '.join(repos)}"


def agamemnon_gh(*args):
    """`gh <args>` as Agent Agamemnon (dev-tools/agamemnon); stdout. TalosError when it fails."""
    common = workspaces.ai_common()
    try:
        return common.agamemnon(*args)
    except common.AiError as e:
        raise TalosError(str(e))
