"""Cause and cascade -- ARCH-001 §6.3.

Two things can go wrong in this stage and neither is visible in the output. It can attribute
a cause that is not one, which hides a real change under an unrelated parent the moment
render collapses the tree; and it can start *helping* -- deduplicating, merging, dropping the
consequences it just explained -- which deletes changes from the JSON that every view reads.
So the tests below assert the design claims: the full event set survives, a link is only a
link, and an event nobody can explain is still shown.

Events and models are built by hand. Nothing here needs libclang, a compile database or a
fixture clone, which is the point of the stage being a pure function (§3).
"""

from __future__ import annotations

import itertools
from typing import Any

from cpp_semantic_diff.classify import CAUSE_RULES, RuleStatus, classify_events
from cpp_semantic_diff.derive import derive_events
from cpp_semantic_diff.diff import diff_models
from cpp_semantic_diff.match import Correspondence, Match, match_models
from cpp_semantic_diff.model import (
    AttrValue,
    ChangeEvent,
    ChangeEventKind,
    Confidence,
    Edge,
    EdgeKind,
    Location,
    MatchTier,
    Model,
    Node,
    NodeId,
    NodeKind,
    Unpaired,
)

N = NodeId("c:@N@ns")
C = NodeId("c:@N@ns@S@C")
M = NodeId("c:@N@ns@S@C@F@m#")
F = NodeId("c:@F@f#")
G = NodeId("c:@F@g#")


def node(usr: str, name: str = "f", **kw: Any) -> Node:
    defaults = {"kind": NodeKind.FUNCTION, "name": name, "qualified_name": name}
    return Node(id=NodeId(usr), **{**defaults, **kw})


def model(rev: str, *nodes: Node, edges: list[Edge] | None = None, **kw: Any) -> Model:
    m = Model(rev=rev, edges=edges or [], **kw)
    for n in nodes:
        m.add_node(n)
    return m


def edge(src: str, dst: str, kind: EdgeKind = EdgeKind.CALLS, line: int = 1) -> Edge:
    return Edge(
        src=NodeId(src),
        dst=NodeId(dst),
        kind=kind,
        locations=[Location(file="src/a.cpp", line=line)],
    )


def holds(src: str, dst: str, line: int = 1) -> Edge:
    return edge(src, dst, EdgeKind.CONTAINS, line)


def classify(
    base: Model, head: Model, correspondence: Correspondence | None = None
) -> list[ChangeEvent]:
    """The two stages together: the delta, then its cause links. The idiom of `test_diff`."""
    corr = correspondence or match_models(base, head)
    return classify_events(diff_models(base, head, corr), base, head, corr)


def by_subject(events: list[ChangeEvent], kind: ChangeEventKind, subject: str) -> ChangeEvent:
    matching = [e for e in events if e.kind is kind and e.subject == subject]
    assert len(matching) == 1, f"expected one {kind.value} on {subject}, got {len(matching)}"
    return matching[0]


def cause_of(events: list[ChangeEvent], kind: ChangeEventKind, subject: str) -> ChangeEvent | None:
    """The event named by `caused_by`, resolved -- so assertions read as claims about the
    cause rather than about an opaque digest."""
    caused_by = by_subject(events, kind, subject).caused_by
    if caused_by is None:
        return None
    return next(e for e in events if e.id == caused_by)


def roots_at(events: list[ChangeEvent], event: ChangeEvent) -> ChangeEvent:
    """Follow `caused_by` to the top of the chain. Depth is the chain, not a flat link."""
    seen: set[str] = set()
    while event.caused_by is not None and event.id not in seen:
        seen.add(event.id)
        event = next(e for e in events if e.id == event.caused_by)
    return event


def synthetic(kind: ChangeEventKind, subject: str, **payload: AttrValue) -> ChangeEvent:
    """An event the pipeline cannot currently produce.

    The unreachable rule in the table (§6.3) has no reachable input, and the temptation is to
    leave it untested until something emits its kind. Handing the
    driver the events it would then receive tests the rule now, the same way `test_diff`'s
    `paired` tests renames before tier 2 exists -- without any of it reaching the pipeline.
    """
    return ChangeEvent(
        kind=kind,
        subject=NodeId(subject),
        payload=dict(payload),
        confidence=Confidence(tier=None, unpaired=Unpaired.NONE, models_complete=True),
    )


