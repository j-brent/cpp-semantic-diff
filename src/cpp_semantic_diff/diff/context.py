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
    NodeId,
    Residue,
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


#: Why an endpoint is unmatched, least doubtful first (ADR-034). An edge event resting on two
#: unmatched endpoints is as doubtful as the more doubtful of them, which is a §11 question
#: for the same reason `TIER_ORDER` is. `rank.weights.UNCERTAINTY_BY_RESIDUE` must rise along
#: it; `tests/test_rank.py` holds the two together.
RESIDUE_ORDER: tuple[Residue, ...] = (Residue.NONE, Residue.CANDIDATE, Residue.AMBIGUOUS)

#: What an event rests on for one entity: its match tier, or why it has none.
type Footing = tuple[MatchTier | None, Residue | None]


@dataclass(frozen=True)
class DiffContext:
    base: Model
    head: Model
    correspondence: Correspondence

    def confidence(self, tier: MatchTier | None, residue: Residue | None = None) -> Confidence:
        """ARCH-001 §11: match tier plus the completeness of *both* models.

        Completeness is read from the models rather than passed in, because an event that
        could be constructed without it would let a caller claim, by omission, that nothing
        was missing. `residue` is required exactly when `tier` is None (`Confidence`).
        """
        return Confidence.derive(tier, self.base.completeness, self.head.completeness, residue)

    def footing(self, side: Side, node_id: NodeId) -> Footing:
        """The tier that matched this node, or why nothing did."""
        corr = self.correspondence
        index = corr.by_base if side is Side.BASE else corr.by_head
        if (m := index.get(node_id)) is not None:
            return (m.tier, None)
        # A node the correspondence says nothing about is not in the model on that side --
        # an edge endpoint the producer kept without a node. There is no evidence about its
        # partner either way, which is the most doubt there is.
        return (None, corr.residue_for(side, node_id) or Residue.AMBIGUOUS)

    def weakest(self, *footings: Footing) -> Footing:
        """The least confident of several footings: an edge event rests on both endpoints.

        If any endpoint was never matched there is no tier to report -- not a default one,
        and not the tier of the endpoint that *was* matched -- so `tier` is None, which is
        what `Confidence.tier` means by unmatched residue. The residue is then the most
        doubtful of the unmatched endpoints' (ADR-034), so an edge between a matched entity
        and a plain addition is no more doubtful than the addition.

        One thing is lost, knowingly: with `tier` None, the matched endpoint's tier is not
        carried, so an edge whose other endpoint is a tier-4 guess reads as doubtful as its
        residue alone. It cannot arise in v0, where only tier 1 runs and carries no doubt;
        ADR-034 records it for when tiers 2-4 land.
        """
        residues = [r for t, r in footings if t is None and r is not None]
        if residues:
            return (None, max(residues, key=RESIDUE_ORDER.index))
        tiers = [t for t, _ in footings if t is not None]
        return (max(tiers, key=TIER_ORDER.index), None)


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
