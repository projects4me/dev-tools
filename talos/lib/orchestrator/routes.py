"""Where a worker hands an issue whose PR is open: the orchestrator that merges its milestone itself, or
None for the reviewer. Shared by the worker's dispatcher and Agamemnon's own pushes (agamemnon/wiring.py)."""

from ..config import TalosError, log
from ..tracker import Tracker
from . import github
from . import policy as orchestration
from . import state as store
from .merge import Merger
from .milestones import live


class MergeRoute:
    """Where the worker sends an issue whose PR is open when an orchestrator merges its milestone itself:
    route(issue) -> (the orchestrator's Gaia user id, its name), or None for the reviewer (other milestones, the
    orchestrator paused, or its Gaia user not readable yet: that is retried on the next issue)."""

    def __init__(self, agent, user_id, is_paused, find_pr=None, milestones=None):
        self.agent = agent
        self.find_pr = find_pr          # find_pr(repo, issue) -> the issue's PR (merge.Merger.find_pr)
        self.user_id = user_id          # user_id() -> the orchestrator's Gaia user id (TalosError when unreadable)
        self.is_paused = is_paused      # is_paused() -> True while the orchestrator is paused
        # milestones() -> its auto-merge milestones now (milestones.live: reloaded when the config changes)
        self.milestones = milestones or (lambda: agent.orchestration.auto_merge_milestones)
        self._id = None

    def __call__(self, issue):
        if not self._id:
            try:
                self._id = self.user_id()
            except TalosError as e:
                log(f"#{issue.get('number')} goes to the reviewer: {self.agent.display_name}'s Gaia user is not "
                    f"readable ({e})", self.agent.name)
                return None
        target = orchestration.merge_target(issue, self.milestones(), self._id, self.is_paused())
        return (target, self.agent.display_name) if target else None


def merge_routes(agents):
    """{worker name: MergeRoute} for every orchestrator: one with no auto-merge milestones yet routes nothing until a
    milestone is added to its config (no restart needed)."""
    return {a.orchestration.worker: MergeRoute(a, Tracker(a).user_id, lambda a=a: bool(store.get(a.name).get("paused")),
                                               Merger(Tracker(a).child_env, github.agamemnon_gh).find_pr,
                                               milestones=live(a))
            for a in agents if a.kind == "orchestrator"}
