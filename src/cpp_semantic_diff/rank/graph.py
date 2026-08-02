"""Graph reads the rank stage needs: what an event is *about*, and what depends on it.

ARCH-001 §7 asks for "transitive reverse reachability of the subject over `calls` +
`contains` + `uses` edges, computed on the head model. Bounded depth; the count matters
more than the set." Three of those words carry the whole design, and each is answered here.

**Reverse.** The walk only ever goes from an entity to the things that *depend on* it. It
never goes the other way. That distinction is not cosmetic: `contains` is a downward
relation, and following it downward from a namespace would collect every entity in the
namespace and report a one-line change to a private helper as touching four thousand
things. Every edge kind below is therefore registered with an explicit direction, and
`contains` is registered child -> container. A changed method reaches its class, its class
reaches whatever inherits from it; nothing ever reaches a sibling.

**Bounded.** Depth is capped, and so is the number of nodes visited. A widely-called
utility in a large repository has a reverse cone that is most of the graph, and paying for
it per event would put the §9 budget out of reach for no gain -- everything past the cap is
"very large", which the saturation curve in `score.py` already treats as identical.

**Count, not set.** `BlastResult` carries numbers. The set is discarded at the end of the
walk, because nothing downstream needs to know *which* four hundred entities depend on the
subject, and carrying it would put an O(events x graph) structure in the output document.

`networkx` is the declared graph library (ARCH-001 §13). It is not used here: both walks
below need per-edge-kind direction rules and a visit cap that its traversal APIs do not
express, and its set-returning results would have to be re-sorted for determinism anyway.
It stays the right answer for the algorithms §6.1's derived events will need.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from cpp_semantic_diff.model import AnyId, Edge, EdgeKind, Model, NodeId, NodeKind

#: Edge kinds whose reverse direction means "is affected by a change to". Registered as
#: (kind, reversed?) where reversed? says the *dependent* is the edge's `src`.
#:
#: ARCH-001 §7 names `calls` + `contains` + `uses`. Both `uses` kinds are in the enum and
#: the v0 producer emits neither (§13 scopes extraction to contains/calls/inherits/
#: overrides), so the `uses` term contributes nothing today. They are listed anyway so the
#: factor strengthens on its own the day the producer emits them, rather than needing this
#: table found and edited.
#:
#: `inherits` and `overrides` are added, and the substitution is deliberate. They are the
#: two relations the producer *does* emit that propagate a change with no interpretation
#: required: change a base class and every derived class changes with it; change a virtual
#: method's contract and every override is answerable to the new one. Leaving them out to
#: match the doc's word list would mean a v0 blast radius computed over `calls` alone --
#: which is to say, over 58 % of fmt's edges while ignoring the 2,138 that carry the C++
#: mechanism most likely to make a change unsafe at a distance.
DEPENDENCY_EDGES: dict[EdgeKind, bool] = {
    # caller -> callee. The dependent is the caller: reverse it.
    EdgeKind.CALLS: True,
    # derived -> base. The dependent is the derived class.
    EdgeKind.INHERITS: True,
    # override -> overridden. The dependent is the override.
    EdgeKind.OVERRIDES: True,
    EdgeKind.USES_IN_INTERFACE: True,
    EdgeKind.USES_IN_IMPLEMENTATION: True,
    # container -> child, and this one is *not* reversed in the usual sense: the container
    # is what a change to the child reaches. Registered as src-is-dependent so the walk
    # travels child -> container and can never travel container -> child.
    EdgeKind.CONTAINS: True,
}

#: Kinds whose containment is a real coupling rather than a filing decision. A class and
#: its methods change together; a namespace and the four hundred entities in it do not.
#: Used by exposure promotion and by decomposition -- if `namespace fmt` counted as a
#: shared container, every change in fmt would be one component and the §3.11 signal would
#: be a constant.
COUPLING_CONTAINER_KINDS: frozenset[NodeKind] = frozenset(
    {NodeKind.CLASS, NodeKind.STRUCT, NodeKind.UNION}
)

_EDGE_KIND_VALUES: frozenset[str] = frozenset(k.value for k in EdgeKind)


def subject_endpoints(subject: AnyId) -> tuple[NodeId, ...]:
    """The node ids an event's subject is about: one for a node, two for an edge.

    `Event.subject` is an `AnyId` -- a node id or an edge id -- and the two are not
    distinguished by type (ARCH-001 §6.2). `Edge.id` is `src|kind|dst` (§4.2), so an edge
    id is recognised by having exactly three `|`-separated parts whose middle is a declared
    edge kind. Anything else is a node id, including the pathological case of a USR that
    happens to contain a pipe, which resolves to no node and simply scores zero blast
    radius rather than mis-resolving to some other entity.
    """
    parts = subject.split("|")
    if len(parts) == 3 and parts[1] in _EDGE_KIND_VALUES:
        return (NodeId(parts[0]), NodeId(parts[2]))
    return (NodeId(subject),)


def subject_home(subject: AnyId) -> NodeId:
    """The **one** entity an event is about. A node is itself; an edge is its source.

    ARCH-001 §6.2 gives an event one subject, but an edge subject names two entities, and
    every consumer that treats both as "what changed" makes the same two mistakes. Blast
    radius attributes a new call `A -> B` to whichever of A and B more of the codebase can
    reach, so a new call into a popular utility inherits the *utility's* fan-in -- and the
    utility did not change. Decomposition counts the event as belonging to both, so two
    unrelated call sites into one untouched callee become one change; on fmt at `HEAD~100`
    that welded 260 of 335 events into a single component through bundled googletest.

    The source is the answer, and it is the answer classify already gives. `rules.py`'s
    `_removed_endpoints` picks the source over the destination because *an edge is written
    in its source's text*: the call site is in the caller's body, the base clause is in the
    derived class, the containment is the container's braces -- and `diff/context.py` puts
    exactly those lines in the event's provenance. So the source is the entity whose text
    changed, which makes it the entity whose behaviour changed and the one a reviewer
    following provenance lands in.

    This is a claim about **attribution**, not about relatedness. That `A` now calls `B` is
    still a relation between them, and `components.py` rule 3 reads it as one -- but only
    when `B` has an event of its own, which is what stops an untouched callee from acting
    as a hub.
    """
    return subject_endpoints(subject)[0]


def containers(model: Model, *, coupling_only: bool = False) -> dict[NodeId, NodeId]:
    """Child -> containing node, from the `contains` edges (ARCH-001 §4.2).

    `coupling_only` drops containers that are namespaces (or any kind outside
    `COUPLING_CONTAINER_KINDS`), which is what stops one enormous namespace from being
    treated as a unit of coupling. A container the model does not carry a node for is
    dropped too: an unresolved edge endpoint (STATUS §3) has no kind to test.
    """
    parents: dict[NodeId, NodeId] = {}
    for edge in model.edges:
        if edge.kind is not EdgeKind.CONTAINS or edge.dst in parents:
            continue
        if coupling_only:
            parent = model.nodes.get(edge.src)
            if parent is None or parent.kind not in COUPLING_CONTAINER_KINDS:
                continue
        parents[edge.dst] = edge.src
    return parents


@dataclass(frozen=True)
class BlastResult:
    """What a bounded reverse walk found. Numbers, never the set -- see the module docstring."""

    count: int
    direct: int
    saturated: bool

    @property
    def detail(self) -> str:
        suffix = "+" if self.saturated else ""
        return f"{self.count}{suffix} dependent(s), {self.direct} direct"


EMPTY_BLAST = BlastResult(count=0, direct=0, saturated=False)


@dataclass(frozen=True)
class BlastGraph:
    """Reverse dependency adjacency for one model, built once and queried per event."""

    dependents: dict[NodeId, tuple[NodeId, ...]]

    @classmethod
    def of(cls, model: Model) -> BlastGraph:
        adjacency: dict[NodeId, set[NodeId]] = defaultdict(set)
        for edge in model.edges:
            _register(adjacency, edge, model)
        # Sorted, because the visit cap makes the traversal order observable: two runs that
        # truncate at a different point would report different counts for the same commit.
        return cls(dependents={k: tuple(sorted(v)) for k, v in adjacency.items()})

    def radius(self, roots: Iterable[NodeId], *, max_depth: int, visit_cap: int) -> BlastResult:
        """Distinct entities that can reach any of `roots`, within `max_depth` hops.

        The roots themselves are excluded: an entity is not its own blast radius, and
        including it would give every event a floor of one and make a leaf indistinguishable
        from a node with a single caller.
        """
        seen: set[NodeId] = set(roots)
        if not seen:
            return EMPTY_BLAST
        frontier = sorted(seen)
        found: set[NodeId] = set()
        direct = 0
        saturated = False

        for depth in range(max_depth):
            nxt: list[NodeId] = []
            for node in frontier:
                for dependent in self.dependents.get(node, ()):
                    if dependent in seen:
                        continue
                    seen.add(dependent)
                    found.add(dependent)
                    nxt.append(dependent)
                    if len(found) >= visit_cap:
                        # Everything past the cap is "very large", which the saturation
                        # curve treats as identical to the cap. Reported, never silent.
                        saturated = True
                        break
                if saturated:
                    break
            if depth == 0:
                direct = len(found)
            if saturated or not nxt:
                break
            frontier = nxt

        return BlastResult(count=len(found), direct=direct, saturated=saturated)


def _register(adjacency: dict[NodeId, set[NodeId]], edge: Edge, model: Model) -> None:
    if edge.kind not in DEPENDENCY_EDGES:
        return
    # An edge whose endpoints are not both nodes is an unresolved edge the builder counted
    # rather than dropped (STATUS §3). It cannot contribute a dependent that exists.
    if edge.src not in model.nodes or edge.dst not in model.nodes:
        return
    if edge.src == edge.dst:
        return  # self-recursion is not a dependent
    adjacency[edge.dst].add(edge.src)
