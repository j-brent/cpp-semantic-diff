"""The delta -- ARCH-001 §6.

The diff stage is where a wrong answer is least visible: it produces well-formed events
either way, and a reviewer reading them has no way to tell that the entity moved file, that
the edge churn is an artifact of comparing raw ids, or that a rename was quietly inferred.
So every class below names the design claim it defends rather than the mechanics that
currently satisfy it.

Models are built by hand. Nothing here needs libclang, a compile database or a fixture
clone: the stage is a pure function of two models and a correspondence (§3), which is most
of the reason it is one.
"""

from __future__ import annotations

from typing import Any

import pytest

from cpp_semantic_diff.diff import diff_models
from cpp_semantic_diff.errors import PreconditionError, ToolchainMismatchError
from cpp_semantic_diff.match import Correspondence, Match, match_models
from cpp_semantic_diff.model import (
    UNREACHABLE_IN_V0,
    V0_KINDS,
    Completeness,
    Edge,
    EdgeKind,
    Event,
    EventKind,
    Location,
    MatchTier,
    Model,
    Node,
    NodeId,
    NodeKind,
    Side,
    TuResult,
    TuStatus,
)

F = NodeId("c:@F@f#")
G = NodeId("c:@F@g#")
C = NodeId("c:@S@C")


def node(usr: str, name: str = "f", **kw: Any) -> Node:
    defaults = {"kind": NodeKind.FUNCTION, "name": name, "qualified_name": name}
    return Node(id=NodeId(usr), **{**defaults, **kw})


def model(rev: str, *nodes: Node, edges: list[Edge] | None = None, **kw: Any) -> Model:
    m = Model(rev=rev, edges=edges or [], **kw)
    for n in nodes:
        m.add_node(n)
    return m


def calls(src: str, dst: str, line: int = 1, **kw: Any) -> Edge:
    return Edge(
        src=NodeId(src),
        dst=NodeId(dst),
        kind=kw.pop("kind", EdgeKind.CALLS),
        locations=[Location(file="src/a.cpp", line=line)],
        **kw,
    )


def diff(base: Model, head: Model, correspondence: Correspondence | None = None) -> list[Event]:
    return diff_models(base, head, correspondence or match_models(base, head))


def kinds(events: list[Event]) -> list[str]:
    return sorted(e.kind.value for e in events)


def only(events: list[Event], kind: EventKind) -> Event:
    matching = [e for e in events if e.kind is kind]
    assert len(matching) == 1, f"expected one {kind.value}, got {kinds(events)}"
    return matching[0]


def paired(base: Model, head: Model, *pairs: tuple[str, str], tier: MatchTier) -> Correspondence:
    """A correspondence a later tier could produce -- tier 1 never will.

    Renames and moves are unreachable in v0 (ARCH-001 §13), and the temptation is to leave
    their handling untested until tiers 2-4 land. Handing the stage the correspondence it would
    then receive tests the handling now, without any of it leaking into the matcher.
    """
    matched_base = {b for b, _ in pairs}
    matched_head = {h for _, h in pairs}
    return Correspondence(
        matches=[Match(base=NodeId(b), head=NodeId(h), tier=tier) for b, h in pairs],
        unmatched_base=sorted(n for n in base.nodes if n not in matched_base),
        unmatched_head=sorted(n for n in head.nodes if n not in matched_head),
        tiers_run=[MatchTier.DECLARED, tier],
    )


class TestNothingChangedMeansNoEvents:
    """The cheapest whole-pipeline correctness check there is.

    `Model.fingerprint()` exists (ir.py) to say that two models carry the same identity and
    the same diffable state. If such a pair produces even one event, something in this
    stage is comparing provenance, or comparing ids it should be comparing entities.
    """

    def test_two_identical_models_produce_zero_events(self) -> None:
        def side(rev: str) -> Model:
            return model(
                rev,
                node(F, attrs={"virtual": True, "const": False}, body_hash="aaa"),
                node(G, name="g", attrs={}, body_hash="bbb"),
                edges=[calls(F, G), calls(F, G, kind=EdgeKind.CONTAINS)],
            )

        base, head = side("a"), side("b")
        assert base.fingerprint() == head.fingerprint()
        assert diff(base, head) == []

    def test_a_node_that_only_moved_file_produces_no_event(self) -> None:
        """ARCH-001 §4.1: location is provenance, never identity. An entity that moved is
        the same entity, and the delta must say nothing about it at all."""
        base = model("a", node(F, location=Location(file="src/old.cpp", line=1, end_line=9)))
        head = model("b", node(F, location=Location(file="lib/new.cpp", line=402, end_line=410)))
        assert base.fingerprint() == head.fingerprint()
        assert diff(base, head) == []

    def test_an_edge_that_only_moved_produces_no_event(self) -> None:
        """Same claim for the call site: an edge's `locations` are provenance (§4.2), so a
        call that moved down the file is not a change to the call graph."""
        base = model("a", node(F), node(G, name="g"), edges=[calls(F, G, line=10)])
        head = model("b", node(F), node(G, name="g"), edges=[calls(F, G, line=99)])
        assert diff(base, head) == []