class TestRemovingAContainerExplainsItsContents:
    """ARCH-001 §6.3: `NodeRemoved(C)` causes `NodeRemoved` for everything C contained.

    The one rule §6.3 gives that this pipeline can actually fire, and the one that carries
    the compression claim: deleting a class is one decision, not one event per member.
    """

    def test_a_removed_member_is_caused_by_its_removed_container(self) -> None:
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            edges=[holds(C, M)],
        )
        events = classify(base, model("b"))
        assert cause_of(events, ChangeEventKind.NODE_REMOVED, M) == by_subject(
            events, ChangeEventKind.NODE_REMOVED, C
        )

    def test_the_outermost_removal_is_the_one_with_no_cause(self) -> None:
        base = model(
            "a",
            node(N, name="ns", kind=NodeKind.NAMESPACE),
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            edges=[holds(N, C), holds(C, M)],
        )
        events = classify(base, model("b"))
        assert cause_of(events, ChangeEventKind.NODE_REMOVED, N) is None

    def test_depth_is_a_chain_and_every_removal_roots_at_the_namespace(self) -> None:
        """Removing a namespace removes classes which remove methods. Each link is to the
        *immediate* container, so render (§6.3) can nest three levels; a flat link to the
        outermost ancestor would collapse the structure into one list."""
        base = model(
            "a",
            node(N, name="ns", kind=NodeKind.NAMESPACE),
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            edges=[holds(N, C), holds(C, M)],
        )
        events = classify(base, model("b"))
        namespace = by_subject(events, ChangeEventKind.NODE_REMOVED, N)

        assert cause_of(events, ChangeEventKind.NODE_REMOVED, M) == by_subject(
            events, ChangeEventKind.NODE_REMOVED, C
        )
        assert cause_of(events, ChangeEventKind.NODE_REMOVED, C) == namespace
        assert all(roots_at(events, e) == namespace for e in events)

    def test_a_member_deleted_from_a_surviving_class_is_shown_not_blamed(self) -> None:
        """The container is still there, so nothing about it explains this. §6.3: an event
        with no matching rule gets no cause link and is shown."""
        klass = node(C, name="C", kind=NodeKind.CLASS)
        base = model("a", klass, node(M, name="m", kind=NodeKind.METHOD), edges=[holds(C, M)])
        events = classify(base, model("b", klass))
        assert cause_of(events, ChangeEventKind.NODE_REMOVED, M) is None

    def test_containment_is_read_from_the_base_model(self) -> None:
        """A removed node's container only exists at base. Reading head's containment would
        find nothing and quietly attribute no cause at all -- a stage that does nothing
        looks exactly like a stage with nothing to do."""
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            edges=[holds(C, M)],
        )
        head = model("b", node(F), node(G, name="g"), edges=[edge(F, G)])
        events = classify(base, head)
        assert cause_of(events, ChangeEventKind.NODE_REMOVED, M) is not None

    def test_an_added_class_explains_its_added_members(self) -> None:
        """§6.3's `contained-addition`. Without it a new class of twenty methods reads as
        twenty-one unexplained events while deleting the identical class reads as one."""
        head = model(
            "b",
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            edges=[holds(C, M)],
        )
        events = classify(model("a"), head)
        assert cause_of(events, ChangeEventKind.NODE_ADDED, M) == by_subject(
            events, ChangeEventKind.NODE_ADDED, C
        )


