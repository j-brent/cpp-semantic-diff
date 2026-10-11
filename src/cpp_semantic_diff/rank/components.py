"""Decomposition: how many changes is this change? ARCH-001 §7, RESEARCH-002 §3.11.

"A change whose delta has three mutually disconnected components is three changes." That
sentence is RESEARCH-002's intent-conformance signal -- *meaningful change that doesn't
belong*: an unrelated fix, a drive-by refactor, a behaviour change smuggled inside a rename
PR -- and it costs one union-find over data the stage already has.

Everything turns on **what connects two events**, because the wrong answer makes the signal
a constant. Before the rules, the thing they are stated over:

**Every event has exactly one home entity** -- `graph.subject_home`: the subject for a node
event, the *source* for an edge event. This is the single decision the partition turned out to
depend on. Counting an edge event as belonging to both of its endpoints made every new call
into an untouched callee a connection between that callee's other callers, so unrelated changes
were welded together through entities that never changed. Measured on fmt at `HEAD~100` on
2026-08-02 (commit `ef2daff`): **78 % of events in one component**, held together by bundled
googletest, because every new `TEST_F` body calls `testing::internal::AssertHelper` and
overrides `testing::Test::TestBody`. Homing each event on one entity drops that to **18 %
across 30 components**, and it is the same correction `score.py` makes for blast radius: an
edge is written in its source's text, so the source is what changed.

Two rules that look like plausible fixes were measured and are *not* here, because after
the home rule neither buys anything: suppressing high-degree hub entities as connectors
(any cap from 3 to 20: no change to the partition) and refusing connections through vendored
code (no change). Both were fixes for a symptom of the attribution bug.

Four rules, each earning its place:

1. **The same home entity.** Two events about one entity are one change. This is the rule
   that collects the six events a single edited method can produce.

2. **A `caused_by` link.** A consequence belongs with its cause (§6.3). Taken as *data*:
   classify is a separate stage and may or may not have run, and a link pointing at an
   event that is not in the list is ignored rather than treated as an error -- this stage
   must not acquire an opinion about how the list was built.

3. **A dependency relation between two entities that both have events.** `calls`,
   `inherits`, `overrides`, `uses` -- the relations of `graph.DEPENDENCY_EDGES` minus
   containment, read off either model, plus the relation an edge event asserts in its own
   subject. If the caller changed and the callee changed, that is one change, whichever
   direction the edge points and whichever side of the diff it survives on. Both models are
   read because an edge deleted by the change still testifies that the two entities were
   related when the author started.

   **Both** ends must have an event of their own, and that is the whole difference from the
   version that collapsed. `EdgeAdded(f -> g)` still lands in the same unit as an event
   about `f` and an event about `g` -- but when `g` has no event, `g` is not evidence about
   anything, and two call sites into it are two changes.

4. **A shared class-like container, or containment itself.** Two methods of one class are
   one change. **Containment through a namespace is not a connection** and this is the rule
   the whole partition depends on: everything in fmt is inside `namespace fmt`, so admitting
   namespaces would make every review exactly one component and turn the signal off while
   leaving it looking as though it worked. Stated over homes like the rest: reading it over
   every entity an event *mentions* let one class re-import the bridging rule 1 had just
   given up, which is how the giant component survived the first attempt at this fix.

The result is a **lower bound on connectedness**, and it will improve without changes here.
v0 emits no `uses-in-interface` edges (§13), so two functions coupled only through a shared
type look disconnected; the honest failure direction is over-splitting, which shows a
reviewer two units where there was one, rather than under-splitting, which hides an
unrelated change inside a large one.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from cpp_semantic_diff.model import ChangeEvent, ChangeEventId, EdgeKind, Model, NodeId
from cpp_semantic_diff.rank.graph import (
    DEPENDENCY_EDGES,
    containers,
    subject_endpoints,
    subject_home,
)

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
    events: list[ChangeEventId] = Field(description="Ordered as the ranking orders them.")
    top_score: float = Field(description="The highest event total in the component.")

    @property
    def size(self) -> int:
        return len(self.events)


class _Union:
    """Union-find over event ids. Ordered by construction: `find` never reorders anything,
    and the caller assigns component indices from scores rather than from merge order."""

    def __init__(self, ids: list[ChangeEventId]) -> None:
        self.parent: dict[ChangeEventId, ChangeEventId] = {i: i for i in ids}

    def find(self, a: ChangeEventId) -> ChangeEventId:
        root = a
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[a] != root:  # path compression
            self.parent[a], a = root, self.parent[a]
        return root

    def join(self, a: ChangeEventId, b: ChangeEventId) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Smaller id becomes the root, so the partition is a function of the data and
            # not of the order the rules happened to fire in.
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            self.parent[hi] = lo


def partition(
    events: list[ChangeEvent], base: Model, head: Model
) -> dict[ChangeEventId, ChangeEventId]:
    """ChangeEvent id -> the id of its component's representative. The four rules, in order.

    Order-independent by construction: `_Union.join` always roots a merged set at its
    smallest id, so the representative of a set is the minimum id in it whatever sequence
    the rules fired in. §10 makes the JSON the product, and a partition that reshuffled
    would make every diff of the output meaningless.
    """
    ids = [e.id for e in events]
    union = _Union(ids)
    by_id = {e.id: e for e in events}

    # Rule 1: one home entity per event (`graph.subject_home`), and events sharing it are one
    # change. An edge event is homed on its source, never on both endpoints -- see the module
    # docstring for what that cost.
    at_home: dict[NodeId, list[ChangeEventId]] = defaultdict(list)
    for event in events:
        at_home[subject_home(event.subject)].append(event.id)
    for group in at_home.values():
        _join_all(union, group)

    # Rule 2: cause links, as data. A dangling link is ignored, not raised on.
    for event in events:
        if event.caused_by is not None and event.caused_by in by_id:
            union.join(event.id, event.caused_by)

    # Rule 3: a dependency relation between two entities that *both* have events. Asserted
    # either by an edge event's own subject or by an edge in either model; the two are the
    # same relation reaching this stage by different routes, so they are read the same way.
    def connect(a: NodeId, b: NodeId) -> None:
        left, right = at_home.get(a), at_home.get(b)
        if left and right:
            union.join(left[0], right[0])

    for event in events:
        endpoints = subject_endpoints(event.subject)
        if len(endpoints) == 2:
            connect(*endpoints)
    for model in (base, head):
        for edge in model.edges:
            if edge.kind in CONNECTING_EDGES:
                connect(edge.src, edge.dst)

    # Rule 4: containment, class-like containers only. Both directions matter: a member and
    # its class are one change, and two members of one class are one change even when the
    # class itself has no event of its own.
    for model in (base, head):
        parents = containers(model, coupling_only=True)
        siblings: dict[NodeId, list[ChangeEventId]] = defaultdict(list)
        for node_id, group in at_home.items():
            parent = parents.get(node_id)
            if parent is None:
                continue
            siblings[parent].extend(group)
            if owner := at_home.get(parent):
                union.join(group[0], owner[0])
        for group in siblings.values():
            _join_all(union, group)

    return {i: union.find(i) for i in ids}


def _join_all(union: _Union, group: list[ChangeEventId]) -> None:
    for other in group[1:]:
        union.join(group[0], other)


def components(
    events: list[ChangeEvent],
    base: Model,
    head: Model,
    order: list[ChangeEventId],
    totals: dict[ChangeEventId, float],
) -> tuple[list[Component], dict[ChangeEventId, int]]:
    """The partition, numbered by descending risk.

    `order` is the ranking, and members are listed in it, so a component reads top-down the
    same way the flat list does. Numbering by `top_score` and then by that order makes the
    indices deterministic without depending on which representative union-find picked.
    """
    representative = partition(events, base, head)

    members: dict[ChangeEventId, list[ChangeEventId]] = defaultdict(list)
    for event_id in order:
        members[representative[event_id]].append(event_id)

    ranked = sorted(
        members.items(),
        key=lambda item: (-max(totals[e] for e in item[1]), order.index(item[1][0])),
    )

    result: list[Component] = []
    index_of: dict[ChangeEventId, int] = {}
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