class TestRenamesLookBadOnPurpose:
    """ARCH-001 §13: v0 ships tier 1 only and measures the damage.

    This pins the decision at the stage most likely to soften it. A name-based fallback
    here would make the output look better and destroy the measurement that decides whether
    tiers 2-4 are worth building -- while also relocating an identity decision out of the
    matcher, where the next person would never look for it.
    """

    def test_a_renamed_function_is_a_remove_plus_an_add_and_not_a_rename(self) -> None:
        base = model("a", node("c:@F@compute#", name="compute", body_hash="same"))
        head = model("b", node("c:@F@calculate#", name="calculate", body_hash="same"))
        events = diff(base, head)
        assert kinds(events) == ["NodeAdded", "NodeRemoved"]
        assert only(events, EventKind.NODE_REMOVED).subject == "c:@F@compute#"
        assert only(events, EventKind.NODE_ADDED).subject == "c:@F@calculate#"

    def test_the_two_halves_of_a_rename_carry_no_tier(self) -> None:
        """ARCH-001 §11. Neither half rests on a match, so neither may claim one."""
        base = model("a", node("c:@F@compute#", name="compute"))
        head = model("b", node("c:@F@calculate#", name="calculate"))
        assert {e.confidence.tier for e in diff(base, head)} == {None}

    def test_a_rename_becomes_one_event_once_a_tier_produces_the_match(self) -> None:
        """The kind is implemented against what tier 2 will hand it, not against tier 1.

        Delete the `paired` scaffolding when tier 2 lands; the assertion stays.
        """
        base = model("a", node("c:@F@compute#", name="compute", body_hash="same"))
        head = model("b", node("c:@F@calculate#", name="calculate", body_hash="same"))
        corr = paired(base, head, ("c:@F@compute#", "c:@F@calculate#"), tier=MatchTier.STRUCTURAL)
        event = only(diff(base, head, corr), EventKind.NODE_RENAMED)
        assert event.payload == {"old_name": "compute", "new_name": "calculate"}
        assert event.subject == "c:@F@calculate#"
        assert event.confidence.tier is MatchTier.STRUCTURAL


class TestAMoveIsAChangeOfContainerNotOfFile:
    """ARCH-001 §6.1: `NodeMoved` means the parent changed. Not the file -- that is §4.1's
    provenance, and the tests above say it produces nothing.

    Unreachable in v0 for the same reason `NodeRenamed` is: the USR encodes the enclosing
    scope, so moving a member from one class to another changes its identity and the pair
    is never matched. These hold the child's id fixed to exercise the comparison the way a
    later tier will drive it.
    """

    CHILD = NodeId("c:@S@C@F@m#")

    def child(self) -> Node:
        return node(self.CHILD, name="m", kind=NodeKind.METHOD)

    def test_a_node_whose_container_is_a_different_entity_has_moved(self) -> None:
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            self.child(),
            edges=[calls(C, self.CHILD, kind=EdgeKind.CONTAINS)],
        )
        head = model(
            "b",
            node("c:@S@D", name="D", kind=NodeKind.CLASS),
            self.child(),
            edges=[calls("c:@S@D", self.CHILD, kind=EdgeKind.CONTAINS)],
        )
        event = only(diff(base, head), EventKind.NODE_MOVED)
        assert event.payload == {"old_parent": C, "new_parent": "c:@S@D"}

    def test_a_member_of_a_renamed_container_has_not_moved(self) -> None:
        """The comparison goes through the correspondence, not through ids. Comparing ids
        would report every member of a renamed class as moved and every containment edge as
        churn -- one rename, two events per member."""
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            self.child(),
            edges=[calls(C, self.CHILD, kind=EdgeKind.CONTAINS)],
        )
        head = model(
            "b",
            node("c:@S@D", name="D", kind=NodeKind.CLASS),
            self.child(),
            edges=[calls("c:@S@D", self.CHILD, kind=EdgeKind.CONTAINS)],
        )
        corr = paired(
            base,
            head,
            (C, "c:@S@D"),
            (self.CHILD, self.CHILD),
            tier=MatchTier.STRUCTURAL,
        )
        assert kinds(diff(base, head, corr)) == ["NodeRenamed"]