class TestAnEdgeIsExplainedByTheEndpointThatVanished:
    """ARCH-001 §6.3's `edge-lost-its-endpoint`, the rule that carries v0's actual cascades.

    `diff/edges.py` emits an edge event for every edge touching a removed node "rather than
    folded into the `NodeRemoved` for that endpoint ... a consequence is linked to its cause
    by classify". Without this rule, removing one class leaves every containment and call
    edge it touched as a top-level event nothing explains.
    """

    def test_an_edge_into_a_removed_node_is_caused_by_that_removal(self) -> None:
        base = model("a", node(F), node(G, name="g"), edges=[edge(F, G)])
        events = classify(base, model("b", node(F)))
        assert cause_of(events, ChangeEventKind.EDGE_REMOVED, f"{F}|calls|{G}") == by_subject(
            events, ChangeEventKind.NODE_REMOVED, G
        )

    def test_an_edge_losing_both_endpoints_is_attributed_to_its_source(self) -> None:
        """Two plausible causes, one `caused_by`. The source wins because an edge is written
        in its source's text -- the call site is in the caller's body, and `edge_hunks`
        carries exactly those lines as this event's provenance."""
        base = model("a", node(F), node(G, name="g"), edges=[edge(F, G)])
        events = classify(base, model("b"))
        assert cause_of(events, ChangeEventKind.EDGE_REMOVED, f"{F}|calls|{G}") == by_subject(
            events, ChangeEventKind.NODE_REMOVED, F
        )

    def test_a_dropped_call_between_two_surviving_nodes_has_no_cause(self) -> None:
        """The change itself, not a consequence of one. Attributing it to either endpoint
        would hide the most interesting kind of call-graph change under an unrelated node."""
        both = (node(F), node(G, name="g"))
        base = model("a", *both, edges=[edge(F, G)])
        events = classify(base, model("b", *both))
        assert cause_of(events, ChangeEventKind.EDGE_REMOVED, f"{F}|calls|{G}") is None

    def test_a_call_moving_between_overloads_is_explained_by_the_overload_it_moved_to(
        self,
    ) -> None:
        """The shape that survived every other rule on a real repository.

        googletest gave `AssertHelper::AssertHelper` a `std::string_view` overload and every
        `TEST` body moved to it. Both endpoints of the lost edge still exist -- the old
        overload is still declared, merely uncalled -- so nothing in the table reached it,
        and 28 of leveldb's 38 remaining top-level events were this one shape.
        """
        old = node("c:@F@h#c", name="h", qualified_name="ns::h", signature="void (const char *)")
        new = node("c:@F@h#s", name="h", qualified_name="ns::h", signature="void (string_view)")
        caller = node(F)

        base = model("a", caller, old, edges=[edge(F, old.id)])
        head = model("b", caller, old, new, edges=[edge(F, new.id)])
        events = classify(base, head)

        assert cause_of(events, ChangeEventKind.EDGE_REMOVED, f"{F}|calls|{old.id}") == by_subject(
            events, ChangeEventKind.EDGE_ADDED, f"{F}|calls|{new.id}"
        )

    def test_the_whole_chain_collapses_under_the_new_overload(self) -> None:
        """The `EdgeAdded` is itself caused by the new overload's `NodeAdded`, so a reviewer
        reads one fact -- an overload appeared -- with both edge events nested beneath it."""
        old = node("c:@F@h#c", name="h", qualified_name="ns::h", signature="void (const char *)")
        new = node("c:@F@h#s", name="h", qualified_name="ns::h", signature="void (string_view)")
        caller = node(F)
        events = classify(
            model("a", caller, old, edges=[edge(F, old.id)]),
            model("b", caller, old, new, edges=[edge(F, new.id)]),
        )
        assert cause_of(events, ChangeEventKind.EDGE_ADDED, f"{F}|calls|{new.id}") == by_subject(
            events, ChangeEventKind.NODE_ADDED, new.id
        )
        assert cause_of(events, ChangeEventKind.NODE_ADDED, new.id) is None, "the root of the chain"

    def test_a_call_that_moved_to_an_unrelated_function_is_not_explained_away(self) -> None:
        """The guard. The rule keys on `qualified_name`, which is what an overload set *is*;
        a caller that dropped one call and gained an unrelated one has made two changes, and
        nesting either under the other would hide a real one."""
        old = node("c:@F@h#c", name="h", qualified_name="ns::h")
        other = node("c:@F@k#", name="k", qualified_name="ns::k")
        caller = node(F)
        events = classify(
            model("a", caller, old, edges=[edge(F, old.id)]),
            model("b", caller, old, other, edges=[edge(F, other.id)]),
        )
        assert cause_of(events, ChangeEventKind.EDGE_REMOVED, f"{F}|calls|{old.id}") is None

    def test_a_removed_endpoint_still_outranks_an_overload_explanation(self) -> None:
        """Both rules can explain one `EdgeRemoved`, and table order decides. An endpoint
        that actually went away is the better account than one the caller stopped using.

        Two old overloads and one new one, so the name tier cannot tell which old one the new
        one is and pairs none (ADR-036); with one of each it would pair them as a signature
        change, and nothing would be removed."""
        old = node("c:@F@h#c", name="h", qualified_name="ns::h")
        other = node("c:@F@h#i", name="h", qualified_name="ns::h")
        new = node("c:@F@h#s", name="h", qualified_name="ns::h")
        caller = node(F)
        events = classify(
            model("a", caller, old, other, edges=[edge(F, old.id)]),
            model("b", caller, new, edges=[edge(F, new.id)]),
        )
        assert cause_of(events, ChangeEventKind.EDGE_REMOVED, f"{F}|calls|{old.id}") == by_subject(
            events, ChangeEventKind.NODE_REMOVED, old.id
        )

    def test_a_same_named_edge_of_a_different_kind_is_not_mistaken_for_an_overload(self) -> None:
        """The locator's own guard: "Same edge kind, and the overload set is `qualified_name`
        equality". A relation of a *different* kind to a same-named node is not a call that
        moved -- it is a second, unrelated fact about the caller, and nesting the dropped call
        under it would hide that fact rather than explain this one. Drop the `edge.kind is not
        removed.kind` check in `_overload_the_caller_moved_to` and this misfires."""
        old = node("c:@F@h#c", name="h", qualified_name="ns::h")
        same_name_other_relation = node("c:@F@h#s", name="h", qualified_name="ns::h")
        caller = node(F)
        events = classify(
            model("a", caller, old, edges=[edge(F, old.id)]),
            model(
                "b",
                caller,
                old,
                same_name_other_relation,
                edges=[edge(F, same_name_other_relation.id, kind=EdgeKind.OVERRIDES)],
            ),
        )
        assert cause_of(events, ChangeEventKind.EDGE_REMOVED, f"{F}|calls|{old.id}") is None

    def test_two_new_overloads_pick_the_same_cause_every_run(self) -> None:
        """§6.3 promises "a stable answer rather than one that depends on model construction"
        when a caller gains more than one same-named overload. Undetectable by running the
        pipeline once -- the bug this guards is nondeterminism, which only shows up as a flaky
        test or, worse, a report that differs between two runs of the same diff."""
        old = node("c:@F@h#c", name="h", qualified_name="ns::h")
        first = node("c:@F@h#1", name="h", qualified_name="ns::h")
        second = node("c:@F@h#2", name="h", qualified_name="ns::h")
        caller = node(F)
        base = model("a", caller, old, edges=[edge(F, old.id)])
        head = model(
            "b",
            caller,
            old,
            first,
            second,
            edges=[edge(F, first.id, line=2), edge(F, second.id, line=3)],
        )
        expected = min(f"{F}|calls|{first.id}", f"{F}|calls|{second.id}")

        for _ in range(5):
            events = classify(base, head)
            cause = cause_of(events, ChangeEventKind.EDGE_REMOVED, f"{F}|calls|{old.id}")
            assert cause == by_subject(events, ChangeEventKind.EDGE_ADDED, expected)

    def test_an_edge_onto_an_added_node_is_caused_by_that_addition(self) -> None:
        head = model("b", node(F), node(G, name="g"), edges=[edge(F, G)])
        events = classify(model("a", node(F)), head)
        assert cause_of(events, ChangeEventKind.EDGE_ADDED, f"{F}|calls|{G}") == by_subject(
            events, ChangeEventKind.NODE_ADDED, G
        )

    def test_the_endpoints_come_from_the_model_not_from_splitting_the_id(self) -> None:
        """`Edge.id` is `src|kind|dst` over USRs, and a USR is an opaque string from clang.
        A rule that split it would produce a *wrong* cause rather than a missing one."""
        weird_src = NodeId("c:@F@a|b#")
        base = model(
            "a",
            node(weird_src, name="a|b"),
            node(G, name="g"),
            edges=[edge(weird_src, G)],
        )
        events = classify(base, model("b"))
        assert cause_of(
            events, ChangeEventKind.EDGE_REMOVED, f"{weird_src}|calls|{G}"
        ) == by_subject(events, ChangeEventKind.NODE_REMOVED, weird_src)


