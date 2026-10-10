"""Edge events (ARCH-001 §6.1): added, removed, kind changed, attr changed -- and why not
retargeted.

**An edge has no identity of its own.** It is its two endpoints and its kind: the IR says
so directly, since `Edge.id` is `src|kind|dst`. Its endpoints are node ids, and what a node
id at base means at head is the one question the match stage exists to answer. So the first
thing this module does is rewrite every base edge into head coordinates through the
correspondence. Comparing raw ids instead would report every edge on every renamed node as
removed and re-added -- and once tiers 2-4 land, a single renamed function would produce
hundreds of edge events where the correct answer is none.

That has a consequence worth stating, because it contradicts a rule ARCH-001 §6.3's first draft gave
as an example: "`NodeRenamed(F)` causes every `EdgeRetargeted` whose endpoint is F". That rule
presumes edges keyed on raw ids, where a rename really does move 200 edges. Under
correspondence-mapped endpoints those 200 events never exist rather than being collapsed
under a cause, which is the better answer to the same problem -- but the cause rule in §6.3
is then describing something this stage cannot emit. §6.3 now marks it unreachable.

**`EdgeRetargeted` is not emitted, and not because it is unimplemented.** It would mean:
this base edge and that head edge are the same edge, whose destination changed from B to C.
With no identity of its own, an edge can only be the same edge if its endpoints are the
same entities -- and the match stage, which owns that judgement and is one-to-one (§5), has
already said B corresponds to B'. Claiming B "became" C is a node-identity claim the
matcher declined to make, made here instead, on weaker evidence. It surfaces as
`EdgeRemoved` plus `EdgeAdded`, which is the same information without the invented
correspondence. Consistent with `V0_KINDS`, which excludes it.

`EdgeKindChanged` *is* emitted, and the asymmetry is the point: the kind is state, not
identity. Between two entities the matcher has already paired, a relation that was `calls`
and is now `overrides` needs no new identity claim -- only the check that the pairing is
unambiguous, which is the same collision rule tier 1 applies to its own key.

`EdgeAttrChanged` is the same argument with the identity question removed entirely. Where
`EdgeKindChanged` pairs two *residual* edges and has to justify the pairing, this fires on
an edge whose whole key -- both endpoints and the kind -- is equal on the two sides, and
reports the one thing left: `Edge.attrs` (§4.2). Without it, `access` going public ->
private on an `inherits` edge between two unchanged classes is invisible in the delta,
because every other edge event is about an edge appearing, disappearing or changing kind,
and this edge does none of those. §6.1's own rule -- new signals become new event kinds --
is what makes that a kind rather than a note.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

from cpp_semantic_diff.diff.attrs import changed_attrs
from cpp_semantic_diff.diff.context import DiffContext, edge_hunks
from cpp_semantic_diff.model import (
    Edge,
    EdgeKind,
    Event,
    EventKind,
    MatchTier,
    NodeId,
    Side,
)

#: An edge in head coordinates. Deliberately the whole of `Edge.id` -- see the module
#: docstring on why nothing weaker is a defensible edge identity.
type EdgeKey = tuple[NodeId, EdgeKind, NodeId]


def edge_events(ctx: DiffContext) -> Iterator[Event]:
    corr = ctx.correspondence
    base_side: dict[EdgeKey, Edge] = {}
    head_side: dict[EdgeKey, Edge] = {}

    for edge in ctx.base.edges:
        src = corr.head_for(edge.src)
        dst = corr.head_for(edge.dst)
        if src is None or dst is None:
            # An endpoint no tier claimed. The edge cannot be expressed at head because one
            # of the things it relates is not there, so it is removed -- and it is emitted
            # rather than folded into the `NodeRemoved` for that endpoint. ARCH-001 §6.3:
            # the event set is complete and collapse happens at render; a consequence is
            # linked to its cause by classify, never dropped here.
            yield _removed(ctx, edge)
            continue
        # One-to-one matching (§5) means two base edges can never translate onto one key.
        base_side[(src, edge.kind, dst)] = edge

    for edge in ctx.head.edges:
        if corr.base_for(edge.src) is None or corr.base_for(edge.dst) is None:
            yield _added(ctx, edge)
            continue
        head_side[(edge.src, edge.kind, edge.dst)] = edge

    # Present on both sides in head coordinates: the same relation between the same two
    # entities. The edge itself produces no event -- including when the two differ in
    # `locations`, which is provenance (§4.2) -- but its *attributes* can still differ, and
    # that is a change with nowhere else to surface: the edge matches, so neither add nor
    # remove fires, and the kind is equal, so `EdgeKindChanged` does not either.
    for key, head_edge in head_side.items():
        base_edge = base_side.get(key)
        if base_edge is not None:
            yield from _attr_changes(ctx, base_edge, head_edge)

    base_residue = {k: e for k, e in base_side.items() if k not in head_side}
    head_residue = {k: e for k, e in head_side.items() if k not in base_side}

    for base_key, head_key in _unambiguous_pairs(base_residue, head_residue):
        old = base_residue.pop(base_key)
        new = head_residue.pop(head_key)
        yield Event(
            kind=EventKind.EDGE_KIND_CHANGED,
            subject=new.id,
            payload={"old_kind": old.kind.value, "new_kind": new.kind.value},
            provenance=edge_hunks((Side.BASE, old), (Side.HEAD, new)),
            confidence=ctx.confidence(_tier(ctx, new, Side.HEAD)),
        )

    for edge in base_residue.values():
        yield _removed(ctx, edge)
    for edge in head_residue.values():
        yield _added(ctx, edge)


def _attr_changes(ctx: DiffContext, old: Edge, new: Edge) -> Iterator[Event]:
    """`EdgeAttrChanged` for an edge that is on both sides with different attributes.

    The one edge event that needs no identity reasoning at all: the key matched, so the
    matcher has already paired both endpoints and the kind is equal. What is left is
    diffable state on a relation both models agree exists -- §4.2's `attrs`, which the
    producer fills with `access` on `inherits` and `virtual` on `calls`. A base class going
    public -> private between two otherwise unchanged classes is a breaking change in C++,
    and it lives *only* here: nothing about either class changes, and the `inherits` edge
    itself is present on both sides.

    The comparison is `attrs.changed_attrs`, the same rule `diff/nodes.py` applies to
    `Node.attrs` -- union of the key sets, compared with the type. Sharing it is not tidying:
    an edge attribute that disappears, and a producer that flips `virtual` from `False` to
    `0`, are both changes that a second, subtly different implementation would drop in
    silence.

    Subject and provenance follow `EdgeKindChanged`: head coordinates for the subject, both
    sides' call sites for the hunks, since a reviewer needs to reach the old attribute as
    well as the new one (§6.2).
    """
    for attr, old_value, new_value in changed_attrs(old.attrs, new.attrs):
        yield Event(
            kind=EventKind.EDGE_ATTR_CHANGED,
            subject=new.id,
            payload={"attr": attr, "old": old_value, "new": new_value},
            provenance=edge_hunks((Side.BASE, old), (Side.HEAD, new)),
            confidence=ctx.confidence(_tier(ctx, new, Side.HEAD)),
        )


def _unambiguous_pairs(
    base_residue: dict[EdgeKey, Edge], head_residue: dict[EdgeKey, Edge]
) -> list[tuple[EdgeKey, EdgeKey]]:
    """Residual edges that pair up on their endpoints alone, with nothing else in the way.

    Keyed on (src, dst) in head coordinates. A pair claimed by two residual edges on either
    side is left alone -- the same rule tier 1 applies to a colliding USR
    (`match.declared._index`): a key two entities share identifies neither, and picking one
    is a coin flip presented as a fact. Those fall through to remove plus add.

    The residues are disjoint by construction, so a pair that survives here differs in the
    only component left: the kind.
    """
    by_pair: dict[tuple[NodeId, NodeId], tuple[list[EdgeKey], list[EdgeKey]]] = defaultdict(
        lambda: ([], [])
    )
    for key in base_residue:
        by_pair[(key[0], key[2])][0].append(key)
    for key in head_residue:
        by_pair[(key[0], key[2])][1].append(key)

    return [(old[0], new[0]) for old, new in by_pair.values() if len(old) == 1 and len(new) == 1]


def _removed(ctx: DiffContext, edge: Edge) -> Event:
    # Base coordinates: the edge does not exist at head. Same rule as `NodeRemoved`.
    return Event(
        kind=EventKind.EDGE_REMOVED,
        subject=edge.id,
        provenance=edge_hunks((Side.BASE, edge)),
        confidence=ctx.confidence(_tier(ctx, edge, Side.BASE)),
    )


def _added(ctx: DiffContext, edge: Edge) -> Event:
    return Event(
        kind=EventKind.EDGE_ADDED,
        subject=edge.id,
        provenance=edge_hunks((Side.HEAD, edge)),
        confidence=ctx.confidence(_tier(ctx, edge, Side.HEAD)),
    )


def _tier(ctx: DiffContext, edge: Edge, side: Side) -> MatchTier | None:
    """The tier an edge event rests on: the weaker of its two endpoints' matches.

    An edge is a claim about two entities, so it is no more confident than the less
    confident of the two identifications -- and if either endpoint was never matched, there
    is no tier, which is what makes an add or a remove report itself as untiered rather
    than borrowing the certainty of the endpoint that *was* matched (§11).
    """
    corr = ctx.correspondence
    index = corr.by_base if side is Side.BASE else corr.by_head
    return ctx.weakest(
        *(m.tier if (m := index.get(node_id)) else None for node_id in (edge.src, edge.dst))
    )