class TestOneAttributeChangeIsOneEvent:
    """ARCH-001 §4.3 and §6.1: an attribute diff is mechanical and names its attribute.

    The failure to guard against is a single lumped "attributes changed" event, or an
    attribute that disappears being lost because only the keys present on both sides were
    compared.
    """

    def test_one_changed_attribute_names_itself_and_nothing_else(self) -> None:
        base = model("a", node(F, attrs={"virtual": False, "const": True}))
        head = model("b", node(F, attrs={"virtual": True, "const": True}))
        event = only(diff(base, head), EventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "virtual", "old": False, "new": True}

    def test_two_changed_attributes_are_two_events(self) -> None:
        base = model("a", node(F, attrs={"virtual": False, "access": "public"}))
        head = model("b", node(F, attrs={"virtual": True, "access": "private"}))
        assert {e.payload["attr"] for e in diff(base, head)} == {"virtual", "access"}

    def test_an_attribute_that_appears_is_a_change(self) -> None:
        base = model("a", node(F, attrs={}))
        head = model("b", node(F, attrs={"virtual": True}))
        event = only(diff(base, head), EventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "virtual", "old": None, "new": True}

    def test_an_attribute_that_disappears_is_a_change(self) -> None:
        """The one that hides behind a falsy test. `virtual` dropped and `virtual` set to
        false are the same shape, and `if not old` would swallow both."""
        base = model("a", node(F, attrs={"virtual": False}))
        head = model("b", node(F, attrs={}))
        event = only(diff(base, head), EventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "virtual", "old": False, "new": None}

    def test_absent_and_explicitly_none_are_the_same_state(self) -> None:
        """`AttrValue` admits None (§4.3) and the IR gives no way to tell "absent" from
        "present and None". Reporting a change between them would invent one."""
        base = model("a", node(F, attrs={"exception_spec": None}))
        head = model("b", node(F, attrs={}))
        assert diff(base, head) == []

    def test_a_value_that_changes_type_is_a_change(self) -> None:
        """`False == 0` in Python, so a producer flipping an attribute's type would slip
        past an `==` comparison -- exactly the sort of drift that is invisible later."""
        base = model("a", node(F, attrs={"bitfield_width": False}))
        head = model("b", node(F, attrs={"bitfield_width": 0}))
        assert only(diff(base, head), EventKind.NODE_ATTR_CHANGED).payload["new"] == 0


class TestABodyChangeIsNotAnAttributeChange:
    """ARCH-001 §4.1: `body_hash` is deliberately outside `attrs`.

    An attribute change has a specific known meaning; a body change only means "look
    closer". Collapsing the two would give rank (§7) one weight for both and render one
    presentation for both.
    """

    def test_a_differing_body_hash_is_exactly_one_body_event(self) -> None:
        base = model("a", node(F, body_hash="aaa"))
        head = model("b", node(F, body_hash="bbb"))
        assert kinds(diff(base, head)) == ["NodeBodyChanged"]

    def test_a_declaration_gaining_a_definition_is_not_a_body_change(self) -> None:
        """No body existed, so nothing inside it changed. It is a `defines`-edge fact in
        §4.2's vocabulary and §13 keeps that edge kind out of v0 -- and from the models
        alone it is indistinguishable from the TU holding the definition having failed to
        parse on one side, which §11 forbids reporting as a finding."""
        base = model("a", node(F, body_hash=None))
        head = model("b", node(F, body_hash="aaa"))
        assert diff(base, head) == []
        assert diff(head, base) == []

    def test_a_body_change_and_an_attribute_change_are_separate_events(self) -> None:
        base = model("a", node(F, attrs={"const": True}, body_hash="aaa"))
        head = model("b", node(F, attrs={"const": False}, body_hash="bbb"))
        assert kinds(diff(base, head)) == ["NodeAttrChanged", "NodeBodyChanged"]