class TestNothingIsCollapsed:
    """ARCH-001 §6.3: collapse happens at render, never here.

    The invariant that catches someone helpfully deduplicating. This stage adds links; the
    JSON always carries the full event set, and the view hides consequences under causes. A
    hidden real change is a far worse failure than a redundant one -- and an event dropped
    here is unrecoverable, because no downstream stage knows it existed.
    """

    def cascade(self) -> tuple[Model, Model]:
        members = [
            node(f"c:@N@ns@S@C@F@m{i}#", name=f"m{i}", kind=NodeKind.METHOD) for i in range(5)
        ]
        base = model(
            "a",
            node(N, name="ns", kind=NodeKind.NAMESPACE),
            node(C, name="C", kind=NodeKind.CLASS),
            *members,
            edges=[holds(N, C), *[holds(C, m.id) for m in members]],
        )
        return base, model("b")

    def test_the_event_count_is_identical_before_and_after(self) -> None:
        base, head = self.cascade()
        corr = match_models(base, head)
        events = diff_models(base, head, corr)
        assert len(classify_events(events, base, head, corr)) == len(events)

    def test_every_event_survives_with_its_kind_subject_and_payload(self) -> None:
        base, head = self.cascade()
        corr = match_models(base, head)
        events = diff_models(base, head, corr)
        classified = classify_events(events, base, head, corr)

        def shape(es: list[ChangeEvent]) -> list[tuple[str, str, str]]:
            return sorted((e.kind.value, e.subject, repr(sorted(e.payload.items()))) for e in es)

        assert shape(classified) == shape(events)

    def test_the_order_is_the_order_it_was_given(self) -> None:
        """Ordering is the diff stage's (content-sorted, §3) and ranking is §7's. A stage
        that reordered here would make the JSON depend on which stage last touched it."""
        base, head = self.cascade()
        corr = match_models(base, head)
        events = diff_models(base, head, corr)
        assert [e.id for e in classify_events(events, base, head, corr)] == [e.id for e in events]

    def test_a_large_cascade_still_has_exactly_one_uncaused_root(self) -> None:
        """The compression claim, in miniature: one decision, one thing to read, and every
        other event still present and reachable from it."""
        base, head = self.cascade()
        events = classify(base, head)
        assert len(events) == 13  # 7 nodes removed + 6 contains edges removed
        assert len([e for e in events if e.caused_by is None]) == 1


