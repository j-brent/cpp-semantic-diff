"""The cause rule table (ARCH-001 §6.3).

§6.3 is emphatic that consequences are linked to causes by **an explicit rule table, not
inference**, so this is a table: a tuple of `CauseRule`, each naming the kind it explains,
the kind that explains it, and a small locator saying which subject to look for. It is meant
to be read side by side with §6.3 and agreed or disagreed with. Nothing outside this module
decides what causes what.

**Where this table and §6.3's example table differ, and why.** §6.3 was written before the
diff stage existed, and two of its decisions moved the ground under it:

1. §6.3's "NodeRenamed(F) causes every EdgeRetargeted whose endpoint is F" describes
   something this pipeline cannot produce, twice over. `EdgeRetargeted` is refused by design
   (`diff/edges.py`): an edge has no identity of its own, so claiming one "was retargeted"
   re-makes a node-identity claim the one-to-one matcher declined. And because base edges
   are rewritten into head coordinates *through the correspondence* before comparison, a
   rename produces no edge events at all -- the 200 consequences §6.3 wants collapsed never
   exist. That is a better answer to the same problem, but it leaves the rule with nothing
   to fire on, and no later tier changes that: tier 2 makes `NodeRenamed` reachable without
   making a single edge event appear beside it. The rule is kept, marked
   `RuleStatus.UNREACHABLE`, because deleting it would lose the argument.

2. §6.3's "NodeAttrChanged(signature) causes SignatureIncompatible on callers" is a
   correct rule about an event this pipeline does not yet emit. `SignatureIncompatible` is
   derived, §12 puts derived events out of v0, and `events.py` records their payloads as
   undesigned. **Classify links causes among events that already exist; it never synthesizes
   one.** Synthesizing here would move the derived-event machinery (which needs both models
   plus graph algorithms) into the stage whose whole contract is relabeling, and would put
   an event in the JSON that no stage claims to own.

Two rules §6.3 does not name are the ones that carry v0's actual cascades, and both were
anticipated by the diff stage in comments rather than by the architecture: an edge event on
an endpoint that was added or removed. `diff/edges.py` emits those "rather than folding
[them] into the `NodeRemoved` for that endpoint ... a consequence is linked to its cause by
classify, never dropped here". Without them, removing one class leaves every containment and
call edge it touched as an unexplained top-level event.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from cpp_semantic_diff.classify.context import ClassifyContext
from cpp_semantic_diff.model import AnyId, AttrValue, EdgeId, Event, EventKind, NodeId


class RuleStatus(StrEnum):
    """Whether a rule can fire on the events this pipeline emits today.

    Recorded on the rule rather than left to a comment, because "correct but cannot fire" is
    exactly the state that rots: the next reader either deletes a rule that a later tier
    needs, or trusts a cascade that is not being collapsed. Both are silent.
    """

    ACTIVE = "active"
    #: Correct, and will fire once a stage that is out of v0 scope (ARCH-001 §12) lands.
    DORMANT = "dormant"
    #: Correct against ARCH-001 §6.3 as written, but describing events the pipeline refuses
    #: to emit by design rather than by omission. No later stage makes it reachable.
    UNREACHABLE = "unreachable"


#: A locator: given the context and one *consequence* event, the subjects a cause might have,
#: **most plausible first**. Returning several is how a rule declares that an event has more
#: than one candidate cause; the classifier takes the first that exists, so the order here is
#: the whole of the tie-break and must be principled rather than incidental.
type Locator = Callable[[ClassifyContext, Event], Iterable[AnyId]]


@dataclass(frozen=True)
class CauseRule:
    """One line of ARCH-001 §6.3's table.

    Read it as: *a `consequence` event is caused by the `cause` event on whichever of the
    subjects `subjects` names -- the first of them that turns out to have one.*
    """

    name: str
    cause: EventKind
    consequence: EventKind
    subjects: Locator
    status: RuleStatus
    arch: str | None
    note: str
    cause_payload: Mapping[str, AttrValue] = field(default_factory=dict)
    """Payload keys the cause event must carry. This is what lets §6.3's
    `NodeAttrChanged(signature)` be a table entry rather than a special case in the driver:
    the parenthesis in §6.3's prose is a payload filter, and it is spelled as one."""


