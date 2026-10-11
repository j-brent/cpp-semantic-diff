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
    ChangeEvent,
    ChangeEventKind,
    Completeness,
    Edge,
    EdgeKind,
    Location,
    MatchTier,
    Model,
    Node,
    NodeId,
    NodeKind,
    Side,
    TuResult,
    TuStatus,
    Unpaired,
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


def diff(
    base: Model, head: Model, correspondence: Correspondence | None = None
) -> list[ChangeEvent]:
    return diff_models(base, head, correspondence or match_models(base, head))


def kinds(events: list[ChangeEvent]) -> list[str]:
    return sorted(e.kind.value for e in events)


def only(events: list[ChangeEvent], kind: ChangeEventKind) -> ChangeEvent:
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
    left_base = sorted(n for n in base.nodes if n not in matched_base)
    left_head = sorted(n for n in head.nodes if n not in matched_head)
    return Correspondence(
        matches=[Match(base=NodeId(b), head=NodeId(h), tier=tier) for b, h in pairs],
        unmatched_base=left_base,
        unmatched_head=left_head,
        unpaired_base=dict.fromkeys(left_base, Unpaired.NONE),
        unpaired_head=dict.fromkeys(left_head, Unpaired.NONE),
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
        assert only(events, ChangeEventKind.NODE_REMOVED).subject == "c:@F@compute#"
        assert only(events, ChangeEventKind.NODE_ADDED).subject == "c:@F@calculate#"

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
        event = only(diff(base, head, corr), ChangeEventKind.NODE_RENAMED)
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
        event = only(diff(base, head), ChangeEventKind.NODE_MOVED)
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
        event = only(diff(base, head), ChangeEventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "virtual", "old": False, "new": True}

    def test_two_changed_attributes_are_two_events(self) -> None:
        base = model("a", node(F, attrs={"virtual": False, "access": "public"}))
        head = model("b", node(F, attrs={"virtual": True, "access": "private"}))
        assert {e.payload["attr"] for e in diff(base, head)} == {"virtual", "access"}

    def test_an_attribute_that_appears_is_a_change(self) -> None:
        base = model("a", node(F, attrs={}))
        head = model("b", node(F, attrs={"virtual": True}))
        event = only(diff(base, head), ChangeEventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "virtual", "old": None, "new": True}

    def test_an_attribute_that_disappears_is_a_change(self) -> None:
        """The one that hides behind a falsy test. `virtual` dropped and `virtual` set to
        false are the same shape, and `if not old` would swallow both."""
        base = model("a", node(F, attrs={"virtual": False}))
        head = model("b", node(F, attrs={}))
        event = only(diff(base, head), ChangeEventKind.NODE_ATTR_CHANGED)
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
        assert only(diff(base, head), ChangeEventKind.NODE_ATTR_CHANGED).payload["new"] == 0


class TestBaseOrderIsTheOrderOfTheBasesOnBothSides:
    """`base_order` is reported only where the bases present on both sides are in a different
    relative order (ADR-043). An added or removed base is an `inherits` edge event already."""

    def events(self, old: str | None, new: str | None) -> list[ChangeEvent]:
        def record(rev: str, order: str | None) -> Model:
            return model(rev, node(C, "C", kind=NodeKind.STRUCT, attrs={"base_order": order}))

        return diff(record("a", old), record("b", new))

    def test_a_pure_addition_is_not_a_reorder(self) -> None:
        assert self.events("A", "A; B") == []
        assert self.events("", "A") == []
        assert self.events("A; B", "A; C; B") == []

    def test_a_removal_is_not_a_reorder(self) -> None:
        assert self.events("A; B; C", "A; C") == []

    def test_a_reorder_is_reported(self) -> None:
        event = only(self.events("A; B", "B; A"), ChangeEventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "base_order", "old": "A; B", "new": "B; A"}

    def test_a_reorder_alongside_an_addition_stays_visible(self) -> None:
        event = only(self.events("A; B", "C; B; A"), ChangeEventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "base_order", "old": "A; B", "new": "C; B; A"}

    def test_an_order_not_determined_on_one_side_is_not_compared(self) -> None:
        assert self.events(None, "A; B") == []


class TestBaseOrderCoexistsWithOtherAttributeChanges:
    """The `BASE_ORDER` filter in `node_events` is a `continue` inside the per-attribute
    loop (`diff/nodes.py`), not a guard around the whole node. A class can change its base
    order and something else -- `final`, say -- in the same commit, and the filter must
    drop only the `base_order` entry, never the attribute sitting beside it in the same
    `attrs` map."""

    def record(self, rev: str, order: str | None, final: bool) -> Model:
        return model(
            rev,
            node(C, "C", kind=NodeKind.STRUCT, attrs={"base_order": order, "final": final}),
        )

    def test_a_pure_addition_still_reports_an_unrelated_attribute_change(self) -> None:
        base = self.record("a", "A", final=False)
        head = self.record("b", "A; B", final=True)
        event = only(diff(base, head), ChangeEventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "final", "old": False, "new": True}

    def test_a_real_reorder_and_an_unrelated_attribute_change_are_two_events(self) -> None:
        base = self.record("a", "A; B", final=False)
        head = self.record("b", "B; A", final=True)
        assert {e.payload["attr"] for e in diff(base, head)} == {"base_order", "final"}


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


class TestASignatureChangeIsOneEvent:
    """ADR-035: a changed signature is `NodeSignatureChanged`, and only that.

    The canonical signature is the function type (§4.1), so it already spells the return
    type and a method's `const`. Reporting those again as attribute changes beside it would
    count one change twice.
    """

    M = NodeId("c:@S@C@F@m#")

    def method(self, signature: str, **attrs: Any) -> Node:
        return node(self.M, name="m", kind=NodeKind.METHOD, signature=signature, attrs=attrs)

    def test_a_differing_signature_carries_both_spellings(self) -> None:
        base = model("a", node(F, signature="void (int)"))
        head = model("b", node(F, signature="void (long)"))
        event = only(diff(base, head), ChangeEventKind.NODE_SIGNATURE_CHANGED)
        assert event.payload == {"old_signature": "void (int)", "new_signature": "void (long)"}
        assert kinds(diff(base, head)) == ["NodeSignatureChanged"]

    def test_the_return_type_and_const_are_not_reported_twice(self) -> None:
        base = model("a", self.method("int () const", return_type="int", const=True))
        head = model("b", self.method("long ()", return_type="long", const=False))
        assert kinds(diff(base, head)) == ["NodeSignatureChanged"]

    def test_other_attributes_still_change_beside_it(self) -> None:
        base = model("a", self.method("int ()", return_type="int", virtual=False))
        head = model("b", self.method("long ()", return_type="long", virtual=True))
        assert kinds(diff(base, head)) == ["NodeAttrChanged", "NodeSignatureChanged"]
        assert only(diff(base, head), ChangeEventKind.NODE_ATTR_CHANGED).payload["attr"] == (
            "virtual"
        )

    def test_without_a_signature_change_the_attributes_report_as_before(self) -> None:
        base = model("a", self.method("int ()", return_type="int", const=False))
        head = model("b", self.method("int ()", return_type="int", const=True))
        assert kinds(diff(base, head)) == ["NodeAttrChanged"]


class TestDefaultArgumentsAreAttributesNotSignature:
    """ADR-043 corrects ARCH-001 §4.1: a default argument is an attribute, not part of the
    signature it used to be folded into. `SIGNATURE_ATTRS` exists to suppress exactly
    `return_type` and `const` beside a signature change, because the signature already
    spells them (ADR-035) -- folding `default_arguments` into that set by mistake would
    silently swallow the fact ADR-043 exists to surface, and only on the one function
    where a signature change happens to land in the same commit.
    """

    def test_signature_attrs_does_not_include_default_arguments(self) -> None:
        from cpp_semantic_diff.diff.nodes import SIGNATURE_ATTRS

        assert "default_arguments" not in SIGNATURE_ATTRS

    def test_a_default_argument_change_is_not_swallowed_by_a_signature_change(self) -> None:
        base = model("a", node(F, signature="void (int)", attrs={"default_arguments": "0"}))
        head = model("b", node(F, signature="void (long)", attrs={"default_arguments": "1"}))
        assert kinds(diff(base, head)) == ["NodeAttrChanged", "NodeSignatureChanged"]
        event = only(diff(base, head), ChangeEventKind.NODE_ATTR_CHANGED)
        assert event.payload == {"attr": "default_arguments", "old": "0", "new": "1"}

    def test_a_default_argument_change_alone_is_an_attribute_change(self) -> None:
        base = model("a", node(F, signature="void (int)", attrs={"default_arguments": "0"}))
        head = model("b", node(F, signature="void (int)", attrs={"default_arguments": "1"}))
        assert kinds(diff(base, head)) == ["NodeAttrChanged"]

    def test_an_unchanged_default_argument_beside_a_signature_change_is_silent(self) -> None:
        base = model("a", node(F, signature="void (int)", attrs={"default_arguments": "0"}))
        head = model("b", node(F, signature="void (long)", attrs={"default_arguments": "0"}))
        assert kinds(diff(base, head)) == ["NodeSignatureChanged"]


class TestSignatureAndBodyApplyOnlyToFunctions:
    """ARCH-001 §6.1's applicability table (`APPLIES_TO`, ADR-035). Only a function or a
    method has a signature or a code body, so no other kind of node is reported with a
    changed one, whatever the producer put in the fields."""

    def test_a_class_never_gets_a_signature_or_body_event(self) -> None:
        def cls(rev: str, signature: str, body: str) -> Model:
            return model(
                rev,
                node(C, name="C", kind=NodeKind.CLASS, signature=signature, body_hash=body),
            )

        assert diff(cls("a", "x", "aaa"), cls("b", "y", "bbb")) == []

    def test_the_table_names_function_and_method_for_both(self) -> None:
        from cpp_semantic_diff.model import APPLIES_TO

        both = {NodeKind.FUNCTION, NodeKind.METHOD}
        assert APPLIES_TO[ChangeEventKind.NODE_SIGNATURE_CHANGED] == both
        assert APPLIES_TO[ChangeEventKind.NODE_BODY_CHANGED] == both


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
        event = only(diff(base, head), ChangeEventKind.EDGE_REMOVED)
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
        event = only(diff(base, head), ChangeEventKind.EDGE_KIND_CHANGED)
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
        event = only(diff(base, head), ChangeEventKind.EDGE_ATTR_CHANGED)
        assert event.payload == {"attr": "access", "old": "public", "new": "private"}
        assert event.subject == f"{self.DERIVED}|inherits|{self.BASE_CLASS}"

    def test_an_unchanged_attribute_is_silent(self) -> None:
        base, head = self.sides("public", "public")
        assert diff(base, head) == []

    def test_the_event_points_at_both_sides(self) -> None:
        """§6.2: the reviewer has to reach the old attribute as well as the new one."""
        base, head = self.sides("public", "private")
        event = only(diff(base, head), ChangeEventKind.EDGE_ATTR_CHANGED)
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
        assert only(diff(appeared, vanished), ChangeEventKind.EDGE_ATTR_CHANGED).payload == {
            "attr": "virtual",
            "old": False,
            "new": None,
        }

        flipped = model("b", node(F), node(G, name="g"), edges=[calls(F, G, attrs={"virtual": 0})])
        assert only(diff(appeared, flipped), ChangeEventKind.EDGE_ATTR_CHANGED).payload["new"] == 0

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
        assert (
            only(events, ChangeEventKind.EDGE_ATTR_CHANGED).confidence.tier is MatchTier.STRUCTURAL
        )


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
        assert {ChangeEventKind.EDGE_KIND_CHANGED, ChangeEventKind.EDGE_ATTR_CHANGED} <= produced


class TestConfidenceIsNeverLaundered:
    """ARCH-001 §11: confidence comes from the match tier and the completeness of *both*
    models, and an event that rests on nothing must say so."""

    def test_a_matched_node_reports_the_tier_that_matched_it(self) -> None:
        base = model("a", node(F, body_hash="aaa"))
        head = model("b", node(F, body_hash="bbb"))
        assert only(diff(base, head), ChangeEventKind.NODE_BODY_CHANGED).confidence.tier is (
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
        assert only(
            diff(base, head), ChangeEventKind.NODE_BODY_CHANGED
        ).confidence.models_complete is (False)

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
        assert only(diff(base, head, corr), ChangeEventKind.EDGE_REMOVED).confidence.tier is (
            MatchTier.SIMILARITY
        )

    def test_an_edge_with_an_unmatched_endpoint_has_no_tier(self) -> None:
        base = model("a", node(F), node(G, name="g"), edges=[calls(F, G)])
        head = model("b", node(F))
        assert only(diff(base, head), ChangeEventKind.EDGE_REMOVED).confidence.tier is None

    def test_an_unmatched_node_carries_why_it_is_unmatched(self) -> None:
        """ADR-034: no partner is not doubt. The match stage says which it is, and the
        event carries that verbatim for rank to weigh."""
        base = model("a", node(F))
        head = model("b", node(F), node(G, name="g"))
        confidence = only(diff(base, head), ChangeEventKind.NODE_ADDED).confidence
        assert (confidence.tier, confidence.unpaired) == (None, Unpaired.NONE)

    def test_an_edge_to_an_unmatched_node_carries_that_node_s_unpaired_reason(self) -> None:
        """An edge between a matched entity and a plain addition is no more doubtful than
        the addition -- which is not doubtful at all. Before ADR-034 the edge took the
        maximum because one endpoint had no tier."""
        base = model("a", node(F))
        head = model("b", node(F), node(G, name="g"), edges=[calls(F, G)])
        confidence = only(diff(base, head), ChangeEventKind.EDGE_ADDED).confidence
        assert (confidence.tier, confidence.unpaired) == (None, Unpaired.NONE)

    def test_an_edge_takes_the_more_doubtful_of_two_unmatched_endpoints(self) -> None:
        a, x, y = NodeId("c:@F@a#"), NodeId("c:@F@x#"), NodeId("c:@F@y#")
        base = model("a", node(a, name="a"))
        head = model("b", node(x, name="x"), node(y, name="y"), edges=[calls(x, y)])
        corr = Correspondence(
            unmatched_base=[a],
            unmatched_head=[x, y],
            unpaired_base={a: Unpaired.CANDIDATE},
            unpaired_head={x: Unpaired.AMBIGUOUS, y: Unpaired.CANDIDATE},
        )
        confidence = only(diff(base, head, corr), ChangeEventKind.EDGE_ADDED).confidence
        assert (confidence.tier, confidence.unpaired) == (None, Unpaired.AMBIGUOUS)


class TestProvenanceRunsBothWays:
    """ARCH-001 §6.2. The reviewer navigates from an event to the lines *and* back. An
    event about two sides that carries one hunk is a one-way link."""

    def test_a_two_sided_event_carries_a_hunk_from_each_side(self) -> None:
        base = model("a", node(F, body_hash="aaa", location=Location(file="a.h", line=3)))
        head = model("b", node(F, body_hash="bbb", location=Location(file="a.h", line=91)))
        event = only(diff(base, head), ChangeEventKind.NODE_BODY_CHANGED)
        assert [(h.side, h.location.line) for h in event.provenance] == [
            (Side.BASE, 3),
            (Side.HEAD, 91),
        ]

    def test_a_one_sided_event_carries_only_that_side(self) -> None:
        base = model("a", node(F, location=Location(file="a.h", line=3)))
        event = only(diff(base, model("b")), ChangeEventKind.NODE_REMOVED)
        assert [(h.side, h.location.file) for h in event.provenance] == [(Side.BASE, "a.h")]

    def test_every_declaration_site_of_a_node_is_a_hunk(self) -> None:
        """A function declared in a header and defined in a source file was editable in
        both; the header line must point back at the event too, location first."""
        decl, defn = Location(file="a.h", line=3), Location(file="a.cpp", line=10)
        base = model("a", node(F, location=defn, sites=(defn, decl)))
        event = only(diff(base, model("b")), ChangeEventKind.NODE_REMOVED)
        assert [h.location for h in event.provenance] == [defn, decl]

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
        assert len(only(diff(base, head), ChangeEventKind.EDGE_REMOVED).provenance) == 2


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
            diff_models(
                base,
                model("b"),
                Correspondence(unmatched_base=[F], unpaired_base={F: Unpaired.NONE}),
            )

    def test_a_correspondence_built_against_other_models_is_refused(self) -> None:
        with pytest.raises(ValueError, match="different model"):
            ghost = NodeId("c:@F@ghost#")
            diff_models(
                model("a"),
                model("b"),
                Correspondence(unmatched_base=[ghost], unpaired_base={ghost: Unpaired.NONE}),
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
        assert only(events, ChangeEventKind.NODE_ADDED).subject == G


class TestAFunctionPairsByNameAcrossAUsrChange:
    """ADR-026, ADR-035, ADR-036: a parameter change moves a function's USR, so tier 1
    cannot pair it with itself -- that is the whole reason the name tier exists. Every
    `NodeSignatureChanged` test above uses `node(F, ...)` with the *same* id on both
    sides, which tier 1 pairs on its own; none of them exercises `MatchTier.NAME` at all,
    so the feature PR #43 actually shipped -- pairing a changed USR by name -- has no
    integration coverage here. These use the real `match_models`, not the `paired()`
    scaffolding, because the claim under test is that the matcher's name tier and this
    stage's reading of it actually agree, end to end.
    """

    def test_a_parameter_change_pairs_by_name_and_reports_one_signature_change(self) -> None:
        """The headline case: the USR differs (a parameter changed), tier 1 cannot pair
        it, and without the name tier this would read as a `NodeRemoved` plus a
        `NodeAdded` instead of one `NodeSignatureChanged` with its callers intact."""
        base = model("a", node("c:@F@f#i#", name="f", signature="void (int)"))
        head = model("b", node("c:@F@f#d#", name="f", signature="void (double)"))
        events = diff(base, head)
        assert kinds(events) == ["NodeSignatureChanged"]
        event = only(events, ChangeEventKind.NODE_SIGNATURE_CHANGED)
        assert event.payload == {"old_signature": "void (int)", "new_signature": "void (double)"}
        assert event.confidence.tier is MatchTier.NAME

    def test_tier_1_takes_the_unchanged_overload_first_the_way_adr_036_s_example_says(
        self,
    ) -> None:
        """ADR-036's own worked example: `f(int)` and `f(double)` before, `f(int)` and
        `f(long)` after. Tier 1 pairs `f(int)` to itself and removes it from both pools,
        which is what leaves exactly one leftover on each side for the name tier to pair.
        Getting the pool bookkeeping wrong here either reports `f(int)` as churn too, or
        leaves `f(double)`/`f(long)` unpaired because the pools were not narrowed first.
        """
        unchanged = node("c:@F@fi#", name="f", signature="void (int)")
        base = model("a", unchanged, node("c:@F@fd#", name="f", signature="void (double)"))
        head = model("b", unchanged, node("c:@F@fl#", name="f", signature="void (long)"))
        events = diff(base, head)
        assert kinds(events) == ["NodeSignatureChanged"]
        event = only(events, ChangeEventKind.NODE_SIGNATURE_CHANGED)
        assert event.payload == {"old_signature": "void (double)", "new_signature": "void (long)"}
        assert event.confidence.tier is MatchTier.NAME

    def test_two_unrelated_overloads_on_each_side_do_not_force_a_pairing(self) -> None:
        """ADR-036: a key with leftovers on both sides that are not one to one pairs none
        of them. Forcing a guess here -- pairing by signature similarity, say -- is exactly
        the confident-and-wrong failure ARCH-001 §11 exists to avoid, so two old overloads
        and two new ones must fall back to removal plus addition, each carrying
        `Unpaired.AMBIGUOUS` rather than a borrowed name-tier confidence."""
        base = model(
            "a",
            node("c:@F@fi#", name="f", signature="void (int)"),
            node("c:@F@fd#", name="f", signature="void (double)"),
        )
        head = model(
            "b",
            node("c:@F@fs#", name="f", signature="void (short)"),
            node("c:@F@fl#", name="f", signature="void (long)"),
        )
        events = diff(base, head)
        assert kinds(events) == ["NodeAdded", "NodeAdded", "NodeRemoved", "NodeRemoved"]
        assert {e.confidence.unpaired for e in events} == {Unpaired.AMBIGUOUS}
        assert {e.confidence.tier for e in events} == {None}

    def test_a_member_of_an_unpaired_class_does_not_pair_with_a_global_function(self) -> None:
        """ADR-036: the parent must already be paired, or the name tier never pairs the
        child -- that is what keeps this tier from ever producing a `NodeMoved`. Renaming
        the class (giving it no head counterpart at all) is not by itself a sharp test: a
        head-side class gives the member a different key regardless of the guard, so the
        two would stay unpaired either way. What actually isolates the guard is a head
        entity whose own key carries *no* parent to narrow against -- a global-scope
        function sharing the member's kind and qualified name. Without the guard, the
        member's key would also collapse its unpaired class to no-parent and the two would
        land in the same name-tier group, pairing as if the member had moved out of its
        class and into the global namespace. With the guard, the member's unpaired class
        blocks it from entering any group at all, and the pair falls back to removal plus
        addition."""
        base = model(
            "a",
            node("c:@S@C", name="C", kind=NodeKind.CLASS),
            node("c:@S@C@F@m#i#", name="m", kind=NodeKind.METHOD, signature="void (int)"),
            edges=[calls("c:@S@C", "c:@S@C@F@m#i#", kind=EdgeKind.CONTAINS)],
        )
        global_fn = node("c:@F@m#d#", name="m", kind=NodeKind.METHOD, signature="void (double)")
        head = model("b", global_fn)
        events = diff(base, head)
        assert kinds(events) == ["EdgeRemoved", "NodeAdded", "NodeRemoved", "NodeRemoved"]
        assert not any(e.kind is ChangeEventKind.NODE_MOVED for e in events)
        assert not any(e.kind is ChangeEventKind.NODE_SIGNATURE_CHANGED for e in events)
        assert {e.confidence.tier for e in events} == {None}

    def test_a_function_renamed_and_reparametrized_is_still_remove_plus_add(self) -> None:
        """The name tier keys on the qualified name (ADR-036), so a function whose name
        *and* signature both changed leaves no identity path: the USR differs (the
        parameter changed) and the qualified name differs too, so there is nothing left
        for either tier to key on. It must read exactly like an unrelated rename -- not
        like a merged signature change that happens to rename its subject, which is the
        mistake a future 'close enough' name match would make."""
        base = model("a", node("c:@F@compute#i#", name="compute", signature="void (int)"))
        head = model("b", node("c:@F@calculate#d#", name="calculate", signature="void (double)"))
        events = diff(base, head)
        assert kinds(events) == ["NodeAdded", "NodeRemoved"]
        assert {e.confidence.tier for e in events} == {None}

    def test_a_default_argument_change_is_still_reported_when_paired_by_name(self) -> None:
        """ADR-043's attribute comparisons flow through `changed_attrs` exactly like
        `virtual` or `access` always have, and every test above pairs its node through
        `MatchTier.DECLARED` -- none exercises a *new* attribute on a pair the name tier
        itself produced. If the attribute loop ever read something other than the matched
        pair's own tier, this is the test that would catch it: the USR differs (the
        parameter type changed), so only the name tier can pair this function at all, and
        the default argument changed on the same function in the same commit."""
        base = model(
            "a",
            node("c:@F@f#i#", name="f", signature="void (int)", attrs={"default_arguments": "0"}),
        )
        head = model(
            "b",
            node(
                "c:@F@f#d#", name="f", signature="void (double)", attrs={"default_arguments": "1"}
            ),
        )
        events = diff(base, head)
        assert kinds(events) == ["NodeAttrChanged", "NodeSignatureChanged"]
        attr_event = only(events, ChangeEventKind.NODE_ATTR_CHANGED)
        assert attr_event.payload == {"attr": "default_arguments", "old": "0", "new": "1"}
        assert attr_event.confidence.tier is MatchTier.NAME


class TestTheNameTiersConfidenceIsNeverLaundered:
    """ARCH-001 §11, ADR-034, ADR-036. The name tier is new to v0's `TIER_ORDER`
    (`diff/context.py`), and two things that could not be exercised before it existed are
    exercised here: the confidence trade the context docstring names explicitly, and the
    tier ordering itself, a transposition in which has no symptom any single-tier test
    would catch.
    """

    def test_an_edge_to_a_new_addition_keeps_the_additions_doubt_not_the_name_tiers(
        self,
    ) -> None:
        """`diff/context.py`'s own rationale for this change: before the name tier ran in
        v0, an edge's matched endpoint never carried a tier other than `DECLARED`'s zero
        uncertainty, so this trade was unreachable. It must still collapse to the
        addition's `Unpaired.NONE`, not borrow the name tier's 0.25 uncertainty, now that
        a name-tier footing can actually appear on an edge endpoint."""
        base = model("a", node("c:@F@f#i#", name="f", signature="void (int)"))
        head = model(
            "b",
            node("c:@F@f#d#", name="f", signature="void (double)"),
            node("c:@F@h#", name="h"),
            edges=[calls("c:@F@f#d#", "c:@F@h#")],
        )
        events = diff(base, head)
        edge_event = only(events, ChangeEventKind.EDGE_ADDED)
        assert (edge_event.confidence.tier, edge_event.confidence.unpaired) == (
            None,
            Unpaired.NONE,
        )
        # The signature change itself still reports the name tier's own confidence --
        # only the edge resting partly on the addition loses it.
        sig_event = only(events, ChangeEventKind.NODE_SIGNATURE_CHANGED)
        assert sig_event.confidence.tier is MatchTier.NAME

    def test_the_name_tier_is_weaker_than_declared_but_stronger_than_structural(self) -> None:
        """`TIER_ORDER` is the only place ARCH-001 §5's tier ordering is expressed in code,
        and ADR-036 is explicit that the name tier's uncertainty (0.25) sits below the
        structural tier's (0.30) -- i.e. the name tier is *more* confident. An edge resting
        on one endpoint from each must report the weaker of the two, `STRUCTURAL`. Built
        by hand, like `test_an_edge_is_no_more_confident_than_its_weaker_endpoint` above,
        because the real matcher never hands one edge a `NAME` endpoint and a `STRUCTURAL`
        one -- v0 only ever runs `DECLARED` then `NAME`."""
        a, b = NodeId("c:@F@a#"), NodeId("c:@F@b#")
        x, y = NodeId("c:@F@x#"), NodeId("c:@F@y#")
        base = model("a", node(a, name="a"), node(b, name="b"), edges=[calls(a, b)])
        head = model("b", node(x, name="x"), node(y, name="y"))
        corr = Correspondence(
            matches=[
                Match(base=a, head=x, tier=MatchTier.NAME),
                Match(base=b, head=y, tier=MatchTier.STRUCTURAL),
            ],
            tiers_run=[MatchTier.NAME, MatchTier.STRUCTURAL],
        )
        assert only(diff(base, head, corr), ChangeEventKind.EDGE_REMOVED).confidence.tier is (
            MatchTier.STRUCTURAL
        )