class TestAnEventNobodyCanExplainIsShown:
    """ARCH-001 §6.3: events with no matching rule get no cause link and are shown.

    None is a real answer here, not a gap to be filled later. Guessing at a cause from
    proximity or a shared file is exactly the inference the rule table replaces.
    """

    def test_a_body_change_keeps_no_cause_and_is_still_present(self) -> None:
        base = model("a", node(F, body_hash="aaa"))
        head = model("b", node(F, body_hash="bbb"))
        events = classify(base, head)
        assert [e.kind for e in events] == [ChangeEventKind.NODE_BODY_CHANGED]
        assert events[0].caused_by is None

    def test_an_attribute_change_keeps_no_cause(self) -> None:
        """`NodeAttrChanged` appears in §6.3 only as a *cause*. Nothing in the table makes
        it a consequence, and inventing one would be inference."""
        base = model("a", node(F, attrs={"virtual": False}))
        head = model("b", node(F, attrs={"virtual": True}))
        assert classify(base, head)[0].caused_by is None

    def test_a_relation_that_changed_kind_keeps_no_cause(self) -> None:
        both = (node(F), node(G, name="g"))
        base = model("a", *both, edges=[edge(F, G)])
        head = model("b", *both, edges=[edge(F, G, kind=EdgeKind.OVERRIDES)])
        events = classify(base, head)
        assert [e.kind for e in events] == [ChangeEventKind.EDGE_KIND_CHANGED]
        assert events[0].caused_by is None

    def test_a_moved_node_keeps_no_cause(self) -> None:
        """§6.3's table does not name `NodeMoved` anywhere -- not as a cause, not as a
        consequence, not even as one of the dormant or unreachable rows. A node whose
        container changed is diffed correctly (`diff/nodes.py`), but this stage has no row
        to explain *why*, and must not borrow the reasoning the table gives `NodeRemoved`'s
        and `NodeAdded`'s own unrelated neighbours for an event the table never mentions."""
        other_ns = NodeId("c:@N@other")
        klass = node(C, name="C", kind=NodeKind.CLASS)
        base = model("a", node(N, name="ns", kind=NodeKind.NAMESPACE), klass, edges=[holds(N, C)])
        head = model(
            "b",
            node(other_ns, name="other", kind=NodeKind.NAMESPACE),
            klass,
            edges=[holds(other_ns, C)],
        )
        events = classify(base, head)
        assert by_subject(events, ChangeEventKind.NODE_MOVED, C).caused_by is None

    def test_an_unrelated_change_beside_a_cascade_is_not_swept_into_it(self) -> None:
        """The failure that would make the compression metric a lie: everything in the diff
        hanging off one cause because the rule was "near enough"."""
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            node(F, body_hash="aaa"),
            edges=[holds(C, M)],
        )
        head = model("b", node(F, body_hash="bbb"))
        events = classify(base, head)
        assert by_subject(events, ChangeEventKind.NODE_BODY_CHANGED, F).caused_by is None


