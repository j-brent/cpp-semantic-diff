"""The delta schema -- ARCH-001 §6.

These assert the design, not the implementation. Every class below names the claim it
defends, because the failure mode for all of them is invisible in output: a schema that
admits a free-text kind, an event that quietly claims completeness it does not have, or a
provenance chain that only runs one way all produce plausible-looking JSON.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cpp_semantic_diff.model import (
    DERIVED_KINDS,
    EDGE_KINDS,
    NODE_KINDS,
    REQUIRED_PAYLOAD,
    UNREACHABLE_IN_V0,
    V0_KINDS,
    Completeness,
    Confidence,
    EdgeId,
    Event,
    EventId,
    EventKind,
    Hunk,
    Location,
    MatchTier,
    NodeId,
    Residue,
    Side,
    TuResult,
    TuStatus,
    events_at,
)

CERTAIN = Confidence(tier=MatchTier.DECLARED, models_complete=True)


def event(
    kind: EventKind = EventKind.NODE_BODY_CHANGED, subject: str = "c:@F@f#", **kw: Any
) -> Event:
    return Event(kind=kind, subject=NodeId(subject), confidence=CERTAIN, **kw)


def attr_changed(
    attr: str = "virtual", old: object = False, new: object = True, **kw: Any
) -> Event:
    return event(EventKind.NODE_ATTR_CHANGED, payload={"attr": attr, "old": old, "new": new}, **kw)


class TestTheSetIsClosed:
    """ARCH-001 §6.1: new signals become new kinds, never free text.

    Every downstream stage switches on the kind. A stringly-typed one turns each of those
    switches into a place where an unrecognized signal is silently dropped.
    """

    def test_kinds_are_exactly_the_ones_arch_names(self) -> None:
        """ARCH-001 §6.1's list, plus `EdgeAttrChanged`.

        **The addition is the point of this test, not an exception to it.** §6.1's rule is
        that a new signal becomes a new *kind*; §4.2 gives edges `attrs` and the producer
        writes `access` on `inherits` and `virtual` on `calls`; so a base class going
        public -> private between two unchanged classes was a real C++ breaking change with
        no kind to carry it, and the diff stage dropped it in silence. Extending the set is
        the sanctioned move and updating this list is how it stays deliberate.

        §6.1's prose still names four edge kinds and needs the fifth written into it.
        """
        assert {k.value for k in EventKind} == {
            "NodeAdded",
            "NodeRemoved",
            "NodeMoved",
            "NodeRenamed",
            "NodeAttrChanged",
            "NodeBodyChanged",
            "EdgeAdded",
            "EdgeRemoved",
            "EdgeRetargeted",
            "EdgeKindChanged",
            "EdgeAttrChanged",
            "BecameUnreachable",
            "BecameReachable",
            "CycleIntroduced",
            "FanInChanged",
            "LayerViolationIntroduced",
            "SignatureIncompatible",
        }

    def test_the_three_groups_partition_the_set(self) -> None:
        assert set(EventKind) == NODE_KINDS | EDGE_KINDS | DERIVED_KINDS
        assert not NODE_KINDS & EDGE_KINDS & DERIVED_KINDS

    def test_v0_emits_a_subset_and_nothing_derived(self) -> None:
        """ARCH-001 §13 -- derived events are out of v0, but their kinds exist so consumers
        are extended rather than rewritten when they land."""
        assert set(EventKind) > V0_KINDS
        assert not V0_KINDS & DERIVED_KINDS

    def test_v0_excludes_exactly_what_v0_cannot_reach(self) -> None:
        """`V0_KINDS` is read as a description of the stage, so it has to be one.

        It used to name edge add/remove only, following §13's prose, while the diff stage
        legitimately reached `EdgeKindChanged` on tier-1 input -- the endpoints are identity
        and belong to the matcher, the kind is diffable state (§6.1). `EdgeAttrChanged`
        arrives the same way. Understated, the constant made `<= V0_KINDS` assertions pass
        for the wrong reason: they were checking against a set with room to spare.

        What remains outside is what v0 genuinely cannot produce, and each exclusion has a
        different reason: renames and moves need a tier past 1 (the USR encodes name and
        scope, so a matched pair agrees on both), `EdgeRetargeted` is refused outright, and
        the derived kinds are deferred.
        """
        assert {
            EventKind.NODE_RENAMED,
            EventKind.NODE_MOVED,
            EventKind.EDGE_RETARGETED,
            *DERIVED_KINDS,
        } == UNREACHABLE_IN_V0
        assert not V0_KINDS & UNREACHABLE_IN_V0
        assert set(EventKind) == V0_KINDS | UNREACHABLE_IN_V0

    def test_an_edge_attribute_change_has_a_kind_of_its_own(self) -> None:
        """ARCH-001 §6.1: new signals become new event kinds, never free text.

        `access` on an `inherits` edge going public -> private is a breaking change in C++
        and is *only* expressible this way: both classes are unchanged and the edge is on
        both sides, so no add, remove or kind change can carry it.
        """
        assert EventKind.EDGE_ATTR_CHANGED in EDGE_KINDS
        assert EventKind.EDGE_ATTR_CHANGED in V0_KINDS

    def test_an_edge_attribute_change_carries_the_same_payload_as_a_node_one(self) -> None:
        """It is the same mechanical attribute diff (§4.3) over a different `attrs` dict. A
        consumer that can read one must not have to learn a second shape for the other."""
        assert (
            REQUIRED_PAYLOAD[EventKind.EDGE_ATTR_CHANGED]
            == REQUIRED_PAYLOAD[EventKind.NODE_ATTR_CHANGED]
            == frozenset({"attr", "old", "new"})
        )
        with pytest.raises(ValidationError, match="requires payload keys"):
            event(EventKind.EDGE_ATTR_CHANGED, payload={"attr": "access", "old": "public"})

    def test_there_is_no_catch_all_kind(self) -> None:
        assert not {k for k in EventKind if k.name in {"OTHER", "CUSTOM", "UNKNOWN"}}

    def test_an_invented_kind_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            # The invented kind is the point: validation, not the checker, must reject it.
            Event(
                kind="SomethingChanged",  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
                subject=NodeId("c:@F@f#"),
                confidence=CERTAIN,
            )

    def test_an_invented_field_is_rejected(self) -> None:
        """extra="forbid" is the other half: no escape hatch by field either."""
        with pytest.raises(ValidationError):
            event(detail="the signature got weird")


class TestTheShapeIsArch62:
    def test_fields_are_exactly_the_seven(self) -> None:
        assert set(Event.model_fields) == {
            "id",
            "kind",
            "subject",
            "payload",
            "caused_by",
            "provenance",
            "confidence",
        }

    def test_nothing_in_the_schema_can_hide_an_event(self) -> None:
        """ARCH-001 §6.3: collapse happens at render, never at diff.

        A `suppressed`/`collapsed` flag here would let the diff stage decide what a reviewer
        never sees. The JSON carries the full set; the view hides consequences under causes.
        """
        assert not {"suppressed", "collapsed", "hidden", "visible"} & set(Event.model_fields)


class TestPayloadStaysMechanical:
    """ARCH-001 §4.3, applied to events: the diff that produces a payload is mechanical."""

    @pytest.mark.parametrize("value", ["public", 3, 3.5, True, None])
    def test_scalars_are_accepted(self, value: object) -> None:
        assert attr_changed(new=value).payload["new"] == value

    @pytest.mark.parametrize("value", [{"nested": 1}, ["a"], (1, 2)])
    def test_structures_are_rejected(self, value: object) -> None:
        with pytest.raises(ValidationError, match="must be scalar"):
            attr_changed(new=value)

    def test_a_kind_gets_the_payload_arch_says_it_carries(self) -> None:
        """NodeAttrChanged(attr, old, new) is named in §6.1. A consumer switching on the
        kind must be able to rely on it rather than probing for keys."""
        with pytest.raises(ValidationError, match="requires payload keys"):
            event(EventKind.NODE_ATTR_CHANGED, payload={"attr": "virtual", "old": False})


class TestIdentityOfAnEvent:
    """The id is derived from what the event *is*, not from when it was produced.

    Ordering-dependent ids would break the §3 promise that each stage is a pure function of
    the last, and would make `caused_by` links differ between two runs over the same commits.
    """

    def test_the_same_change_gets_the_same_id_every_run(self) -> None:
        assert attr_changed().id == attr_changed().id

    def test_different_changes_to_one_subject_are_different_events(self) -> None:
        assert attr_changed(attr="virtual").id != attr_changed(attr="const").id

    def test_where_it_was_found_is_not_what_it_is(self) -> None:
        """The §4.1 rule that makes Location provenance, restated for events."""
        located = attr_changed(
            provenance=[Hunk(side=Side.HEAD, location=Location(file="a.cpp", line=7))]
        )
        assert located.id == attr_changed().id

    def test_linking_a_cause_does_not_renumber_an_event(self) -> None:
        """classify adds `caused_by` after diff has run (ARCH-001 §3). If that changed the
        id, every event already pointing at this one would dangle."""
        cause = event(EventKind.NODE_REMOVED, subject="c:@F@gone#")
        assert attr_changed(caused_by=cause.id).id == attr_changed().id

    def test_the_id_survives_the_json_round_trip(self) -> None:
        """JSON is the product (ARCH-001 §10); ids in it have to still resolve."""
        e = attr_changed()
        assert Event.model_validate_json(e.model_dump_json()).id == e.id


class TestAnEventIsImmutable:
    """ARCH-001 §3: every stage is a pure function of the previous stage's output.

    A mutable event makes that a convention rather than a fact. The obvious implementation
    of a stage that changes nothing is to return the list it was given -- and then two
    owners hold the same objects, and a write in the later one edits the earlier one's
    output after the fact, with no diagnostic anywhere. `Match`, `Hunk` and `Confidence`
    were already frozen; this is the one that was not.
    """

    def test_a_field_cannot_be_written_after_construction(self) -> None:
        with pytest.raises(ValidationError, match="frozen"):
            # Writing the frozen field is the point of the test.
            attr_changed().caused_by = EventId("NodeRemoved:deadbeef")  # type: ignore[misc]

    def test_a_cause_is_added_by_copying_and_the_original_is_untouched(self) -> None:
        """What `classify` does (ARCH-001 §6.3), and the reason freezing costs nothing:
        `model_copy(update=...)` is the supported way to produce the changed event."""
        original = attr_changed()
        cause = event(EventKind.NODE_REMOVED, subject="c:@F@gone#")
        linked = original.model_copy(update={"caused_by": cause.id})

        assert linked.caused_by == cause.id
        assert original.caused_by is None
        # The id is derived from kind, subject and payload and excludes `caused_by`, so
        # linking must not renumber an event other events already point at.
        assert linked.id == original.id

    def test_two_stages_holding_one_list_cannot_edit_each_other(self) -> None:
        """The failure freezing prevents, written as the scenario rather than the mechanism.

        `stage_two` here is any stage that returns its input -- which is what a stage with
        nothing to say looks like -- and the write it attempts would otherwise land in the
        list `stage_one` already returned.
        """
        produced = [attr_changed()]
        passed_through = list(produced)
        with pytest.raises(ValidationError, match="frozen"):
            # Writing the frozen field is the point of the test.
            passed_through[0].payload = {}  # type: ignore[misc]
        assert produced[0].payload == {"attr": "virtual", "old": False, "new": True}


class TestConfidenceIsAlwaysStated:
    """ARCH-001 §11: a tool that silently degrades converts absence of evidence into
    apparent evidence of absence."""

    def test_an_event_cannot_omit_its_confidence(self) -> None:
        with pytest.raises(ValidationError):
            # The missing confidence is the point: validation must reject it.
            Event(kind=EventKind.NODE_BODY_CHANGED, subject=NodeId("c:@F@f#"))  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]

    def test_completeness_cannot_be_assumed(self) -> None:
        """No default: omitting it would be a silent claim that nothing was missing."""
        with pytest.raises(ValidationError):
            # The missing completeness is the point: validation must reject it.
            Confidence(tier=MatchTier.DECLARED)  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]

    def test_the_match_tier_reaches_the_event(self) -> None:
        """ARCH-001 §5: the tier propagates into every event derived from the match."""
        assert attr_changed().confidence.tier is MatchTier.DECLARED

    def test_an_unmatched_subject_has_no_tier(self) -> None:
        """Add and remove events have no pair. In v0 that also covers every rename
        (§13) -- stating None is how the damage stays visible."""
        e = Event(
            kind=EventKind.NODE_ADDED,
            subject=NodeId("c:@F@new#"),
            confidence=Confidence(tier=None, residue=Residue.NONE, models_complete=True),
        )
        assert e.confidence.tier is None

    def test_an_incomplete_model_on_either_side_is_carried(self) -> None:
        whole = Completeness(tus_total=1, tus=[TuResult(path="a.cpp", status=TuStatus.PARSED)])
        partial = Completeness(
            tus_total=2,
            tus=[
                TuResult(path="a.cpp", status=TuStatus.PARSED),
                TuResult(path="b.cpp", status=TuStatus.FAILED, detail="missing header"),
            ],
        )
        assert Confidence.derive(MatchTier.DECLARED, whole, whole).models_complete
        assert not Confidence.derive(MatchTier.DECLARED, whole, partial).models_complete
        assert not Confidence.derive(MatchTier.DECLARED, partial, whole).models_complete

    def test_an_unmatched_subject_must_say_why(self) -> None:
        """ADR-034. Without a residue rank would have to guess how doubtful an add or a
        remove is, and a default would be a guess presented as a measurement."""
        with pytest.raises(ValidationError, match="residue is required"):
            Confidence(tier=None, models_complete=True)

    def test_a_matched_subject_has_no_residue(self) -> None:
        with pytest.raises(ValidationError, match="has no residue"):
            Confidence(tier=MatchTier.DECLARED, residue=Residue.NONE, models_complete=True)

    def test_the_residue_survives_a_json_round_trip(self) -> None:
        c = Confidence(tier=None, residue=Residue.CANDIDATE, models_complete=True)
        assert Confidence.model_validate_json(c.model_dump_json()) == c


class TestProvenanceRunsBothWays:
    """ARCH-001 §6.2. Without the reverse direction the tool is a second artifact to read
    alongside the diff -- more work for the reviewer, not less."""

    @staticmethod
    def _spanning(first: int, last: int, side: Side = Side.HEAD, file: str = "src/a.cpp") -> Event:
        return event(
            EventKind.NODE_BODY_CHANGED,
            subject=f"c:@F@f{first}#",
            provenance=[Hunk(side=side, location=Location(file=file, line=first, end_line=last))],
        )

    def test_an_event_points_at_a_side_a_file_and_a_span(self) -> None:
        hunk = self._spanning(10, 20).provenance[0]
        assert (hunk.side, hunk.location.file, hunk.location.line, hunk.location.end_line) == (
            Side.HEAD,
            "src/a.cpp",
            10,
            20,
        )

    def test_a_line_finds_the_events_it_caused(self) -> None:
        inside, elsewhere = self._spanning(10, 20), self._spanning(80, 90)
        assert events_at([inside, elsewhere], Side.HEAD, "src/a.cpp", 15) == [inside]
        assert events_at([inside, elsewhere], Side.HEAD, "src/a.cpp", 30) == []

    def test_a_single_line_hunk_is_its_own_span(self) -> None:
        e = event(provenance=[Hunk(side=Side.BASE, location=Location(file="a.cpp", line=4))])
        assert events_at([e], Side.BASE, "a.cpp", 4) == [e]
        assert events_at([e], Side.BASE, "a.cpp", 5) == []

    def test_the_two_sides_are_not_confused(self) -> None:
        """A removal's lines exist only in base; asking head must not find them."""
        removed = self._spanning(10, 20, side=Side.BASE)
        assert events_at([removed], Side.HEAD, "src/a.cpp", 15) == []
        assert events_at([removed], Side.BASE, "src/a.cpp", 15) == [removed]


class TestCauseIsALinkNotARemoval:
    """ARCH-001 §6.3: one rename with 200 consequences is one event with 200 links."""

    def test_consequences_stay_in_the_set_and_keep_their_own_evidence(self) -> None:
        cause = event(EventKind.NODE_RENAMED, payload={"old_name": "f", "new_name": "g"})
        consequences = [
            Event(
                kind=EventKind.EDGE_RETARGETED,
                subject=EdgeId(f"c:@F@caller{i}#|calls|c:@F@f#"),
                payload={"old_dst": "c:@F@f#", "new_dst": "c:@F@g#"},
                caused_by=cause.id,
                confidence=CERTAIN,
            )
            for i in range(3)
        ]
        assert len({e.id for e in [cause, *consequences]}) == 4
        assert all(e.caused_by == cause.id for e in consequences)
        assert all(e.confidence is not None for e in consequences)

    def test_an_event_with_no_rule_keeps_no_cause_and_is_shown(self) -> None:
        """§6.3: when cause attribution is uncertain, default to showing."""
        assert event().caused_by is None