class TestEdgesAreMatchedThroughTheCorrespondence:
    """ARCH-001 §5 and §6.1. The most expensive mistake available in this stage.

    An edge has no identity of its own, so `A -> B` at base is the same edge as `A' -> B'`
    at head exactly when the matcher paired A with A' and B with B'. Compare raw ids
    instead and every edge on every renamed node reads as churn -- which passes every
    single-node test in this file, because in v0 the two ids happen to be equal.
    """

    def test_an_unchanged_edge_between_two_matched_nodes_is_not_churn(self) -> None:
        base = model("a", node(F), node(G, name="g"), edges=[calls(F, G)])
        head = model("b", node(F), node(G, name="g"), edges=[calls(F, G)])
        assert diff(base, head) == []

    def test_an_edge_between_two_renamed_nodes_is_still_the_same_edge(self) -> None:
        """The test that only fails if endpoints are compared as strings. Both endpoints
        change id; the correspondence says both are the same entities; the call graph did
        not change, so the delta must be silent about it."""
        base = model(
            "a",
            node("c:@F@a#", name="a"),
            node("c:@F@b#", name="b"),
            edges=[calls("c:@F@a#", "c:@F@b#")],
        )
        head = model(
            "b",
            node("c:@F@x#", name="x"),
            node("c:@F@y#", name="y"),
            edges=[calls("c:@F@x#", "c:@F@y#")],
        )
        corr = paired(
            base,
            head,
            ("c:@F@a#", "c:@F@x#"),
            ("c:@F@b#", "c:@F@y#"),
            tier=MatchTier.SIMILARITY,
        )
        assert kinds(diff(base, head, corr)) == ["NodeRenamed", "NodeRenamed"]

    def test_a_dropped_call_between_two_matched_nodes_is_one_removal(self) -> None:
        base = model("a", node(F), node(G, name="g"), edges=[calls(F, G)])
        head = model("b", node(F), node(G, name="g"))
        event = only(diff(base, head), EventKind.EDGE_REMOVED)
        assert event.subject == f"{F}|calls|{G}"
        assert [h.side for h in event.provenance] == [Side.BASE]

    def test_an_edge_to_an_unmatched_node_is_reported_not_suppressed(self) -> None:
        """It is a consequence of the node event, and ARCH-001 §6.3 puts collapse at render
        and cause attribution in classify. Dropping it here would delete a real change from
        the JSON that every view reads."""
        base = model("a", node(F), node(G, name="g"), edges=[calls(F, G)])
        head = model("b", node(F))
        assert kinds(diff(base, head)) == ["EdgeRemoved", "NodeRemoved"]

    def test_a_retarget_is_a_remove_plus_an_add_not_a_retarget(self) -> None:
        """`EdgeRetargeted` would assert that the call to G *became* the call to H. G is
        matched to G and H to H; claiming G became H is a node-identity claim the matcher
        declined to make, made here instead on weaker evidence (§5)."""
        both = (node(F), node(G, name="g"), node("c:@F@h#", name="h"))
        base = model("a", *both, edges=[calls(F, G)])
        head = model("b", *both, edges=[calls(F, "c:@F@h#")])
        assert kinds(diff(base, head)) == ["EdgeAdded", "EdgeRemoved"]

    def test_a_relation_that_changes_kind_between_the_same_pair_is_one_event(self) -> None:
        """The endpoints are identity and the matcher already paired them; the kind is
        state. No new identity claim is needed, so this one is reportable as a change."""
        both = (node(F), node(G, name="g"))
        base = model("a", *both, edges=[calls(F, G)])
        head = model("b", *both, edges=[calls(F, G, kind=EdgeKind.OVERRIDES)])
        event = only(diff(base, head), EventKind.EDGE_KIND_CHANGED)
        assert event.payload == {"old_kind": "calls", "new_kind": "overrides"}
        assert {h.side for h in event.provenance} == {Side.BASE, Side.HEAD}

    def test_an_ambiguous_kind_change_falls_back_to_remove_plus_add(self) -> None:
        """Two residual edges between one pair on each side: which became which is a coin
        flip, and `match.declared._index` sets the precedent -- a key two things share
        identifies neither."""
        both = (node(F), node(G, name="g"))
        base = model(
            "a",
            *both,
            edges=[calls(F, G, kind=EdgeKind.CALLS), calls(F, G, kind=EdgeKind.INHERITS)],
        )
        head = model(
            "b",
            *both,
            edges=[calls(F, G, kind=EdgeKind.OVERRIDES), calls(F, G, kind=EdgeKind.INSTANTIATES)],
        )
        assert kinds(diff(base, head)) == [
            "EdgeAdded",
            "EdgeAdded",
            "EdgeRemoved",
            "EdgeRemoved",
        ]


