"""Decomposition: how many changes is this change? ARCH-001 §7, RESEARCH-002 §3.11.

"A change whose delta has three mutually disconnected components is three changes." That
sentence is RESEARCH-002's intent-conformance signal -- *meaningful change that doesn't
belong*: an unrelated fix, a drive-by refactor, a behaviour change smuggled inside a rename
PR -- and it costs one union-find over data the stage already has.

Everything turns on **what connects two events**, because the wrong answer makes the signal
a constant. Four rules, each earning its place:

1. **The same subject.** Two events about one entity are one change. This is the rule that
   collects the six events a single edited method can produce.

2. **A `caused_by` link.** A consequence belongs with its cause (§6.3). Taken as *data*:
   classify is a separate stage and may or may not have run, and a link pointing at an
   event that is not in the list is ignored rather than treated as an error -- this stage
   must not acquire an opinion about how the list was built.

3. **A dependency edge between their subjects, in either model.** `calls`, `inherits`,
   `overrides`, `uses` -- the relations of `graph.DEPENDENCY_EDGES` minus containment. If
   the caller changed and the callee changed, that is one change, whichever direction the
   edge points and whichever side of the diff it survives on. Both models are read because
   an edge deleted by the change still testifies that the two entities were related when the
   author started.

4. **A shared class-like container, or containment itself.** Two methods of one class are
   one change. **Containment through a namespace is not a connection** and this is the rule
   the whole partition depends on: everything in fmt is inside `namespace fmt`, so admitting
   namespaces would make every review exactly one component and turn the signal off while
   leaving it looking as though it worked.

The result is a **lower bound on connectedness**, and it will improve without changes here.
v0 emits no `uses-in-interface` edges (§12), so two functions coupled only through a shared
type look disconnected; the honest failure direction is over-splitting, which shows a
reviewer two units where there was one, rather than under-splitting, which hides an
unrelated change inside a large one.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from cpp_semantic_diff.model import EdgeKind, Event, EventId, Model, NodeId
from cpp_semantic_diff.rank.graph import DEPENDENCY_EDGES, containers, subject_endpoints

#: Rule 3's edge set: dependency minus containment. Containment is rule 4, which has the
#: namespace exclusion the module docstring is about; folding the two together would lose it.
CONNECTING_EDGES: frozenset[EdgeKind] = frozenset(DEPENDENCY_EDGES) - {EdgeKind.CONTAINS}


class Component(BaseModel):
    """One independently reviewable unit of the delta.

    `index` is assigned by descending `top_score`, so a consumer that shows components in
    order is showing the most consequential change first -- the same claim the event
    ordering makes, one level up.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    events: list[EventId] = Field(description="Ordered as the ranking orders them.")
    top_score: float = Field(description="The highest event total in the component.")

    @property
    def size(self) -> int:
        return len(self.events)


class _Union:
    """Union-find over event ids. Ordered by construction: `find` never reorders anything,
    and the caller assigns component indices from scores rather than from merge order."""

    def __init__(self, ids: list[EventId]) -> None:
        self.parent: dict[EventId, EventId] = {i: i for i in ids}

    def find(self, a: EventId) -> EventId:
        root = a
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[a] != root:  # path compression
            self.parent[a], a = root, self.parent[a]
        return root

    def join(self, a: EventId, b: EventId) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Smaller id becomes the root, so the partition is a function of the data and
            # not of the order the rules happened to fire in.
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            self.parent[hi] = lo


def partition(events: list[Event], base: Model, head: Model) -> dict[EventId, EventId]:
    """Event id -> the id of its component's representative. The four rules, in order."""
    ids = [e.id for e in events]
    union = _Union(ids)
    by_id = {e.id: e for e in events}

    # Rule 1: the same subject entity. An edge subject contributes both endpoints, which is
    # what puts `EdgeAdded(f -> g)` in the same unit as an event about f and one about g.
    by_node: dict[NodeId, list[EventId]] = defaultdict(list)
    for event in events:
        for node_id in subject_endpoints(event.subject):
            by_node[node_id].append(event.id)
    for group in by_node.values():
        _join_all(union, group)

    # Rule 2: cause links, as data. A dangling link is ignored, not raised on.
    for event in events:
        if event.caused_by is not None and event.caused_by in by_id:
            union.join(event.id, event.caused_by)

    # Rule 3: a dependency edge relating two touched entities, on either side.
    for model in (base, head):
        for edge in model.edges:
            if edge.kind not in CONNECTING_EDGES:
                continue
            src, dst = by_node.get(edge.src), by_node.get(edge.dst)
            if src and dst:
                union.join(src[0], dst[0])

    # Rule 4: containment, class-like containers only. Both directions matter: a member and
    # its class are one change, and two members of one class are one change even when the
    # class itself has no event of its own.
    for model in (base, head):
        parents = containers(model, coupling_only=True)
        siblings: dict[NodeId, list[EventId]] = defaultdict(list)
        for node_id, group in by_node.items():
            parent = parents.get(node_id)
            if parent is None:
                continue
            siblings[parent].extend(group)
            if owner := by_node.get(parent):
                union.join(group[0], owner[0])
        for group in siblings.values():
            _join_all(union, group)

    return {i: union.find(i) for i in ids}


def _join_all(union: _Union, group: list[EventId]) -> None:
    for other in group[1:]:
        union.join(group[0], other)


def components(
    events: list[Event],
    base: Model,
    head: Model,
    order: list[EventId],
    totals: dict[EventId, float],
) -> tuple[list[Component], dict[EventId, int]]:
    """The partition, numbered by descending risk.

    `order` is the ranking, and members are listed in it, so a component reads top-down the
    same way the flat list does. Numbering by `top_score` and then by that order makes the
    indices deterministic without depending on which representative union-find picked.
    """
    representative = partition(events, base, head)

    members: dict[EventId, list[EventId]] = defaultdict(list)
    for event_id in order:
        members[representative[event_id]].append(event_id)

    ranked = sorted(
        members.items(),
        key=lambda item: (-max(totals[e] for e in item[1]), order.index(item[1][0])),
    )

    result: list[Component] = []
    index_of: dict[EventId, int] = {}
    for index, (_, member_ids) in enumerate(ranked):
        result.append(
            Component(
                index=index,
                events=member_ids,
                top_score=max(totals[e] for e in member_ids),
            )
        )
        for event_id in member_ids:
            index_of[event_id] = index
    return result, index_of