# --------------------------------------------------------------------------------------
# Locators. Each is one relation from the model, expressed in the coordinates the events it
# joins are already in -- base for removals, head for additions -- so that no rule has to
# translate between the two sides a second time (`diff/nodes.py` on why removals stay in
# base coordinates).


def _removed_container(ctx: ClassifyContext, event: Event) -> Iterator[NodeId]:
    """The container of a removed node, if the container was itself removed.

    Only the *immediate* container. Depth is handled by the chain -- a method links to its
    class, the class to its namespace -- rather than by walking to the outermost removed
    ancestor, because the chain is what render (§6.3) needs to nest the tree, and the
    outermost link would flatten three levels of structure into one.

    The `removed` test is what stops a member deleted from a *surviving* class being blamed
    on anything: that is a real, independently interesting change, and §6.3 would rather
    show it than hide it under a container that is still there.
    """
    parent = ctx.base_parents.get(NodeId(event.subject))
    if parent is not None and ctx.removed(parent):
        yield parent


def _added_container(ctx: ClassifyContext, event: Event) -> Iterator[NodeId]:
    """The mirror of `_removed_container`, on the head side.

    Not in §6.3, which names only the removal direction. The relation is the same one: a new
    class arrives with its methods, and the methods are consequences of the class in exactly
    the sense §6.3 means. Omitting it would make a large addition read as N unexplained
    top-level events while the identical removal read as one.
    """
    parent = ctx.head_parents.get(NodeId(event.subject))
    if parent is not None and ctx.added(parent):
        yield parent


def _removed_endpoints(ctx: ClassifyContext, event: Event) -> Iterator[NodeId]:
    """The endpoints of a removed edge that did not survive -- **source first**.

    An edge with both endpoints removed genuinely has two causes, and §6.3 gives one
    `caused_by`. Source wins, and not arbitrarily: an edge is written in its source's text.
    The call site is in the caller's body, the base clause is in the derived class, the
    containment is the container's braces -- and `edge_hunks` (`diff/context.py`) carries
    exactly those lines as the event's provenance. So the source's disappearance is the
    change that deleted the lines this event points at, which makes it the cause a reviewer
    following provenance would arrive at anyway.

    An edge between two nodes that both survived yields nothing and keeps `caused_by` None:
    a call that stopped happening between two entities that are both still there is the
    change itself, not a consequence of one.
    """
    for node_id in ctx.base_endpoints.get(EdgeId(event.subject), ()):
        if ctx.removed(node_id):
            yield node_id


def _added_endpoints(ctx: ClassifyContext, event: Event) -> Iterator[NodeId]:
    """The mirror of `_removed_endpoints`, on the head side. Source first, same reason."""
    for node_id in ctx.head_endpoints.get(EdgeId(event.subject), ()):
        if ctx.added(node_id):
            yield node_id


def _renamed_endpoint_of_retarget(ctx: ClassifyContext, event: Event) -> Iterator[NodeId]:
    """§6.3's "whose endpoint is F", in head coordinates. Cannot fire -- see the module docstring.

    Written out anyway so that the rule is a rule. `NodeRenamed`'s subject is the head id of
    the matched pair (`diff/nodes.py`), so every candidate has to arrive as a head id: the
    edge's own endpoints already are, `new_dst` is, and `old_dst` is a base id and goes
    through the correspondence -- the same translation every other cross-side comparison in
    this pipeline makes.
    """
    seen: set[NodeId] = set()
    old_dst = event.payload.get("old_dst")
    new_dst = event.payload.get("new_dst")
    candidates: tuple[NodeId | None, ...] = (
        *ctx.head_endpoints.get(EdgeId(event.subject), ()),
        NodeId(new_dst) if isinstance(new_dst, str) else None,
        ctx.correspondence.head_for(NodeId(old_dst)) if isinstance(old_dst, str) else None,
    )
    for candidate in candidates:
        if candidate is not None and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _callee_of(ctx: ClassifyContext, event: Event) -> Iterator[NodeId]:
    """The callees of the caller a `SignatureIncompatible` is about (§6.3), head side.

    §6.3 says the consequence lands "on callers", so the consequence's subject is the caller
    and the cause is on the callee whose signature moved. Sorted, so that a caller of two
    changed callees gets a stable answer rather than one that depends on edge order -- but
    that stability is a floor, not an answer. When the payload of `SignatureIncompatible` is
    designed (`events.py` records it as undesigned, §12), it should name the callee, and
    this locator should read it from there; the ambiguity is an artifact of the payload
    being empty, not a property of the relation.
    """
    yield from ctx.head_callees.get(NodeId(event.subject), ())


