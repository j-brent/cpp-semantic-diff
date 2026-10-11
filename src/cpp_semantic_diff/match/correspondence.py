"""What the match stage produces: a bipartite, one-to-one mapping plus the residue.

ARCH-001 §5. The invariants live in the type rather than in the matcher, because a
correspondence that pairs one node twice does not fail loudly -- it produces a delta that is
merely wrong, and wrong with authority is the failure mode this project exists to argue
against.
"""

from __future__ import annotations

from functools import cached_property

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cpp_semantic_diff.match.declared import usr_file
from cpp_semantic_diff.model import MatchTier, NodeId, Side, Unpaired


class Match(BaseModel):
    """One base node paired with one head node, and the tier that paired them.

    The tier is not bookkeeping. It propagates into the confidence of every event derived
    from this pair (ARCH-001 §5, §11), so a match that cannot say how it was made cannot be
    reported honestly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base: NodeId
    head: NodeId
    tier: MatchTier


class Correspondence(BaseModel):
    """The pairing and the residue on each side.

    The residue is a first-class output, not leftovers. ARCH-001 §5: splits and merges are
    *not* forced into a match; they surface here as correlated add/remove residue. In v0 so do
    renames, on purpose (§13) -- the size of this residue is the measurement that decides
    whether tiers 2-4 are worth building.
    """

    model_config = ConfigDict(extra="forbid")

    matches: list[Match] = Field(default_factory=list)
    unmatched_base: list[NodeId] = Field(default_factory=list)
    unmatched_head: list[NodeId] = Field(default_factory=list)

    unpaired_base: dict[NodeId, Unpaired] = Field(
        default_factory=dict,
        description="Why each `unmatched_base` node is unmatched, keyed by exactly those "
        "nodes (ADR-034). No partner is not the same as a doubtful pairing, and only this "
        "stage saw both sides' leftovers, so it is the stage that says which is which.",
    )
    unpaired_head: dict[NodeId, Unpaired] = Field(
        default_factory=dict,
        description="The same for `unmatched_head`.",
    )

    tiers_run: list[MatchTier] = Field(
        default_factory=list,
        description="Which tiers actually ran, in order. Unmatched means different things "
        "depending on this: without rename detection (v0, ARCH-001 §13) it includes every "
        "rename, and output that does not say so overstates what the residue means.",
    )

    @model_validator(mode="after")
    def _enforce_bipartite_one_to_one(self) -> Correspondence:
        base_ids = [m.base for m in self.matches]
        head_ids = [m.head for m in self.matches]
        for side, ids in (("base", base_ids), ("head", head_ids)):
            if len(set(ids)) != len(ids):
                dupes = sorted({i for i in ids if ids.count(i) > 1})
                raise ValueError(
                    f"{side} node(s) {dupes} appear in more than one match; identity is "
                    f"one-to-one (ARCH-001 §5)"
                )
        # Residue means "no tier claimed this". A node that is both matched and residue
        # would be counted twice by the diff stage -- once as changed, once as added.
        if overlap := set(base_ids) & set(self.unmatched_base):
            raise ValueError(f"base node(s) {sorted(overlap)} are both matched and unmatched")
        if overlap := set(head_ids) & set(self.unmatched_head):
            raise ValueError(f"head node(s) {sorted(overlap)} are both matched and unmatched")
        # Every leftover has a reason and nothing else does. A missing reason would leave
        # rank to guess how doubtful an add or a remove is, and any guess is a default
        # presented as a measurement.
        for side, unmatched, reasons in (
            ("base", self.unmatched_base, self.unpaired_base),
            ("head", self.unmatched_head, self.unpaired_head),
        ):
            if missing := set(unmatched) - reasons.keys():
                raise ValueError(
                    f"unmatched {side} node(s) {sorted(missing)} have no unpaired reason"
                )
            if extra := reasons.keys() - set(unmatched):
                raise ValueError(
                    f"{side} node(s) {sorted(extra)} have an unpaired reason but are not unmatched"
                )
        return self

    @cached_property
    def by_base(self) -> dict[NodeId, Match]:
        return {m.base: m for m in self.matches}

    @cached_property
    def by_head(self) -> dict[NodeId, Match]:
        return {m.head: m for m in self.matches}

    def head_for(self, base: NodeId) -> NodeId | None:
        m = self.by_base.get(base)
        return m.head if m else None

    def base_for(self, head: NodeId) -> NodeId | None:
        m = self.by_head.get(head)
        return m.base if m else None

    def tier_for(self, base: NodeId) -> MatchTier | None:
        """The tier that matched this base node, or None if nothing did.

        None is exactly what `Confidence.tier` wants for an unmatched subject, so the diff
        stage never has to invent a tier for an add or a remove.
        """
        m = self.by_base.get(base)
        return m.tier if m else None

    def unpaired_for(self, side: Side, node_id: NodeId) -> Unpaired | None:
        """Why this node is unmatched, or None if it is not residue on that side."""
        return (self.unpaired_base if side is Side.BASE else self.unpaired_head).get(node_id)

    @property
    def internal_linkage_residue(self) -> int:
        """Residue nodes whose USR is file-scoped -- the ARCH-001 §5 hazard, counted.

        These are the internal-linkage and anonymous-namespace entities whose identity a
        file rename can break. Counting them keeps the hazard measured rather than assumed:
        if this is non-zero on a review that renamed no files, the tier-1 key is wrong.
        """
        return sum(
            1 for n in (*self.unmatched_base, *self.unmatched_head) if usr_file(n) is not None
        )
