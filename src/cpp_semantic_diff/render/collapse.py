"""Cascade collapse: hiding consequences under their causes. ARCH-001 §6.3.

**This is the only place in the pipeline that collapses anything.** The diff stage says so
in its own docstring, classify says it "adds links and nothing else", and STATUS §8 records
the decision with its reason: the JSON always carries the full event set and the view hides
consequences under causes. A stage that collapsed earlier would delete changes from the
document every downstream view reads, with no way for any of them to know.

So this module builds a **tree over event ids** and touches no event. It is a pure function
of a `DiffDocument`: give it the same document twice and you get the same tree, and throw
the tree away and the document is unchanged. `flatten()` exists to make the invariant
testable in one line -- every event in the document appears exactly once in the tree.

**Why classify's chains nest.** `classify/rules.py` links a removed method to its *immediate*
container rather than to the outermost removed ancestor, "because the chain is what render
(§6.3) needs to nest the tree, and the outermost link would flatten three levels of
structure into one". This module is the consumer that argument was written for: it follows
one link per level, so a removed namespace shows its classes, which show their methods.

**Three ways a cause link is refused, and all three default to showing.** §6.3: "when cause
attribution is uncertain, default to showing, because a hidden real change is a far worse
failure than a redundant one." Concretely:

1. **No cause.** `caused_by is None` -- the common case, and a top-level event.
2. **A dangling cause.** `caused_by` names an event the document does not carry. That
   should be impossible (ids are content-derived and classify never invents one), which is
   exactly why it must not be trusted: the only safe reading of an impossible state is that
   something upstream is wrong, and burying an event under a cause that is not there would
   make it invisible in every view at once.
3. **A cycle.** Classify refuses links that close one, but this module reads a *document*
   and a document can arrive from a file, from a test, or from a future stage. An event
   inside a cycle is promoted to top level rather than dropped -- a cycle is precisely the
   case where "which of these is the cause" has no answer, and §6.3 says show.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cpp_semantic_diff.model import EventId

if TYPE_CHECKING:  # pragma: no cover - import cycle only at type-check time
    from cpp_semantic_diff.render.document import DiffDocument


@dataclass(frozen=True)
class CollapseNode:
    """One event and the events it explains, in reading order."""

    event_id: EventId
    children: tuple[CollapseNode, ...] = ()

    @property
    def hidden(self) -> int:
        """Events this node collapses -- the whole subtree below it, at every depth."""
        return sum(1 + child.hidden for child in self.children)

    def walk(self, depth: int = 0) -> Iterator[tuple[CollapseNode, int]]:
        """Depth-first, cause before consequence: the order a reader reads them in."""
        yield self, depth
        for child in self.children:
            yield from child.walk(depth + 1)


@dataclass(frozen=True)
class Collapsed:
    """The whole delta as a forest, plus what the view needs to be honest about it."""

    roots: tuple[CollapseNode, ...]

    #: Events whose cause link was refused -- dangling or cyclic, see the module docstring.
    #: They are top-level in `roots` like any uncaused event; this names *why*, so that a
    #: refusal is diagnosable instead of merely invisible.
    promoted: tuple[EventId, ...] = ()

    def flatten(self) -> list[EventId]:
        """Every event id in the forest, in reading order.

        The §6.3 invariant is a set equality against the document, and this is the half of
        it the view side owns: `set(flatten()) == {e.id for e in document.events}`, always.
        """
        return [node.event_id for root in self.roots for node, _ in root.walk()]

    @property
    def hidden(self) -> int:
        return sum(root.hidden for root in self.roots)


def collapse(document: DiffDocument, *, enabled: bool = True) -> Collapsed:
    """Nest consequences under causes, in the document's ranking order (ARCH-001 §7).

    `enabled=False` returns the same events as a flat forest rather than a different set.
    That is the point of the flag: "show me everything" and "show me the tree" must differ
    in shape only, so a reviewer who distrusts the collapse can turn it off and be certain
    they are looking at the same delta rather than at a second opinion about it.
    """
    order = [e.id for e in document.ranked()]
    if not enabled:
        return Collapsed(roots=tuple(CollapseNode(event_id=i) for i in order))

    present = set(order)
    index = document.by_id

    # A cause link is used only when it survives all three refusals. `promoted` records
    # the ones that did not, because a link that was silently ignored is a link nobody will
    # ever find the bug in.
    parent: dict[EventId, EventId] = {}
    promoted: list[EventId] = []
    for event_id in order:
        cause = index[event_id].caused_by
        if cause is None:
            continue
        if cause not in present:
            promoted.append(event_id)  # refusal 2: dangling
            continue
        parent[event_id] = cause

    for event_id in sorted(_on_a_cycle(parent)):
        del parent[event_id]
        promoted.append(event_id)  # refusal 3: cyclic

    children: dict[EventId, list[EventId]] = defaultdict(list)
    for event_id in order:  # ranking order in, ranking order out, at every depth
        if (cause := parent.get(event_id)) is not None:
            children[cause].append(event_id)

    def build(event_id: EventId) -> CollapseNode:
        return CollapseNode(
            event_id=event_id,
            children=tuple(build(child) for child in children[event_id]),
        )

    roots = tuple(build(i) for i in order if i not in parent)
    return Collapsed(roots=roots, promoted=tuple(promoted))


def _on_a_cycle(parent: dict[EventId, EventId]) -> set[EventId]:
    """Every event lying on a cause cycle. **All of them lose their link, not one of them.**

    Breaking a cycle at an arbitrary link would leave the rest of it nested -- which is to
    say, it would pick a cause out of a set of events that by construction have no ordering
    between them, on no evidence, and then present that pick as structure. §6.3's rule is
    the opposite: when cause attribution is uncertain, show. So every member is promoted to
    top level and the cycle simply produces no nesting.

    `parent` is a functional graph (one cause per event), so this is the standard three-
    colour walk: a node reached while still on the current path closes a cycle, and the
    tail of the path from that node is the cycle.
    """
    VISITING, DONE = 0, 1
    colour: dict[EventId, int] = {}
    on_cycle: set[EventId] = set()

    for start in parent:
        path: list[EventId] = []
        current: EventId | None = start
        while current is not None and current not in colour:
            colour[current] = VISITING
            path.append(current)
            current = parent.get(current)
        if current is not None and colour.get(current) == VISITING:
            on_cycle.update(path[path.index(current) :])
        for event_id in path:
            colour[event_id] = DONE

    return on_cycle
