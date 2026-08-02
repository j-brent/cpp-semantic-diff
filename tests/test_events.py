"""The delta schema -- ARCH-001 §6.

These assert the design, not the implementation. Every class below names the claim it
defends, because the failure mode for all of them is invisible in output: a schema that
admits a free-text kind, an event that quietly claims completeness it does not have, or a
provenance chain that only runs one way all produce plausible-looking JSON.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cpp_semantic_diff.model import (
    DERIVED_KINDS,
    EDGE_KINDS,
    NODE_KINDS,
    V0_KINDS,
    Completeness,
    Confidence,
    Event,
    EventKind,
    Hunk,
    Location,
    MatchTier,
    Side,
    TuResult,
    TuStatus,
    events_at,
)

CERTAIN = Confidence(tier=MatchTier.DECLARED, models_complete=True)


def event(kind: EventKind = EventKind.NODE_BODY_CHANGED, subject: str = "c:@F@f#", **kw) -> Event:
    return Event(kind=kind, subject=subject, confidence=CERTAIN, **kw)


def attr_changed(attr: str = "virtual", old: object = False, new: object = True, **kw) -> Event:
    return event(EventKind.NODE_ATTR_CHANGED, payload={"attr": attr, "old": old, "new": new}, **kw)


class TestTheSetIsClosed:
    """ARCH-001 §6.1: new signals become new kinds, never free text.

    Every downstream stage switches on the kind. A stringly-typed one turns each of those
    switches into a place where an unrecognized signal is silently dropped.
    """

    def test_kinds_are_exactly_the_ones_arch_names(self) -> None:
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
        """ARCH-001 §12 -- derived events are out of v0, but their kinds exist so consumers
        are extended rather than rewritten when they land."""
        assert set(EventKind) > V0_KINDS
        assert not V0_KINDS & DERIVED_KINDS

    def test_there_is_no_catch_all_kind(self) -> None:
        assert not {k for k in EventKind if k.name in {"OTHER", "CUSTOM", "UNKNOWN"}}

    def test_an_invented_kind_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(kind="SomethingChanged", subject="c:@F@f#", confidence=CERTAIN)

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
        """ARCH-001 §6.3, STATUS §6: collapse happens at render, never at diff.

        A `suppressed`/`collapsed` flag here would let the diff stage decide what a reviewer
        never sees. The JSON carries the full set; the view hides consequences under causes.
        """
        assert not {"suppressed", "collapsed", "hidden", "visible"} & set(Event.model_fields)


class TestPayloadStaysMechanical:
    """ARCH-001 §4.3, applied to events: the diff that produces a payload is mechanical."""

    @pytest.mark.parametrize("value", ["public", 3, 3.5, True, None])
    def test_scalars_are_accepted(self, value) -> None:
        assert attr_changed(new=value).payload["new"] == value

    @pytest.mark.parametrize("value", [{"nested": 1}, ["a"], (1, 2)])
    def test_structures_are_rejected(self, value) -> None:
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


class TestConfidenceIsAlwaysStated:
    """ARCH-001 §11: a tool that silently degrades converts absence of evidence into
    apparent evidence of absence."""

    def test_an_event_cannot_omit_its_confidence(self) -> None:
        with pytest.raises(ValidationError):
            Event(kind=EventKind.NODE_BODY_CHANGED, subject="c:@F@f#")

    def test_completeness_cannot_be_assumed(self) -> None:
        """No default: omitting it would be a silent claim that nothing was missing."""
        with pytest.raises(ValidationError):
            Confidence(tier=MatchTier.DECLARED)

    def test_the_match_tier_reaches_the_event(self) -> None:
        """ARCH-001 §5: the tier propagates into every event derived from the match."""
        assert attr_changed().confidence.tier is MatchTier.DECLARED

    def test_an_unmatched_subject_has_no_tier(self) -> None:
        """Add and remove events have no pair. In v0 that also covers every rename
        (§12) -- stating None is how the damage stays visible."""
        e = Event(
            kind=EventKind.NODE_ADDED,
            subject="c:@F@new#",
            confidence=Confidence(tier=None, models_complete=True),
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
                subject=f"c:@F@caller{i}#|calls|c:@F@f#",
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
