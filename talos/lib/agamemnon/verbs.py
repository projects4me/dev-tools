"""The `aga` verbs and the guard every one of them passes before it runs.

Each verb belongs to a premise tier (what kind of action it is; premise.json grants tiers per incident type) and has a
visibility level (0 read, 1 reversible and private, 2 reversible and visible). Level 3 actions (force push, deleting
branches or workspaces with unpushed work, closing PRs, writing main/dev, changing config or limits) have no verb.
The guard is pure: the caller reads the incident, the issue and the journal and passes them in."""

from dataclasses import dataclass

from ..config import TalosError

READ, PRIVATE, VISIBLE = 0, 1, 2


class Refused(TalosError):
    """A verb's precondition is not met; nothing was done."""


@dataclass(frozen=True)
class Verb:
    name: str
    tier: str           # the premise tier that must be granted for the incident type
    level: int          # READ, PRIVATE or VISIBLE
    help: str

    @property
    def acts(self):
        """Whether the verb changes anything (and so is journaled and needs a reason)."""
        return self.level > READ


VERBS = {v.name: v for v in (
    Verb("status", "read", READ, "the issue's status, assignee, Talos state and PR"),
    Verb("logs", "read", READ, "the tail of the issue's latest run log"),
    Verb("diff", "read", READ, "the workspace's uncommitted changes"),
    Verb("mergecheck", "read", READ, "the merge gate on the issue's PR"),
    Verb("workspace", "read", READ, "the workspace's branch, HEAD and git status"),
    Verb("overlap", "read", READ, "files this issue's branch shares with the other in-flight issues"),
    Verb("backup", "restore", PRIVATE, "commit the workspace state to refs/agamemnon/backup/<issue>/<time>, push it"),
    Verb("restore", "restore", PRIVATE, "restore known generated files to HEAD (needs a backup in this incident)"),
    Verb("retry", "retry", PRIVATE, "give the issue back to the worker to run again"),
    Verb("rerun-ci", "retry", PRIVATE, "re-run the failed CI jobs on the PR's head"),
    Verb("wait-for", "wait", PRIVATE, "wait until another issue is done or a test slot is free"),
    Verb("hold-until-tomorrow", "wait", PRIVATE, "keep the issue until tomorrow, then give it back to the worker"),
    Verb("send", "handoff", PRIVATE, "send the issue to the worker with notes"),
    Verb("update-branch", "update", VISIBLE, "merge the base into the PR branch on GitHub (never a rebase)"),
    Verb("push-merge", "update", VISIBLE, "push a resolved merge from the workspace (fast-forward only)"),
    Verb("resolve-conflict", "resolve", VISIBLE, "merge the base into the branch in the workspace, resolve only "
         "additive conflicts, test, push as Odysseus, back to CodeRabbit (needs a backup in this incident)"),
    Verb("merge", "merge", VISIBLE, "merge the PR through the merge gate"),
)}


def check(verb_name, incident, premise, issue, milestone_prefix, actions_so_far, max_actions, why):
    """The Verb when it may run now; Refused otherwise. incident is the aga_incidents row (or None), issue the
    tracker's view of the incident's issue."""
    verb = VERBS.get(verb_name)
    if not verb:
        raise Refused(f"unknown verb '{verb_name}'")
    if not incident:
        raise Refused("no incident: aga acts only inside an incident Talos opened (AGA_INCIDENT)")
    if incident.get("closed_at"):
        raise Refused(f"incident {incident['id']} is closed")
    if not premise.allows(incident["type"], verb.tier):
        raise Refused(f"'{verb.name}' needs tier '{verb.tier}', which the premise does not grant for "
                      f"{incident['type']} (it grants: {', '.join(premise.tiers_for(incident['type'])) or 'nothing'})")
    milestone = str(issue.get("milestone") or "")
    if not milestone.lower().startswith(milestone_prefix.lower()):
        raise Refused(f"#{issue.get('number')} is in milestone '{milestone or '-'}', not an {milestone_prefix} one")
    if verb.acts:
        if not str(why or "").strip():
            raise Refused(f"'{verb.name}' changes something: give the reasoning with --why")
        if actions_so_far >= max_actions:
            raise Refused(f"incident {incident['id']} already took {actions_so_far} action(s), the most allowed "
                          f"({max_actions}); hand it to the owner")
    return verb