class TestAnEdgeAttributeChangeIsAChange:
    """ARCH-001 §4.2 gives edges `attrs`; §6.1's rule is that a new signal becomes a new
    kind. `EdgeAttrChanged` is where those two meet.

    The case that matters is `access` on an `inherits` edge. A base class going public ->
    private is a breaking change in C++ -- every conversion from derived to base outside the
    class stops compiling -- and it is expressible *nowhere else* in the closed set: both
    classes are unchanged, so no node event fires, and the edge is present on both sides
    with the same kind, so no add, remove or kind change fires either. Before this kind
    existed the diff stage matched the edge, found it equal on everything it looked at, and
    said nothing.
    """

    BASE_CLASS = NodeId("c:@S@Base")
    DERIVED = NodeId("c:@S@Derived")

    def sides(self, before: str, after: str) -> tuple[Model, Model]:
        both = (
            node(self.BASE_CLASS, name="Base", kind=NodeKind.CLASS),
            node(self.DERIVED, name="Derived", kind=NodeKind.CLASS),
        )
        return (
            model(
                "a",
                *both,
                edges=[
                    calls(
                        self.DERIVED,
                        self.BASE_CLASS,
                        kind=EdgeKind.INHERITS,
                        attrs={"access": before},
                    )
                ],
            ),
            model(
                "b",
                *both,
                edges=[
                    calls(
                        self.DERIVED,
                        self.BASE_CLASS,
                        kind=EdgeKind.INHERITS,
                        attrs={"access": after},
                    )
                ],
            ),
        )

    def test_a_base_class_going_private_is_reported(self) -> None:
        base, head = self.sides("public", "private")
        event = only(diff(base, head), EventKind.EDGE_ATTR_CHANGED)
        assert event.payload == {"attr": "access", "old": "public", "new": "private"}
        assert event.subject == f"{self.DERIVED}|inherits|{self.BASE_CLASS}"

    def test_an_unchanged_attribute_is_silent(self) -> None:
        base, head = self.sides("public", "public")
        assert diff(base, head) == []

    def test_the_event_points_at_both_sides(self) -> None:
        """§6.2: the reviewer has to reach the old attribute as well as the new one."""
        base, head = self.sides("public", "private")
        event = only(diff(base, head), EventKind.EDGE_ATTR_CHANGED)
        assert {h.side for h in event.provenance} == {Side.BASE, Side.HEAD}

    def test_the_comparison_is_the_one_nodes_already_use(self) -> None:
        """The rule is shared with `diff/nodes.py` (`diff/attrs.py`), and these are the two
        properties that a second, subtly different implementation would drop in silence: an
        attribute present on one side only, and a value that changed type where `False == 0`
        would have compared equal."""
        appeared = model(
            "a", node(F), node(G, name="g"), edges=[calls(F, G, attrs={"virtual": False})]
        )
        vanished = model("b", node(F), node(G, name="g"), edges=[calls(F, G)])
        assert only(diff(appeared, vanished), EventKind.EDGE_ATTR_CHANGED).payload == {
            "attr": "virtual",
            "old": False,
            "new": None,
        }

        flipped = model("b", node(F), node(G, name="g"), edges=[calls(F, G, attrs={"virtual": 0})])
        assert only(diff(appeared, flipped), EventKind.EDGE_ATTR_CHANGED).payload["new"] == 0

    def test_two_changed_attributes_on_one_edge_are_two_events(self) -> None:
        """One event per attribute, each naming its own -- the same shape §6.1 gives
        `NodeAttrChanged`, so rank and render need no second presentation."""
        both = (node(F), node(G, name="g"))
        base = model("a", *both, edges=[calls(F, G, attrs={"virtual": False, "access": "public"})])
        head = model("b", *both, edges=[calls(F, G, attrs={"virtual": True, "access": "private"})])
        assert {e.payload["attr"] for e in diff(base, head)} == {"virtual", "access"}

    def test_an_added_edge_does_not_also_report_its_attributes(self) -> None:
        """The attributes of an edge that did not exist before are not a change to them.
        Emitting both would double-count one change and give classify two events to link."""
        base = model("a", node(F), node(G, name="g"))
        head = model("b", node(F), node(G, name="g"), edges=[calls(F, G, attrs={"virtual": True})])
        assert kinds(diff(base, head)) == ["EdgeAdded"]

    def test_it_travels_through_the_correspondence_like_every_other_edge_event(self) -> None:
        """The endpoints are compared as entities, not as ids (see the class above). An
        attribute change on an edge between two *renamed* classes is still one event."""
        base = model(
            "a",
            node("c:@S@A", name="A", kind=NodeKind.CLASS),
            node("c:@S@B", name="B", kind=NodeKind.CLASS),
            edges=[calls("c:@S@B", "c:@S@A", kind=EdgeKind.INHERITS, attrs={"access": "public"})],
        )
        head = model(
            "b",
            node("c:@S@X", name="X", kind=NodeKind.CLASS),
            node("c:@S@Y", name="Y", kind=NodeKind.CLASS),
            edges=[calls("c:@S@Y", "c:@S@X", kind=EdgeKind.INHERITS, attrs={"access": "private"})],
        )
        corr = paired(
            base, head, ("c:@S@A", "c:@S@X"), ("c:@S@B", "c:@S@Y"), tier=MatchTier.STRUCTURAL
        )
        events = diff(base, head, corr)
        assert kinds(events) == ["EdgeAttrChanged", "NodeRenamed", "NodeRenamed"]
        assert only(events, EventKind.EDGE_ATTR_CHANGED).confidence.tier is MatchTier.STRUCTURAL