# --------------------------------------------------------------------------------------
# The table. Ordered; the first rule that finds a cause wins, which matters only if two
# rules ever share a consequence kind -- none do today, and the ordering is the documented
# tie-break if one ever does.

CAUSE_RULES: tuple[CauseRule, ...] = (
    CauseRule(
        name="contained-removal",
        arch="§6.3: NodeRemoved(C) causes NodeRemoved for everything C contained",
        cause=EventKind.NODE_REMOVED,
        consequence=EventKind.NODE_REMOVED,
        subjects=_removed_container,
        status=RuleStatus.ACTIVE,
        note="Containment comes from the base model's `contains` edges and the survival "
        "test goes through the correspondence, never through raw ids. Depth is the chain: "
        "a method links to its class, the class to its namespace.",
    ),
    CauseRule(
        name="contained-addition",
        arch=None,
        cause=EventKind.NODE_ADDED,
        consequence=EventKind.NODE_ADDED,
        subjects=_added_container,
        status=RuleStatus.ACTIVE,
        note="Not in §6.3, which names only the removal direction; the relation is the "
        "same one read on the head side. A new class arrives with its methods.",
    ),
    CauseRule(
        name="edge-lost-its-endpoint",
        arch=None,
        cause=EventKind.NODE_REMOVED,
        consequence=EventKind.EDGE_REMOVED,
        subjects=_removed_endpoints,
        status=RuleStatus.ACTIVE,
        note="Not in §6.3, and it is what carries v0's real cascades: removing one class "
        "removes every containment and call edge that touched it. `diff/edges.py` emits "
        "those deliberately and names this stage as the one that links them.",
    ),
    CauseRule(
        name="edge-gained-its-endpoint",
        arch=None,
        cause=EventKind.NODE_ADDED,
        consequence=EventKind.EDGE_ADDED,
        subjects=_added_endpoints,
        status=RuleStatus.ACTIVE,
        note="The mirror of `edge-lost-its-endpoint` on the head side.",
    ),
    CauseRule(
        name="rename-retargets-edges",
        arch="§6.3: NodeRenamed(F) causes every EdgeRetargeted whose endpoint is F",
        cause=EventKind.NODE_RENAMED,
        consequence=EventKind.EDGE_RETARGETED,
        subjects=_renamed_endpoint_of_retarget,
        status=RuleStatus.UNREACHABLE,
        note="`EdgeRetargeted` is refused by design (`diff/edges.py`), and edges compared "
        "in head coordinates mean a rename emits no edge events at all -- the 200 "
        "consequences never exist rather than needing collapse. No later tier changes "
        "this; only reversing the edge-identity decision would.",
    ),
    CauseRule(
        name="signature-change-breaks-callers",
        arch="§6.3: NodeAttrChanged(signature) causes SignatureIncompatible on callers",
        cause=EventKind.NODE_ATTR_CHANGED,
        cause_payload={"attr": "signature"},
        consequence=EventKind.SIGNATURE_INCOMPATIBLE,
        subjects=_callee_of,
        status=RuleStatus.DORMANT,
        note="Correct, and dormant because nothing emits `SignatureIncompatible`: it is "
        "derived, §12 puts derived events out of v0, and its payload is undesigned. This "
        "stage links events that exist and never synthesizes one -- doing so here would "
        "put an event in the JSON that no stage owns.",
    ),
)
