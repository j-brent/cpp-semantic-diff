"""Node events (ARCH-001 §6.1): added, removed, moved, renamed, attr changed, body changed.

The residue of the correspondence is the add/remove half and needs no comparison: a base
node no tier claimed is removed, a head node no tier claimed is added. The matched half is
a field-by-field comparison of two nodes the match stage has already declared to be the
same entity, which is what makes each difference a *change* to one thing rather than the
disappearance of one thing and the appearance of another.

Two of the six kinds cannot fire in v0, and that is the point rather than a gap. Tier 1 keys on
the USR, which encodes the entity's name and its enclosing scope (ARCH-001 §5), so a node whose
name or parent differs from its match's does not exist -- the pair would never have been
matched. `NodeRenamed` and `NodeMoved` are therefore written against what a later tier will
produce and are unreachable until one runs. Softening that here -- pairing a removed node with
an added one because the names look related -- would move an identity decision out of the
matcher and destroy the measurement v0 exists to take (ARCH-001 §13).
"""

from __future__ import annotations

from collections.abc import Iterator

from cpp_semantic_diff.diff.attrs import changed_attrs
from cpp_semantic_diff.diff.context import DiffContext, node_hunks
from cpp_semantic_diff.match import Correspondence, Match
from cpp_semantic_diff.match.residue import containers as containers
from cpp_semantic_diff.model import (
    AttrValue,
    Event,
    EventKind,
    NodeId,
    Side,
)


def node_events(ctx: DiffContext) -> Iterator[Event]:
    corr = ctx.correspondence

    for node_id in corr.unmatched_base:
        node = ctx.base.nodes[node_id]
        yield Event(
            kind=EventKind.NODE_REMOVED,
            # Base coordinates: the subject does not exist at head, so head has no name for
            # it. Everything else in this stage is expressed in head coordinates, which is
            # what lets classify (§6.3) join an event about a node to events about its
            # edges without translating between the two sides again.
            subject=node_id,
            provenance=node_hunks((Side.BASE, node)),
            confidence=ctx.confidence(None, corr.residue_base[node_id]),
        )

    for node_id in corr.unmatched_head:
        node = ctx.head.nodes[node_id]
        yield Event(
            kind=EventKind.NODE_ADDED,
            subject=node_id,
            provenance=node_hunks((Side.HEAD, node)),
            confidence=ctx.confidence(None, corr.residue_head[node_id]),
        )

    base_parents = containers(ctx.base)
    head_parents = containers(ctx.head)
    for match in corr.matches:
        yield from _changes(ctx, match, base_parents, head_parents)


def _changes(
    ctx: DiffContext,
    match: Match,
    base_parents: dict[NodeId, NodeId],
    head_parents: dict[NodeId, NodeId],
) -> Iterator[Event]:
    base = ctx.base.nodes[match.base]
    head = ctx.head.nodes[match.head]

    def event(kind: EventKind, **payload: AttrValue) -> Event:
        return Event(
            kind=kind,
            subject=match.head,
            payload=dict(payload),
            provenance=node_hunks((Side.BASE, base), (Side.HEAD, head)),
            confidence=ctx.confidence(match.tier),
        )

    old_parent = base_parents.get(match.base)
    new_parent = head_parents.get(match.head)
    if _parent_changed(ctx.correspondence, old_parent, new_parent):
        yield event(EventKind.NODE_MOVED, old_parent=old_parent, new_parent=new_parent)

    if base.name != head.name:
        yield event(EventKind.NODE_RENAMED, old_name=base.name, new_name=head.name)

    # One event per attribute, each naming the attribute it is about (§6.1). The rule --
    # union of the key sets, compared with the type -- is `attrs.changed_attrs`, shared with
    # the edge half so that `NodeAttrChanged` and `EdgeAttrChanged` cannot come to mean
    # different things.
    for attr, old, new in changed_attrs(base.attrs, head.attrs):
        yield event(EventKind.NODE_ATTR_CHANGED, attr=attr, old=old, new=new)

    # ARCH-001 §4.1: `body_hash` is deliberately not an attribute, so a differing body is
    # never a `NodeAttrChanged`. An attribute change has a specific known meaning; a body
    # change only means "look closer", and the two carry different weight in rank (§7) and
    # render differently.
    both_have_bodies = base.body_hash is not None and head.body_hash is not None
    if both_have_bodies and base.body_hash != head.body_hash:
        yield event(EventKind.NODE_BODY_CHANGED)
    # A hash on one side and None on the other is *not* a body change and gets no event.
    # It says a definition appeared or disappeared while the declaration stayed -- a
    # `defines`/`declares` edge fact in ARCH-001 §4.2's vocabulary, and §13 keeps both
    # edge kinds out of v0, so the closed event set has no way to say it. Reporting it as
    # a body change would claim something inside changed when there was nothing inside
    # before. It is also indistinguishable, from the models alone, from a completeness
    # artifact: the TU holding the definition failing to parse on one side produces the
    # identical shape, and turning that into an event is §11's laundering of absence of
    # evidence into evidence of absence.


def _parent_changed(
    corr: Correspondence, old_parent: NodeId | None, new_parent: NodeId | None
) -> bool:
    """Whether the two parents are different *entities* -- not different ids.

    The comparison has to go through the correspondence for the same reason edges do: if
    the container was itself renamed, its base and head ids differ while the containment
    did not change, and comparing ids would report every member of a renamed class as
    moved.
    """
    if old_parent is None or new_parent is None:
        return (old_parent is None) != (new_parent is None)
    return corr.head_for(old_parent) != new_parent