class TestTheEventSetIsComplete:
    """ARCH-001 §6.3: cascade collapse happens at render, never at diff.

    The JSON always carries the full set; the view hides consequences under causes. A
    hidden real change is a far worse failure than a redundant one, so this stage emits
    every consequence and links none of them.
    """

    def test_removing_a_container_emits_every_consequence(self) -> None:
        members = [node(f"c:@S@C@F@m{i}#", name=f"m{i}", kind=NodeKind.METHOD) for i in range(3)]
        base = model(
            "a",
            node(C, name="C", kind=NodeKind.CLASS),
            *members,
            edges=[calls(C, m.id, kind=EdgeKind.CONTAINS) for m in members],
        )
        events = diff(base, model("b"))
        assert kinds(events) == ["EdgeRemoved"] * 3 + ["NodeRemoved"] * 4

    def test_no_event_carries_a_cause(self) -> None:
        """`caused_by` belongs to classify (§6.3). Setting it here would put the rule table
        in the stage that has no access to it."""
        base = model("a", node(F, body_hash="aaa"), node(G, name="g"))
        head = model("b", node(F, body_hash="bbb"))
        events = diff(base, head)
        assert events and all(e.caused_by is None for e in events)

    def test_a_v0_delta_stays_inside_the_v0_event_set(self) -> None:
        """ARCH-001 §13. Under tier-1 identity a matched pair agrees on name and on
        enclosing scope by construction -- the USR encodes both -- so no amount of real
        input produces `NodeRenamed` or `NodeMoved`, and `EdgeRetargeted` is refused
        outright. If this fails, either a later tier landed or something here started
        inferring identity.

        **`V0_KINDS` was widened, so this asserts the exclusion directly as well.** The
        constant used to name edge add/remove only, which left `EdgeKindChanged` -- reachable
        on tier-1 input, and now `EdgeAttrChanged` with it -- outside a set this test claimed
        the delta stayed inside. The subset check was passing against a set with room to
        spare. Naming what must *not* appear keeps the test meaningful after the widening,
        and the input below now reaches both of the newly-named kinds so the set is not
        merely a superset.
        """
        both = (node(C, name="C", kind=NodeKind.CLASS),)
        base = model(
            "a",
            node(F, attrs={"virtual": False}, body_hash="aaa"),
            node(G, name="g"),
            *both,
            edges=[calls(F, G), calls(F, C, attrs={"virtual": False})],
        )
        head = model(
            "b",
            node(F, attrs={"virtual": True}, body_hash="bbb"),
            node(G, name="g"),
            *both,
            edges=[
                calls(F, G, kind=EdgeKind.OVERRIDES),
                calls(F, C, attrs={"virtual": True}),
            ],
        )
        produced = {e.kind for e in diff(base, head)}
        assert produced <= V0_KINDS
        assert not produced & UNREACHABLE_IN_V0
        assert {EventKind.EDGE_KIND_CHANGED, EventKind.EDGE_ATTR_CHANGED} <= produced


class TestConfidenceIsNeverLaundered:
    """ARCH-001 §11: confidence comes from the match tier and the completeness of *both*
    models, and an event that rests on nothing must say so."""

    def test_a_matched_node_reports_the_tier_that_matched_it(self) -> None:
        base = model("a", node(F, body_hash="aaa"))
        head = model("b", node(F, body_hash="bbb"))
        assert only(diff(base, head), EventKind.NODE_BODY_CHANGED).confidence.tier is (
            MatchTier.DECLARED
        )

    def test_an_incomplete_model_on_either_side_taints_every_event(self) -> None:
        """A model that failed to parse a TU cannot support a confident claim about
        anything -- including entities in the TUs that did parse, since what is missing may
        be exactly what would have contradicted the event."""
        broken = Completeness(
            tus_total=2,
            tus=[
                TuResult(path="a.cpp", status=TuStatus.PARSED),
                TuResult(path="b.cpp", status=TuStatus.FAILED, detail="stddef.h not found"),
            ],
        )
        whole = Completeness(tus_total=1, tus=[TuResult(path="a.cpp", status=TuStatus.PARSED)])
        base = model("a", node(F, body_hash="aaa"), completeness=broken)
        head = model("b", node(F, body_hash="bbb"), completeness=whole)
        assert only(diff(base, head), EventKind.NODE_BODY_CHANGED).confidence.models_complete is (
            False
        )

    def test_an_edge_is_no_more_confident_than_its_weaker_endpoint(self) -> None:
        """An edge event is a claim about two entities. Taking the stronger tier -- or the
        source's -- would report a similarity guess with tier-1 authority."""
        base = model(
            "a",
            node("c:@F@a#", name="a"),
            node("c:@F@b#", name="b"),
            edges=[calls("c:@F@a#", "c:@F@b#")],
        )
        head = model("b", node("c:@F@a#", name="a"), node("c:@F@y#", name="y"))
        corr = Correspondence(
            matches=[
                Match(base=NodeId("c:@F@a#"), head=NodeId("c:@F@a#"), tier=MatchTier.DECLARED),
                Match(base=NodeId("c:@F@b#"), head=NodeId("c:@F@y#"), tier=MatchTier.SIMILARITY),
            ],
            tiers_run=[MatchTier.DECLARED, MatchTier.SIMILARITY],
        )
        assert only(diff(base, head, corr), EventKind.EDGE_REMOVED).confidence.tier is (
            MatchTier.SIMILARITY
        )

    def test_an_edge_with_an_unmatched_endpoint_has_no_tier(self) -> None:
        base = model("a", node(F), node(G, name="g"), edges=[calls(F, G)])
        head = model("b", node(F))
        assert only(diff(base, head), EventKind.EDGE_REMOVED).confidence.tier is None


