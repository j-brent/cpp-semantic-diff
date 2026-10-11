"""`CallerOfChangedSignature`: each call into a function whose signature changed (ADR-040).

When F's signature changes, every call to F was written against the old one. The call may
still compile and do something else: `render(int)` becomes `render(double)`, and `render(2)`
now converts its argument silently. A reviewer needs each such call site, filed under the
signature change. The diff stage cannot report them: a paired function's call edges map
through the correspondence (`diff/edges.py`), so an unchanged call produces no edge event.

**Which calls.** A `calls` edge G -> F at head where F has a `NodeSignatureChanged`, and the
same call existed at base: a base `calls` edge whose two endpoints map, through the
correspondence, to G and F. A new caller, or a new call from an old caller, is left out. It
is already an `EdgeAdded`, and it was written against the new signature.

**What it says.** The subject is the head call edge, so the event's home is the caller G
(`rank.subject_home`) and its provenance is the call sites. The payload names caller and
callee, both signatures, and `caller_edited`: whether G has its own `NodeBodyChanged` or
`NodeSignatureChanged` in this diff. An unedited caller is the one the text diff never shows.

**A lower bound.** Only calls the model has as edges. Calls that depend on a template
parameter and calls from initializers or default arguments are missing (issue #13), and so
are calls through a function pointer. Their callers get no event, and nothing in
the output counts them.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from cpp_semantic_diff.diff.context import DiffContext, edge_hunks
from cpp_semantic_diff.match import Correspondence
from cpp_semantic_diff.model import (
    ChangeEvent,
    ChangeEventKind,
    Edge,
    EdgeKind,
    Model,
    NodeId,
    Side,
)

#: The kinds that say a caller was itself edited. A change to its attributes alone does not
#: touch the call site's text, so it does not count.
_EDITS: frozenset[ChangeEventKind] = frozenset(
    {ChangeEventKind.NODE_BODY_CHANGED, ChangeEventKind.NODE_SIGNATURE_CHANGED}
)


def callers_of_changed_signatures(
    events: Sequence[ChangeEvent],
    base: Model,
    head: Model,
    correspondence: Correspondence,
) -> Iterator[ChangeEvent]:
    """One `CallerOfChangedSignature` per call edge on both sides into a changed signature."""
    # Head ids: a matched node's event is about its head id (`diff/nodes.py`).
    changed = {
        NodeId(e.subject): e for e in events if e.kind is ChangeEventKind.NODE_SIGNATURE_CHANGED
    }
    if not changed:
        return
    edited = {NodeId(e.subject) for e in events if e.kind in _EDITS}

    # Base calls into a changed function, in head coordinates. Through the correspondence,
    # never by id: a name-paired function has one id at base and another at head (ADR-036).
    old_calls: dict[tuple[NodeId, NodeId], Edge] = {}
    for edge in base.edges:
        if edge.kind is not EdgeKind.CALLS:
            continue
        src = correspondence.head_for(edge.src)
        dst = correspondence.head_for(edge.dst)
        if src is not None and dst is not None and dst in changed:
            old_calls[(src, dst)] = edge

    ctx = DiffContext(base=base, head=head, correspondence=correspondence)
    for edge in head.edges:
        if edge.kind is not EdgeKind.CALLS:
            continue
        old = old_calls.get((edge.src, edge.dst))
        if old is None:
            continue
        cause = changed[edge.dst]
        yield ChangeEvent(
            kind=ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE,
            subject=edge.id,
            payload={
                "caller": edge.src,
                "callee": edge.dst,
                "old_signature": cause.payload["old_signature"],
                "new_signature": cause.payload["new_signature"],
                "caller_edited": edge.src in edited,
            },
            # The call sites on both sides, as `EdgeAttrChanged` carries them: the head site is
            # the one that must work with the new signature, the base site is where a reviewer
            # reading the old text finds the same call (§6.2).
            provenance=edge_hunks((Side.BASE, old), (Side.HEAD, edge)),
            confidence=ctx.confidence(
                *ctx.weakest(ctx.footing(Side.HEAD, edge.src), ctx.footing(Side.HEAD, edge.dst))
            ),
        )
