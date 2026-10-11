"""The cause rule table (ARCH-001 §6.3).

§6.3 is emphatic that consequences are linked to causes by **an explicit rule table, not
inference**, so this is a table: a tuple of `CauseRule`, each naming the kind it explains,
the kind that explains it, and a small locator saying which subject to look for. It is meant
to be read side by side with §6.3 and agreed or disagreed with. Nothing outside this module
decides what causes what.

**One row does not fire in v0.** `rename-retargets-edges` is unreachable: "NodeRenamed(F)
causes every EdgeRetargeted whose endpoint is F" describes something this pipeline cannot
produce, twice over. `EdgeRetargeted` is refused by design (`diff/edges.py`): an edge has no
identity of its own, so claiming one "was retargeted" re-makes a node-identity claim the
one-to-one matcher declined. And because base edges are rewritten into head coordinates
*through the correspondence* before comparison, a rename produces no edge events at all --
the 200 consequences §6.3 wants collapsed never exist. That is a better answer to the same
problem, but it leaves the rule with nothing to fire on, and no later tier changes that: tier
2 makes `NodeRenamed` reachable without making a single edge event appear beside it. The rule
is kept, marked `RuleStatus.UNREACHABLE`, because deleting it would lose the argument.

`signature-change-breaks-callers` links each `CallerOfChangedSignature` to the
`NodeSignatureChanged` of its callee. The derive stage creates those events (ADR-040), not
this one. **Classify links causes among events that already exist; it never synthesizes one.**

The two edge-endpoint rules are the ones that carry v0's actual cascades: an edge event on an
endpoint that was added or removed. `diff/edges.py` emits those "rather than folding
[them] into the `NodeRemoved` for that endpoint ... a consequence is linked to its cause by
classify, never dropped here". Without them, removing one class leaves every containment and
call edge it touched as an unexplained top-level event.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from cpp_semantic_diff.classify.context import ClassifyContext
from cpp_semantic_diff.model import (
    AnyId,
    AttrValue,
    ChangeEvent,
    ChangeEventKind,
    EdgeId,
    EdgeKind,
    NodeId,
)


class RuleStatus(StrEnum):
    """Whether a rule can fire on the events this pipeline emits today.

    Recorded on the rule rather than left to a comment, because "correct but cannot fire" is
    exactly the state that rots: the next reader either deletes a rule that a later tier
    needs, or trusts a cascade that is not being collapsed. Both are silent.
    """

    ACTIVE = "active"
    #: Correct, and will fire once a stage that is out of v0 scope (ARCH-001 §13) lands.
    DORMANT = "dormant"
    #: Correct against ARCH-001 §6.3 as written, but describing events the pipeline refuses
    #: to emit by design rather than by omission. No later stage makes it reachable.
    UNREACHABLE = "unreachable"


#: A locator: given the context and one *consequence* event, the subjects a cause might have,
#: **most plausible first**. Returning several is how a rule declares that an event has more
#: than one candidate cause; the classifier takes the first that exists, so the order here is
#: the whole of the tie-break and must be principled rather than incidental.
type Locator = Callable[[ClassifyContext, ChangeEvent], Iterable[AnyId]]


@dataclass(frozen=True)
class CauseRule:
    """One line of ARCH-001 §6.3's table.

    Read it as: *a `consequence` event is caused by the `cause` event on whichever of the
    subjects `subjects` names -- the first of them that turns out to have one.*
    """

    name: str
    cause: ChangeEventKind
    consequence: ChangeEventKind
    subjects: Locator
    status: RuleStatus
    arch: str | None
    note: str
    cause_payload: Mapping[str, AttrValue] = field(default_factory=dict)
    """Payload keys the cause event must carry. This is what would let a §6.3 row written
    as `NodeAttrChanged(<attr>)` be a table entry rather than a special case in the driver:
    the parenthesis is a payload filter, and it is spelled as one. No row needs one today --
    the signature, which did, has its own kind (ADR-035)."""


# --------------------------------------------------------------------------------------
# Locators. Each is one relation from the model, expressed in the coordinates the events it
# joins are already in -- base for removals, head for additions -- so that no rule has to
# translate between the two sides a second time (`diff/nodes.py` on why removals stay in
# base coordinates).


def _removed_container(ctx: ClassifyContext, event: ChangeEvent) -> Iterator[NodeId]:
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


def _added_container(ctx: ClassifyContext, event: ChangeEvent) -> Iterator[NodeId]:
    """The mirror of `_removed_container`, on the head side.

    The relation is the same one: a new class arrives with its methods, and the methods are
    consequences of the class in exactly the sense §6.3 means. Omitting it would make a large
    addition read as N unexplained top-level events while the identical removal read as one.
    """
    parent = ctx.head_parents.get(NodeId(event.subject))
    if parent is not None and ctx.added(parent):
        yield parent


def _removed_endpoints(ctx: ClassifyContext, event: ChangeEvent) -> Iterator[NodeId]:
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


def _added_endpoints(ctx: ClassifyContext, event: ChangeEvent) -> Iterator[NodeId]:
    """The mirror of `_removed_endpoints`, on the head side. Source first, same reason."""
    for node_id in ctx.head_endpoints.get(EdgeId(event.subject), ()):
        if ctx.added(node_id):
            yield node_id


def _renamed_endpoint_of_retarget(ctx: ClassifyContext, event: ChangeEvent) -> Iterator[NodeId]:
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


def _callee_of_call(ctx: ClassifyContext, event: ChangeEvent) -> Iterator[NodeId]:
    """The callee of the call a `CallerOfChangedSignature` is about: its subject edge's head
    destination (ADR-040).

    The subject is one call edge, so the answer is one node, and a caller of two changed
    functions gets two events, each with its own cause. Read off the head model, like every
    other endpoint here, rather than off the payload's `callee`: the model holds the fact.
    """
    endpoints = ctx.head_endpoints.get(EdgeId(event.subject))
    if endpoints is not None:
        yield endpoints[1]


def _overload_the_caller_moved_to(ctx: ClassifyContext, event: ChangeEvent) -> Iterator[EdgeId]:
    """The edge this caller gained to a *different overload of the same function*.

    The shape, measured on leveldb: googletest gave `AssertHelper::AssertHelper` a
    `std::string_view` overload, and every `TEST` body stopped calling the `const char *` one.
    The result is `EdgeRemoved` events whose endpoints both survive -- the old overload is
    still declared, it is merely no longer called -- so neither `edge-lost-its-endpoint` nor
    anything else in this table reaches them, and they were the bulk of what was left at top
    level after every other cascade had collapsed.

    **This is not `EdgeRetargeted` by another name.** That event is refused because an edge's
    id is `src|kind|dst`, so "retargeted from B to C" asserts B and C are the same entity --
    a node-identity claim the one-to-one matcher already declined. This rule asserts nothing
    of the kind: B and C are distinct nodes, distinct overloads, both present at head. The
    claim is only that one call site moved within an overload set, which is a fact about the
    *caller's* outgoing edges and needs no opinion about whether B and C are the same thing.

    Same edge kind, and the overload set is `qualified_name` equality -- which is what an
    overload set *is*, since overloads differ exactly in signature. Where the old overload
    was removed and the new one is the only other of its name, the name tier pairs them as
    one function whose signature changed (ADR-036), the edge maps through the
    correspondence, and this rule has nothing to explain. Candidates arrive in
    `head_out_edges` order, which is sorted by edge id, so a caller that gained two overloads
    gets a stable answer rather than one that depends on model construction.
    """
    removed = ctx.base_edges.get(EdgeId(event.subject))
    if removed is None:
        return
    old = ctx.base.nodes.get(removed.dst)
    if old is None:
        return
    for edge in ctx.head_out_edges.get(removed.src, ()):
        if edge.kind is not removed.kind or edge.dst == removed.dst:
            continue
        gained = ctx.head.nodes.get(edge.dst)
        if gained is not None and gained.qualified_name == old.qualified_name:
            yield edge.id


def _overridden_function(ctx: ClassifyContext, event: ChangeEvent) -> Iterator[NodeId]:
    """The virtual function a removed `overrides` edge pointed at, in head coordinates.

    When a virtual function's signature changes and an override keeps the old one, the
    override stops overriding: the `overrides` edge is removed while both functions survive.
    The signature change is the cause, so the locator names the edge's destination, mapped
    through the correspondence like every other cross-side id. Where that destination has no
    `NodeSignatureChanged`, the classifier finds no cause and the edge stays at top level.

    Only while the override's class still derives from the overridden function's class at
    head, directly or not. A class that stopped inheriting lost the override for that reason,
    and filing it under the signature change would hide the real cause.
    """
    removed = ctx.base_edges.get(EdgeId(event.subject))
    if removed is None or removed.kind is not EdgeKind.OVERRIDES:
        return
    overridden = ctx.correspondence.head_for(removed.dst)
    if overridden is None or not _still_derives(ctx, removed.src, removed.dst):
        return
    yield overridden


def _still_derives(ctx: ClassifyContext, override: NodeId, overridden: NodeId) -> bool:
    """Whether the class holding `override` derives from the class holding `overridden` at
    head, through `inherits` edges. Both arguments are base ids; the classes are mapped
    through the correspondence."""
    derived_class = ctx.base_parents.get(override)
    base_class = ctx.base_parents.get(overridden)
    if derived_class is None or base_class is None:
        return False
    start = ctx.correspondence.head_for(derived_class)
    goal = ctx.correspondence.head_for(base_class)
    if start is None or goal is None:
        return False
    seen = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        if current == goal:
            return True
        for edge in ctx.head_out_edges.get(current, ()):
            if edge.kind is EdgeKind.INHERITS and edge.dst not in seen:
                seen.add(edge.dst)
                frontier.append(edge.dst)
    return False


# --------------------------------------------------------------------------------------
# The table. Ordered; the first rule that finds a cause wins, which matters only if two
# rules ever share a consequence kind -- `edge-lost-its-endpoint` and `call-moved-to-another
# -overload` both explain an `EdgeRemoved`, and the order below is the tie-break: an endpoint
# that actually went away is the better explanation than one the caller merely stopped using.

CAUSE_RULES: tuple[CauseRule, ...] = (
    CauseRule(
        name="contained-removal",
        arch="§6.3: NodeRemoved(C) causes NodeRemoved for everything C contained",
        cause=ChangeEventKind.NODE_REMOVED,
        consequence=ChangeEventKind.NODE_REMOVED,
        subjects=_removed_container,
        status=RuleStatus.ACTIVE,
        note="Containment comes from the base model's `contains` edges and the survival "
        "test goes through the correspondence, never through raw ids. Depth is the chain: "
        "a method links to its class, the class to its namespace.",
    ),
    CauseRule(
        name="contained-addition",
        arch="§6.3: NodeAdded(C) causes NodeAdded for everything C contains",
        cause=ChangeEventKind.NODE_ADDED,
        consequence=ChangeEventKind.NODE_ADDED,
        subjects=_added_container,
        status=RuleStatus.ACTIVE,
        note="The mirror of `contained-removal`: the same relation read on the head side. "
        "A new class arrives with its methods.",
    ),
    CauseRule(
        name="edge-lost-its-endpoint",
        arch="§6.3: NodeRemoved(N) causes EdgeRemoved for edges touching N",
        cause=ChangeEventKind.NODE_REMOVED,
        consequence=ChangeEventKind.EDGE_REMOVED,
        subjects=_removed_endpoints,
        status=RuleStatus.ACTIVE,
        note="It carries v0's real cascades: removing one class removes every containment "
        "and call edge that touched it. `diff/edges.py` emits those deliberately and names "
        "this stage as the one that links them.",
    ),
    CauseRule(
        name="call-moved-to-another-overload",
        arch="§6.3: EdgeAdded(caller→G) causes EdgeRemoved(caller→F), "
        "F and G overloads of one name",
        cause=ChangeEventKind.EDGE_ADDED,
        consequence=ChangeEventKind.EDGE_REMOVED,
        subjects=_overload_the_caller_moved_to,
        status=RuleStatus.ACTIVE,
        note="Found by running the tool: the one shape that survived every other rule on a "
        "real repository. An overload is added to a dependency and every call site moves to it, "
        "leaving an `EdgeRemoved` whose endpoints both still exist. The cause is the "
        "`EdgeAdded` rather than the new overload's `NodeAdded` so that the chain stays "
        "one-step-per-fact -- the addition is already linked to the node by "
        "`edge-gained-its-endpoint`, and both collapse under it.",
    ),
    CauseRule(
        name="signature-change-breaks-overrides",
        arch="§6.3: NodeSignatureChanged(F) causes EdgeRemoved(override→F)",
        cause=ChangeEventKind.NODE_SIGNATURE_CHANGED,
        consequence=ChangeEventKind.EDGE_REMOVED,
        subjects=_overridden_function,
        status=RuleStatus.ACTIVE,
        note="A virtual function's signature changed and an override kept the old one, so it "
        "no longer overrides. Both functions survive, so no endpoint rule reaches the removed "
        "`overrides` edge. Below the endpoint and overload rules: an override that went away "
        "or moved is the better account (ADR-040).",
    ),
    CauseRule(
        name="edge-gained-its-endpoint",
        arch="§6.3: NodeAdded(N) causes EdgeAdded for edges touching N",
        cause=ChangeEventKind.NODE_ADDED,
        consequence=ChangeEventKind.EDGE_ADDED,
        subjects=_added_endpoints,
        status=RuleStatus.ACTIVE,
        note="The mirror of `edge-lost-its-endpoint` on the head side.",
    ),
    CauseRule(
        name="rename-retargets-edges",
        arch="§6.3: NodeRenamed(F) causes EdgeRetargeted at F",
        cause=ChangeEventKind.NODE_RENAMED,
        consequence=ChangeEventKind.EDGE_RETARGETED,
        subjects=_renamed_endpoint_of_retarget,
        status=RuleStatus.UNREACHABLE,
        note="`EdgeRetargeted` is refused by design (`diff/edges.py`), and edges compared "
        "in head coordinates mean a rename emits no edge events at all -- the 200 "
        "consequences never exist rather than needing collapse. No later tier changes "
        "this; only reversing the edge-identity decision would.",
    ),
    CauseRule(
        name="signature-change-breaks-callers",
        arch="§6.3: NodeSignatureChanged(F) causes CallerOfChangedSignature for each call into F",
        cause=ChangeEventKind.NODE_SIGNATURE_CHANGED,
        consequence=ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE,
        subjects=_callee_of_call,
        status=RuleStatus.ACTIVE,
        note="The derive stage emits one `CallerOfChangedSignature` per call into a function "
        "whose signature changed (ADR-040); this links each to that change. The callee is the "
        "subject edge's head destination, so every caller event has exactly one candidate.",
    ),
)
