"""The lookups the cause rules need, built once from the two models (ARCH-001 §6.3).

ARCH-001 §3's stage table gives classify a single input -- the `ChangeEvent` list -- and that
understates it. Every rule in §6.3 is a statement about the *graph*: what contained what,
which entities an edge related, whether an entity survived. None of those questions can be
answered from a list of events, and the ones that look answerable are traps. Containment
could in principle be recovered by splitting an `EdgeRemoved` subject on `|`, but `Edge.id`
is `src|kind|dst` over USRs, so a split is a guess about a string where the model has the
fact; and a *surviving* container emits no event at all, so half the relation is simply not
in the list. The stage stays a pure function (§3) -- it just needs three inputs rather than
one.

Nothing here is state. It is the same shape as `diff/context.py` and for the same reason:
the cross-cutting questions every rule asks get one answer, in one place, rather than one
per rule.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from cpp_semantic_diff.match import Correspondence, containers
from cpp_semantic_diff.model import Edge, EdgeId, EdgeKind, Model, NodeId


@dataclass(frozen=True)
class ClassifyContext:
    base: Model
    head: Model
    correspondence: Correspondence

    #: Child -> container, on each side. Read from the model the event's subject is
    #: expressed in: a `NodeRemoved` subject is a base id (`diff/nodes.py` says so in
    #: place), a `NodeAdded` subject is a head id, and using the wrong side's map would
    #: silently attribute nothing at all rather than fail.
    base_parents: Mapping[NodeId, NodeId]
    head_parents: Mapping[NodeId, NodeId]

    #: Edge id -> its two endpoints, which is how an edge event's subject gets back to the
    #: entities it relates. See `_endpoints` on why this is read off the model.
    base_endpoints: Mapping[EdgeId, tuple[NodeId, NodeId]]
    head_endpoints: Mapping[EdgeId, tuple[NodeId, NodeId]]

    #: Caller -> callees, head side. Used only by the dormant `SignatureIncompatible` rule
    #: (§6.3), and built anyway so that rule is a table entry rather than a rewrite.
    head_callees: Mapping[NodeId, tuple[NodeId, ...]]

    #: Edge id -> the edge, base side. An edge event's subject names an edge, and a rule that
    #: needs its *kind* must not recover it by splitting the id: `Edge.id` is `src|kind|dst`
    #: over USRs, and a USR is an opaque clang string that may contain anything.
    base_edges: Mapping[EdgeId, Edge]

    #: Source -> its outgoing edges, head side. What lets a rule ask "does this caller now
    #: call something else instead", which is a question about the head graph and not about
    #: any event.
    head_out_edges: Mapping[NodeId, tuple[Edge, ...]]

    @classmethod
    def build(cls, base: Model, head: Model, correspondence: Correspondence) -> ClassifyContext:
        callees: dict[NodeId, set[NodeId]] = defaultdict(set)
        out: dict[NodeId, list[Edge]] = defaultdict(list)
        for edge in head.edges:
            if edge.kind is EdgeKind.CALLS:
                callees[edge.src].add(edge.dst)
            out[edge.src].append(edge)
        return cls(
            base=base,
            head=head,
            correspondence=correspondence,
            # The one definition match and diff share, not re-derived. `contains` follows the
            # *semantic* parent (`match/unpaired.py`), so an out-of-line definition belongs to
            # its class; a second copy of that rule here would drift from the one the diff
            # stage compares parents with, and the two stages would disagree about what
            # "contained" means while both looked right.
            base_parents=containers(base),
            head_parents=containers(head),
            base_endpoints=_endpoints(base),
            head_endpoints=_endpoints(head),
            head_callees={src: tuple(sorted(dsts)) for src, dsts in callees.items()},
            base_edges={edge.id: edge for edge in base.edges},
            # Sorted by id so a rule yielding several candidates yields them in an order
            # that does not depend on model construction (§3).
            head_out_edges={
                src: tuple(sorted(edges, key=lambda e: e.id)) for src, edges in out.items()
            },
        )

    def removed(self, base_id: NodeId) -> bool:
        """No head node corresponds to this base node -- exactly what `NodeRemoved` means.

        Asked of the correspondence, never of the id. ARCH-001 §5 makes "what does this base
        id mean at head" the match stage's question, and `diff/edges.py` already refuses to
        answer it a second time; a cause rule that compared ids would, the moment tier 2
        lands, report every member of a renamed class as having lost its container.
        """
        return self.correspondence.head_for(base_id) is None

    def added(self, head_id: NodeId) -> bool:
        """No base node corresponds to this head node -- what `NodeAdded` means."""
        return self.correspondence.base_for(head_id) is None


def _endpoints(model: Model) -> dict[EdgeId, tuple[NodeId, NodeId]]:
    """Edge id -> (src, dst), read off the model rather than parsed back out of the id.

    `Edge.id` is `src|kind|dst` (ARCH-001 §4.2) over USRs, and a USR is an opaque string
    from clang. Splitting one here would be a guess about a separator in a place where the
    model holds the fact, and the failure mode is a *wrong* cause link rather than a missing
    one -- the worse of the two, since §6.3 would rather show an event than hide it under
    the wrong parent.
    """
    return {edge.id: (edge.src, edge.dst) for edge in model.edges}