class TestProvenanceRunsBothWays:
    """ARCH-001 §6.2. The reviewer navigates from an event to the lines *and* back. An
    event about two sides that carries one hunk is a one-way link."""

    def test_a_two_sided_event_carries_a_hunk_from_each_side(self) -> None:
        base = model("a", node(F, body_hash="aaa", location=Location(file="a.h", line=3)))
        head = model("b", node(F, body_hash="bbb", location=Location(file="a.h", line=91)))
        event = only(diff(base, head), EventKind.NODE_BODY_CHANGED)
        assert [(h.side, h.location.line) for h in event.provenance] == [
            (Side.BASE, 3),
            (Side.HEAD, 91),
        ]

    def test_a_one_sided_event_carries_only_that_side(self) -> None:
        base = model("a", node(F, location=Location(file="a.h", line=3)))
        event = only(diff(base, model("b")), EventKind.NODE_REMOVED)
        assert [(h.side, h.location.file) for h in event.provenance] == [(Side.BASE, "a.h")]

    def test_every_call_site_of_an_edge_is_a_hunk(self) -> None:
        """A function calling another twice is one edge with two locations (§4.2). Both
        lines must point back at the event, or the reverse index has a hole."""
        edge = Edge(
            src=F,
            dst=G,
            kind=EdgeKind.CALLS,
            locations=[Location(file="a.cpp", line=4), Location(file="a.cpp", line=17)],
        )
        base = model("a", node(F), node(G, name="g"), edges=[edge])
        head = model("b", node(F), node(G, name="g"))
        assert len(only(diff(base, head), EventKind.EDGE_REMOVED).provenance) == 2


class TestTheStageIsAPureFunction:
    """ARCH-001 §3. Purity is what makes the delta cacheable, testable from hand-built
    models, and reproducible in the JSON contract (§10)."""

    def test_the_same_inputs_produce_the_same_events_in_the_same_order(self) -> None:
        base = model("a", node(F, body_hash="aaa"), node(G, name="g"), edges=[calls(F, G)])
        head = model("b", node(F, body_hash="bbb"), node(C, name="C", kind=NodeKind.CLASS))
        first = [e.id for e in diff(base, head)]
        assert first == [e.id for e in diff(base, head)]
        # Content-ordered, not construction-ordered: a delta whose order came from dict
        # iteration would produce a different JSON document (§10) for the same two commits.
        assert kinds(diff(base, head)) == [e.kind.value for e in diff(base, head)]

    def test_the_inputs_are_not_modified(self) -> None:
        base = model("a", node(F, attrs={"virtual": False}, body_hash="aaa"))
        head = model("b", node(F, attrs={"virtual": True}, body_hash="bbb"))
        before = (base.fingerprint(), head.fingerprint())
        diff(base, head)
        assert (base.fingerprint(), head.fingerprint()) == before

    def test_a_correspondence_that_leaves_a_node_unaccounted_for_is_refused(self) -> None:
        """A node in neither the matches nor the residue produces no event at all -- the
        §11 failure with no symptom. `Correspondence` cannot catch it because it never sees
        the models, so this stage does."""
        base = model("a", node(F), node(G, name="g"))
        with pytest.raises(ValueError, match="neither matched nor residue"):
            diff_models(base, model("b"), Correspondence(unmatched_base=[F]))

    def test_a_correspondence_built_against_other_models_is_refused(self) -> None:
        with pytest.raises(ValueError, match="different model"):
            diff_models(
                model("a"), model("b"), Correspondence(unmatched_base=[NodeId("c:@F@ghost#")])
            )