class TestACauseChainIsNeverACycle:
    """`caused_by` is a single optional id, so the link graph is a forest of chains -- and a
    cycle in it would make render hide two events under each other and show neither.

    The IR does not forbid a containment cycle, so the guard is not hypothetical. A refused
    link becomes no link rather than an error: §6.3 says default to showing.
    """

    def links(self, events: list[ChangeEvent]) -> dict[str, str]:
        return {e.id: e.caused_by for e in events if e.caused_by is not None}

    def assert_acyclic(self, events: list[ChangeEvent]) -> None:
        links = self.links(events)
        for start in links:
            seen: set[str] = set()
            current: str | None = start
            while current is not None:
                assert current not in seen, f"cause cycle through {current}"
                seen.add(current)
                current = links.get(current)

    def test_a_containment_cycle_produces_no_cause_cycle(self) -> None:
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            edges=[holds(C, M), holds(M, C)],
        )
        events = classify(base, model("b"))
        self.assert_acyclic(events)
        assert len([e for e in events if e.caused_by is None]) >= 1

    def test_a_containment_cycle_loses_no_event(self) -> None:
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            edges=[holds(C, M), holds(M, C)],
        )
        corr = match_models(base, model("b"))
        events = diff_models(base, model("b"), corr)
        assert len(classify_events(events, base, model("b"), corr)) == len(events)

    def test_a_node_containing_itself_is_not_its_own_cause(self) -> None:
        base = model("a", node(C, name="C", kind=NodeKind.CLASS), edges=[holds(C, C)])
        events = classify(base, model("b"))
        assert by_subject(events, ChangeEventKind.NODE_REMOVED, C).caused_by is None
        self.assert_acyclic(events)

    def test_a_deep_cascade_is_acyclic(self) -> None:
        depth = 8
        ids = [NodeId(f"c:@N@n{i}") for i in range(depth)]
        base = model(
            "a",
            *[node(i, name=f"n{k}", kind=NodeKind.NAMESPACE) for k, i in enumerate(ids)],
            edges=[holds(a, b) for a, b in itertools.pairwise(ids)],
        )
        events = classify(base, model("b"))
        self.assert_acyclic(events)
        assert len([e for e in events if e.caused_by is None]) == 1


class TestTheStageIsAPureFunction:
    """ARCH-001 §3. And one specific claim `events.py` makes on this stage's behalf:
    `ChangeEvent.id` excludes `caused_by` from its digest precisely so that linking a cause does
    not renumber an event that other events already point at."""

    def inputs(self) -> tuple[Model, Model, Correspondence]:
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            node(F, body_hash="aaa"),
            edges=[holds(C, M), edge(F, M)],
        )
        head = model("b", node(F, body_hash="bbb"))
        return base, head, match_models(base, head)

    def test_the_same_inputs_produce_the_same_links(self) -> None:
        base, head, corr = self.inputs()
        events = diff_models(base, head, corr)
        first = classify_events(events, base, head, corr)
        second = classify_events(events, base, head, corr)
        assert [(e.id, e.caused_by) for e in first] == [(e.id, e.caused_by) for e in second]

    def test_the_input_events_are_not_modified(self) -> None:
        base, head, corr = self.inputs()
        events = diff_models(base, head, corr)
        classified = classify_events(events, base, head, corr)
        assert any(e.caused_by is not None for e in classified)
        assert all(e.caused_by is None for e in events)

    def test_the_input_models_are_not_modified(self) -> None:
        base, head, corr = self.inputs()
        before = (base.fingerprint(), head.fingerprint())
        classify_events(diff_models(base, head, corr), base, head, corr)
        assert (base.fingerprint(), head.fingerprint()) == before

    def test_linking_a_cause_does_not_renumber_the_event(self) -> None:
        """The property `_derive_id` was written for. If ids moved, a cause link assigned
        early in the pass would point at an id that no longer exists by the end of it."""
        base, head, corr = self.inputs()
        events = diff_models(base, head, corr)
        assert [e.id for e in classify_events(events, base, head, corr)] == [e.id for e in events]

    def test_classifying_twice_changes_nothing(self) -> None:
        """The stage owns `caused_by` and derives it from the table alone, so it is
        idempotent -- which is what lets the pipeline be stopped and resumed at this
        boundary (§3)."""
        base, head, corr = self.inputs()
        once = classify_events(diff_models(base, head, corr), base, head, corr)
        twice = classify_events(once, base, head, corr)
        assert [(e.id, e.caused_by) for e in twice] == [(e.id, e.caused_by) for e in once]

    def test_links_go_through_the_correspondence_not_through_ids(self) -> None:
        """A container matched to a *differently named* head node has not been removed, so
        nothing it contained can be blamed on it. Comparing ids would see the base id absent
        from head and invent a removal cascade. Unreachable under tier 1 (the USR encodes
        the name); this is the correspondence tier 2 will hand the stage."""
        renamed_base = NodeId("c:@S@Old")
        renamed_head = NodeId("c:@S@New")
        member = NodeId("c:@S@Old@F@m#")
        base = model(
            "a",
            node(renamed_base, name="Old", kind=NodeKind.CLASS),
            node(member, name="m", kind=NodeKind.METHOD),
            edges=[holds(renamed_base, member)],
        )
        head = model("b", node(renamed_head, name="New", kind=NodeKind.CLASS))
        corr = Correspondence(
            matches=[Match(base=renamed_base, head=renamed_head, tier=MatchTier.STRUCTURAL)],
            unmatched_base=[member],
            unpaired_base={member: Unpaired.NONE},
            tiers_run=[MatchTier.DECLARED, MatchTier.STRUCTURAL],
        )
        events = classify(base, head, corr)
        assert by_subject(events, ChangeEventKind.NODE_REMOVED, member).caused_by is None


