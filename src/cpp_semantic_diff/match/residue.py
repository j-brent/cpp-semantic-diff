"""Why each leftover is a leftover (ADR-034).

The residue is every entity no tier paired, and in v0 that is every addition, every removal
and every rename (ARCH-001 §13). Rank wants to know which of those the tool may have got
wrong, and "no partner" does not say: a genuine addition has no partner by definition. So
the match stage, which is the only stage that knows what was left on both sides, records for
each leftover how much evidence there is that a partner was missed. That is a measurement of
doubt, not an identity decision -- nothing here pairs anything.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from cpp_semantic_diff.model import EdgeKind, Model, NodeId, NodeKind, Residue


def containers(model: Model) -> dict[NodeId, NodeId]:
    """Child -> containing node, read off the `contains` edges (ARCH-001 §4.2).

    The producer follows the *semantic* parent, so an out-of-line definition is contained
    by its class rather than by the namespace it was written in, and a node has at most one
    container. The IR does not forbid a second one; if one ever appears the move check in
    `diff/nodes.py` is comparing *a* parent rather than *the* parent, which is why this is a
    named function and not an inline comprehension.

    It lives here rather than in the diff stage because the match stage now needs it too,
    and diff already depends on match; one definition is what keeps the stages agreeing on
    what "parent" means.
    """
    parents: dict[NodeId, NodeId] = {}
    for edge in model.edges:
        if edge.kind is EdgeKind.CONTAINS:
            parents.setdefault(edge.dst, edge.src)
    return parents


#: Stands for "the parent is itself unmatched", so it can never equal a real parent on the
#: other side. Two leftovers under two unmatched parents may well be the same entity, but
#: saying so is a claim about the parents first, and nothing here makes identity claims.
_UNPAIRED_PARENT = NodeId("\x00unpaired-parent")

type _Slot = tuple[NodeKind, NodeId | None]


def residue_reasons(
    base: Model,
    head: Model,
    base_left: Collection[NodeId],
    head_left: Collection[NodeId],
    head_for: Mapping[NodeId, NodeId],
    ambiguous_base: Collection[NodeId],
    ambiguous_head: Collection[NodeId],
) -> tuple[dict[NodeId, Residue], dict[NodeId, Residue]]:
    """A `Residue` for every leftover on each side.

    `AMBIGUOUS` comes from the tiers, which are the only ones who know they declined a
    choice. Otherwise a leftover is a `CANDIDATE` when the other side's leftovers hold an
    entity of the same kind under the same parent -- ARCH-001 §5's tier-4 candidate set,
    counted and not scored -- and `NONE` when they hold none. "Same parent" is asked in head
    coordinates through the matches made so far: a base parent means its head partner, and
    top level means top level.
    """
    base_parents = containers(base)
    head_parents = containers(head)

    def base_slot(node_id: NodeId) -> _Slot:
        parent = base_parents.get(node_id)
        mapped = None if parent is None else head_for.get(parent, _UNPAIRED_PARENT)
        return (base.nodes[node_id].kind, mapped)

    def head_slot(node_id: NodeId) -> _Slot:
        return (head.nodes[node_id].kind, head_parents.get(node_id))

    base_slots = {base_slot(n) for n in base_left}
    head_slots = {head_slot(n) for n in head_left}

    def reason(slot: _Slot, ambiguous: bool, other: set[_Slot]) -> Residue:
        if ambiguous:
            return Residue.AMBIGUOUS
        # An unpaired parent never matches, so it is never a candidate -- see above.
        return Residue.CANDIDATE if slot[1] != _UNPAIRED_PARENT and slot in other else Residue.NONE

    amb_b, amb_h = set(ambiguous_base), set(ambiguous_head)
    return (
        {n: reason(base_slot(n), n in amb_b, head_slots) for n in base_left},
        {n: reason(head_slot(n), n in amb_h, base_slots) for n in head_left},
    )
