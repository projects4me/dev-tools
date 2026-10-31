"""The premise: the incident types Agamemnon may handle, each with its allowed tool tiers, read from
agents/agamemnon/premise.json. An incident type not listed goes to the owner as before; widening the premise is a
config change."""

import json
from pathlib import Path

from ..config import TalosError

PREMISE_FILE = Path(__file__).resolve().parent.parent.parent / "agents" / "agamemnon" / "premise.json"
OTHER = "other"     # the classifier's answer when nothing matches: never in the premise


class Premise:
    def __init__(self, tiers, incidents):
        self.tiers = dict(tiers)            # tier name -> what it allows
        self.incidents = dict(incidents)    # incident type -> {"tiers": [...], "does": "..."}

    def handles(self, incident):
        return incident in self.incidents

    def tiers_for(self, incident):
        """The tool tiers allowed for the incident type; empty (nothing allowed) when it isn't in the premise."""
        return tuple(self.incidents.get(incident, {}).get("tiers", ()))

    def allows(self, incident, tier):
        return tier in self.tiers_for(incident)


def parse(data):
    """A Premise from the parsed JSON; TalosError when it names an unknown tier or the reserved type 'other'."""
    tiers, incidents = data.get("tiers") or {}, data.get("incidents") or {}
    if OTHER in incidents:
        raise TalosError(f"premise: '{OTHER}' is the type for what is not handled and cannot be listed")
    for name, spec in incidents.items():
        unknown = [t for t in spec.get("tiers", []) if t not in tiers]
        if unknown:
            raise TalosError(f"premise: incident '{name}' uses unknown tier(s) {', '.join(unknown)}")
    return Premise(tiers, incidents)


def load(path=PREMISE_FILE):
    try:
        return parse(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        raise TalosError(f"cannot read the Agamemnon premise {path}: {e}")