class TestOneToolchainOrNoDiff:
    """ARCH-001 §12.2: `diff` refuses two models whose toolchain identity differs.

    The failure this defends against is not a missed change, it is an *invented* one.
    `std::string` gaining a member because the base was parsed against libstdc++ 12 and the
    head against 14 arrives as a `NodeAdded` like any other, at full confidence,
    indistinguishable in the output from a real change -- which §12.2 calls worse than
    anything in §11 and why the response is refusal rather than a note.

    Asserted at this boundary because this is the first stage holding both models: the
    cache makes it true incidentally (a changed producer misses cache), but that is a
    consequence of a caching decision rather than a guarantee, and `Correspondence` never
    sees a model. It must hold for API callers and not only for `cli.py`, so it is tested
    through `diff_models` directly.
    """

    def toolchain_model(self, rev: str, producer: str, *nodes: Node) -> Model:
        return model(
            rev,
            *nodes,
            completeness=Completeness(producers=[producer], producer_version=producer),
        )

    def test_two_models_from_one_toolchain_diff_normally(self) -> None:
        same = "libclang 18.1.3 x86_64-pc-linux-gnu"
        base = self.toolchain_model("a", same, node(F, body_hash="aaa"))
        head = self.toolchain_model("b", same, node(F, body_hash="bbb"))
        assert kinds(diff(base, head)) == ["NodeBodyChanged"]

    def test_a_different_parser_is_refused(self) -> None:
        base = self.toolchain_model("a", "libclang 18.1.3 x86_64-pc-linux-gnu", node(F))
        head = self.toolchain_model("b", "libclang 19.1.0 x86_64-pc-linux-gnu", node(F))
        with pytest.raises(ToolchainMismatchError):
            diff(base, head)

    def test_a_different_target_is_refused(self) -> None:
        """The case the content digests cannot catch: same parser, same headers,
        byte-identical on both hosts, different `sizeof` (ARCH-001 §12.1)."""
        base = self.toolchain_model("a", "libclang 18.1.3 x86_64-pc-linux-gnu", node(F))
        head = self.toolchain_model("b", "libclang 18.1.3 aarch64-unknown-linux-gnu", node(F))
        with pytest.raises(ToolchainMismatchError):
            diff(base, head)

    def test_the_refusal_names_both_identities_and_what_differs(self) -> None:
        """An error a user cannot act on is a §11 failure of its own: the whole reason
        this is fatal is that the user has to go rebuild one of the two sides, and they
        cannot do that without being told which one is wrong."""
        base = self.toolchain_model("a", "libclang 18.1.3 x86_64-pc-linux-gnu", node(F))
        head = self.toolchain_model("b", "libclang 18.1.3 aarch64-unknown-linux-gnu", node(F))
        with pytest.raises(ToolchainMismatchError) as caught:
            diff(base, head)
        message = str(caught.value)
        assert "x86_64-pc-linux-gnu" in message
        assert "aarch64-unknown-linux-gnu" in message
        assert "base" in message and "head" in message

    def test_the_refusal_is_a_precondition_failure(self) -> None:
        """§11 treatment, so `cli.py` renders it as one and nothing catches it to produce
        a partial delta."""
        assert issubclass(ToolchainMismatchError, PreconditionError)

    def test_an_unrecorded_toolchain_does_not_read_as_agreement_with_a_recorded_one(
        self,
    ) -> None:
        """A model that never said what built it has not said it matches."""
        base = model("a", node(F))
        head = self.toolchain_model("b", "libclang 18.1.3 x86_64-pc-linux-gnu", node(F))
        with pytest.raises(ToolchainMismatchError):
            diff(base, head)

    def test_the_check_runs_before_any_event_is_built(self) -> None:
        """A mismatch must not be reported as a delta with a warning attached: the events
        it would produce are the fabricated ones, so none may be constructed at all."""
        base = self.toolchain_model("a", "libclang 18.1.3 x86_64-pc-linux-gnu", node(F))
        head = self.toolchain_model("b", "libclang 19.1.0 x86_64-pc-linux-gnu", node(G, name="g"))
        with pytest.raises(ToolchainMismatchError):
            # A correspondence built against neither model -- `_check_covers` would raise
            # too. The toolchain check has to win, or the user is sent to debug the wrong
            # thing.
            diff_models(base, head, Correspondence())


class TestTheDroppedEdgeAsymmetryIsInherited:
    """The builder drops edges whose endpoints it cannot resolve and records only a count, in
    prose, in `Completeness.notes`.

    This stage cannot detect that -- a dropped edge leaves no trace in the model it was
    dropped from. What it can do is not make the asymmetry worse, and the shape of the
    bug is reassuring: an endpoint resolves on one side and not the other only when the
    node sets differ there, so the resulting edge event always arrives with the node event
    that explains it. Linking the two is classify's job (§6.3).
    """

    def test_an_edge_whose_endpoint_exists_on_one_side_arrives_with_its_node_event(self) -> None:
        base = model("a", node(F))
        head = model("b", node(F), node(G, name="g"), edges=[calls(F, G)])
        events = diff(base, head)
        assert kinds(events) == ["EdgeAdded", "NodeAdded"]
        assert only(events, EventKind.NODE_ADDED).subject == G