class TestTheRuleTableIsExplicit:
    """ARCH-001 §6.3: cause links come from an explicit rule table, not inference.

    These assert properties of the table itself, because a table that has quietly grown an
    entry nobody can trace to a design decision is the failure §6.3's word "explicit" is
    guarding against.
    """

    def test_every_rule_says_where_it_comes_from(self) -> None:
        for rule in CAUSE_RULES:
            assert rule.note, f"{rule.name} has no rationale"
            assert rule.arch, f"{rule.name} cites no ARCH section"

    def test_only_the_unreachable_row_cannot_fire(self) -> None:
        """One of §6.3's rows cannot fire. It is in the table -- deleting it would lose the
        argument about why -- and every other row is reachable from the events this pipeline
        emits, `signature-change-breaks-callers` since the derive stage (ADR-040)."""
        cannot_fire = {"rename-retargets-edges": RuleStatus.UNREACHABLE}
        for rule in CAUSE_RULES:
            assert rule.status is cannot_fire.get(rule.name, RuleStatus.ACTIVE), rule.name

    def test_every_rule_cites_its_arch_row(self) -> None:
        """ADR-023: §6.3 is the whole table, so a rule that cites no row is one the design
        cannot be read to explain. Asserted so that a rule added only in code is a visible
        failure here rather than a silent divergence."""
        uncited = {r.name for r in CAUSE_RULES if not (r.arch and r.arch.startswith("§6.3"))}
        assert uncited == set()

    def test_no_rule_names_a_kind_outside_the_closed_event_set(self) -> None:
        """§6.1: the event set is closed. A rule keyed on a kind nothing emits is a rule
        that silently never runs, which is why `RuleStatus` is on the record."""
        for rule in CAUSE_RULES:
            assert rule.cause in ChangeEventKind and rule.consequence in ChangeEventKind

    def test_no_dormant_or_unreachable_rule_fires_on_a_v0_delta(self) -> None:
        """If one ever does, either a later tier landed or a stage started emitting a kind
        §13 puts out of v0 -- both worth knowing about at the moment they happen."""
        base = model(
            "a",
            node(N, name="ns", kind=NodeKind.NAMESPACE),
            node(C, name="C", kind=NodeKind.CLASS),
            node(M, name="m", kind=NodeKind.METHOD),
            node(F, signature="void ()", body_hash="aaa"),
            edges=[holds(N, C), holds(C, M), edge(F, M)],
        )
        head = model("b", node(F, signature="void (int)", body_hash="bbb"))
        live = {r.consequence for r in CAUSE_RULES if r.status is RuleStatus.ACTIVE}
        assert {e.kind for e in classify(base, head) if e.caused_by is not None} <= live


