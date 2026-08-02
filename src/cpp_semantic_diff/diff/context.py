"""The three inputs of the diff stage, and the derivations every event needs from them.

ARCH-001 §3 makes each stage a pure function of the previous stage's output, so this holds
inputs and never state. It exists so that the node half and the edge half of the delta
answer the two cross-cutting questions -- how confident is this event (§11), and where in
source did it come from (§6.2) -- the same way, rather than each growing its own answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from cpp_semantic_diff.match import Correspondence
from cpp_semantic_diff.model import (
    Confidence,
    Edge,
    Hunk,
    MatchTier,
    Model,
    Node,
    Side,
)

#: ARCH-001 §5's tier table, most confident first. The order lives here rather than on
#: `MatchTier` because the only question that needs it is a §11 question this stage owns:
#: an event resting on two matches is no more confident than the weaker of them.
TIER_ORDER: tuple[MatchTier, ...] = (
    MatchTier.DECLARED,
    MatchTier.STRUCTURAL,
    MatchTier.POSITIONAL,
    MatchTier.SIMILARITY,
)


@dataclass(frozen=True)
class DiffContext:
    base: Model
    head: Model
    correspondence: Correspondence

    def confidence(self, tier: MatchTier | None) -> Confidence:
        """ARCH-001 §11: match tier plus the completeness of *both* models.

        Completeness is read from the models rather than passed in, because an event that
        could be constructed without it would let a caller claim, by omission, that nothing
        was missing.
        """
        return Confidence.derive(tier, self.base.completeness, self.head.completeness)

    def weakest(self, *tiers: MatchTier | None) -> MatchTier | None:
        """The least confident of several tiers; None if any of them is None.

        An edge event rests on the match of each endpoint. If one endpoint was never
        matched there is no tier to report -- not a default one -- and None is exactly what
        `Confidence.tier` means by unmatched residue.
        """
        if any(t is None for t in tiers):
            return None
        return max(tiers, key=lambda t: TIER_ORDER.index(t))  # type: ignore[arg-type]


def node_hunks(*sides: tuple[Side, Node]) -> list[Hunk]:
    """Provenance for a node event, from whichever sides the event has (ARCH-001 §6.2).

    `Location` is provenance and never identity (§4.1) -- a node that only moved file
    produces no event at all -- but it is precisely what a reviewer needs to get from an
    event to the lines, so an event about two sides carries a hunk for each.
    """
    return [Hunk(side=side, location=n.location) for side, n in sides if n.location is not None]


def edge_hunks(*sides: tuple[Side, Edge]) -> list[Hunk]:
    """Provenance for an edge event: every site that produced the edge.

    An edge carries a list of locations because a function calling another twice is one
    edge with two call sites (§4.2). All of them are hunks: the reverse direction of §6.2
    -- from a line in the ordinary diff to the events it caused -- is only complete if
    every line that produced the edge points back at it.
    """
    return [Hunk(side=side, location=loc) for side, e in sides for loc in e.locations]