class TestASignatureChangeExplainsEachCaller:
    """§6.3's `signature-change-breaks-callers` (ADR-040): each `CallerOfChangedSignature` is
    caused by the `NodeSignatureChanged` of the call's callee."""

    H = NodeId("c:@F@h#")

    def classified(self) -> list[ChangeEvent]:
        """`f` calls `g` and `h`, and both change signature: two causes, two callers."""
        base = model(
            "a",
            node(F),
            node(G, name="g", signature="void ()"),
            node(self.H, name="h", signature="int ()"),
            edges=[edge(F, G), edge(F, self.H, line=2)],
        )
        head = model(
            "b",
            node(F),
            node(G, name="g", signature="void (int)"),
            node(self.H, name="h", signature="long ()"),
            edges=[edge(F, G), edge(F, self.H, line=2)],
        )
        corr = match_models(base, head)
        derived = derive_events(diff_models(base, head, corr), base, head, corr)
        return classify_events(derived, base, head, corr)

    def test_the_cause_is_the_signature_change_of_the_subject_edges_callee(self) -> None:
        events = self.classified()
        for callee in (G, self.H):
            cause = cause_of(
                events, ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE, f"{F}|calls|{callee}"
            )
            assert cause is not None
            assert cause.kind is ChangeEventKind.NODE_SIGNATURE_CHANGED
            assert cause.subject == callee

    def test_an_attribute_change_on_the_callee_is_not_the_cause(self) -> None:
        """A `virtual` flip does not change what a call site was written against (ADR-035)."""
        head = model("b", node(F), node(G, name="g"), edges=[edge(F, G)])
        caller = ChangeEvent(
            kind=ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE,
            subject=edge(F, G).id,
            payload={
                "caller": F,
                "callee": G,
                "old_signature": "void ()",
                "new_signature": "void (int)",
                "caller_edited": False,
            },
            confidence=Confidence(tier=MatchTier.DECLARED, models_complete=True),
        )
        events = [
            synthetic(ChangeEventKind.NODE_ATTR_CHANGED, G, attr="virtual", old=False, new=True),
            caller,
        ]
        classified = classify_events(events, head, head, match_models(head, head))
        assert (
            cause_of(classified, ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE, caller.subject)
            is None
        )

    def test_classify_does_not_synthesize_the_derived_event_itself(self) -> None:
        """The line this stage does not cross: it links events and creates none. The derive
        stage, which runs before it, owns the caller events (ADR-040)."""
        base = model("a", node(F), node(G, name="g", signature="void ()"), edges=[edge(F, G)])
        head = model("b", node(F), node(G, name="g", signature="void (int)"), edges=[edge(F, G)])
        kinds = {e.kind for e in classify(base, head)}
        assert ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE not in kinds


class TestTheUnreachableRuleIsImplementedNotPretended:
    """The rule §6.3 names that this pipeline cannot reach.

    It is kept and implemented rather than deleted, because deleting it loses the argument
    about *why* it cannot fire. It is tested against events built by hand, so the table entry
    is code that works rather than a comment claiming it would.
    """

    def test_the_pipeline_never_emits_the_kind_the_unreachable_rule_keys_on(self) -> None:
        """The claim the test below must not be read as contradicting. `EdgeRetargeted` is
        refused by design (`diff/edges.py`), so it appears in no delta this pipeline produces."""
        base = model("a", node(F), node(G, name="g"), edges=[edge(F, G)])
        head = model("b", node(F), node(C, name="C", kind=NodeKind.CLASS), edges=[edge(F, C)])
        unreachable = {r.consequence for r in CAUSE_RULES if r.status is not RuleStatus.ACTIVE}
        assert {e.kind for e in classify(base, head)} & unreachable == set()

    def test_a_rename_would_explain_a_retargeted_edge(self) -> None:
        """§6.3's first rule, against the only input that could ever trigger it. It cannot
        arrive from this pipeline: an edge has no identity of its own, so `EdgeRetargeted`
        is refused, and edges compared in head coordinates mean a rename produces no edge
        events at all rather than 200 needing collapse."""
        head = model("b", node(F), node(G, name="g"), edges=[edge(F, G)])
        events = [
            synthetic(ChangeEventKind.NODE_RENAMED, G, old_name="old", new_name="g"),
            synthetic(
                ChangeEventKind.EDGE_RETARGETED, f"{F}|calls|{G}", old_dst="c:@F@was#", new_dst=G
            ),
        ]
        corr = match_models(head, head)
        classified = classify_events(events, head, head, corr)
        cause = cause_of(classified, ChangeEventKind.EDGE_RETARGETED, f"{F}|calls|{G}")
        assert cause is not None and cause.kind is ChangeEventKind.NODE_RENAMED
