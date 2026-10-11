"""The derive stage -- ARCH-001 §3, ADR-040.

v0 derives one kind: `CallerOfChangedSignature`, one per call into a function whose signature
changed, where the same call existed before. Each class below names the claim it defends.

Models are built by hand; the stage is a pure function of the diff's events, two models and
a correspondence (§3).
"""

from __future__ import annotations

from typing import Any

from cpp_semantic_diff.derive import derive_events
from cpp_semantic_diff.diff import diff_models
from cpp_semantic_diff.match import match_models
from cpp_semantic_diff.model import (
    DERIVED_KINDS,
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
)

F = NodeId("c:@F@f#")
G = NodeId("c:@F@g#")
H = NodeId("c:@F@h#")
K = NodeId("c:@F@k#")
CALLER = ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE


def node(usr: str, name: str, **kw: Any) -> Node:
    defaults = {"kind": NodeKind.FUNCTION, "name": name, "qualified_name": name}
    return Node(id=NodeId(usr), **{**defaults, **kw})


def model(
    rev: str,
    *nodes: Node,
    edges: list[Edge] | None = None,
    completeness: Completeness | None = None,
) -> Model:
    kwargs: dict[str, Any] = {"rev": rev, "edges": edges or []}
    if completeness is not None:
        kwargs["completeness"] = completeness
    m = Model(**kwargs)
    for n in nodes:
        m.add_node(n)
    return m


def calls(src: str, dst: str, line: int = 1, kind: EdgeKind = EdgeKind.CALLS) -> Edge:
    return Edge(
        src=NodeId(src), dst=NodeId(dst), kind=kind, locations=[Location(file="a.cpp", line=line)]
    )


def f(signature: str, usr: str = F, **kw: Any) -> Node:
    return node(usr, "f", signature=signature, **kw)


def caller(usr: str, body: str = "same") -> Node:
    return node(usr, usr.split("@")[-1].rstrip("#"), signature="void ()", body_hash=body)


def derive(base: Model, head: Model) -> list[ChangeEvent]:
    corr = match_models(base, head)
    return derive_events(diff_models(base, head, corr), base, head, corr)


def callers(events: list[ChangeEvent]) -> list[ChangeEvent]:
    return [e for e in events if e.kind is CALLER]


class TestEachCallOnBothSidesIsReported:
    def test_an_unedited_caller_gets_one_event_saying_so(self) -> None:
        base = model("a", f("void (int)"), caller(G), edges=[calls(G, F, line=3)])
        head_edge = calls(G, F, line=4)
        head = model("b", f("void (double)"), caller(G), edges=[head_edge])
        (event,) = callers(derive(base, head))
        # ADR-040: "the subject is the head call edge" -- checked against the edge itself,
        # not a restated id format, so this fails only when the wrong edge is named.
        assert event.subject == head_edge.id
        assert event.payload == {
            "caller": G,
            "callee": F,
            "old_signature": "void (int)",
            "new_signature": "void (double)",
            "caller_edited": False,
        }
        # The call sites on both sides, so a reviewer gets from the event to the lines.
        assert {(h.side, h.location.line) for h in event.provenance} == {
            (Side.BASE, 3),
            (Side.HEAD, 4),
        }
        assert event.confidence.tier is MatchTier.DECLARED
        assert event.caused_by is None  # linking is classify's job

    def test_a_caller_whose_body_changed_is_marked_edited(self) -> None:
        base = model("a", f("void (int)"), caller(G, "old"), edges=[calls(G, F)])
        head = model("b", f("void (double)"), caller(G, "new"), edges=[calls(G, F)])
        (event,) = callers(derive(base, head))
        assert event.payload["caller_edited"] is True

    def test_a_caller_whose_own_signature_changed_is_marked_edited(self) -> None:
        base = model("a", f("void (int)"), caller(G), edges=[calls(G, F)])
        head = model(
            "b",
            f("void (double)"),
            node(G, "g", signature="int ()", body_hash="same"),
            edges=[calls(G, F)],
        )
        (event,) = callers(derive(base, head))
        assert event.payload["caller_edited"] is True

    def test_a_caller_of_two_changed_functions_gets_two_events(self) -> None:
        h_old, h_new = node(H, "h", signature="int ()"), node(H, "h", signature="long ()")
        base = model("a", f("void (int)"), h_old, caller(G), edges=[calls(G, F), calls(G, H)])
        head = model("b", f("void (double)"), h_new, caller(G), edges=[calls(G, F), calls(G, H)])
        events = callers(derive(base, head))
        # Exactly two, not merely "at least these two callees" -- the ADR's count claim.
        assert len(events) == 2
        assert {e.payload["callee"] for e in events} == {F, H}

    def test_a_name_paired_callee_is_reported_in_head_coordinates(self) -> None:
        """A function whose parameter types changed has one USR at base and another at head
        (ADR-036). The call maps through the correspondence, never by id."""
        old, new = f("void (int)", "c:@F@f#I#"), f("void (double)", "c:@F@f#d#")
        base = model("a", old, caller(G), edges=[calls(G, old.id)])
        head_edge = calls(G, new.id)
        head = model("b", new, caller(G), edges=[head_edge])
        (event,) = callers(derive(base, head))
        assert event.subject == head_edge.id
        assert event.payload["callee"] == new.id
        assert event.confidence.tier is MatchTier.NAME


class TestOnlyCallsThatExistedBeforeAreReported:
    """A new call was written against the new signature, and is already an `EdgeAdded`."""

    def test_a_new_caller_gets_no_event(self) -> None:
        base = model("a", f("void (int)"))
        head = model("b", f("void (double)"), caller(K), edges=[calls(K, F)])
        assert callers(derive(base, head)) == []

    def test_a_new_call_from_an_old_caller_gets_no_event(self) -> None:
        base = model("a", f("void (int)"), caller(G))
        head = model("b", f("void (double)"), caller(G, "new"), edges=[calls(G, F)])
        assert callers(derive(base, head)) == []

    def test_a_dropped_call_gets_no_event(self) -> None:
        base = model("a", f("void (int)"), caller(G), edges=[calls(G, F)])
        head = model("b", f("void (double)"), caller(G, "new"))
        assert callers(derive(base, head)) == []

    def test_an_edge_that_is_not_a_call_gets_no_event(self) -> None:
        base = model("a", f("void (int)"), caller(G), edges=[calls(G, F, kind=EdgeKind.OVERRIDES)])
        head = model(
            "b", f("void (double)"), caller(G), edges=[calls(G, F, kind=EdgeKind.OVERRIDES)]
        )
        assert callers(derive(base, head)) == []

    def test_a_caller_of_an_unchanged_function_gets_no_event(self) -> None:
        base = model("a", f("void (int)", body_hash="x"), caller(G), edges=[calls(G, F)])
        head = model("b", f("void (int)", body_hash="y"), caller(G), edges=[calls(G, F)])
        assert callers(derive(base, head)) == []


class TestCallerEditedReflectsOnlyBodyOrSignature:
    """callers.py: "A change to its attributes alone does not touch the call site's text,
    so it does not count." `_EDITS` is exactly `{NodeBodyChanged, NodeSignatureChanged}`."""

    def test_an_attribute_only_change_on_the_caller_is_not_marked_edited(self) -> None:
        old_g = node(G, "g", signature="void ()", body_hash="same", attrs={"is_const": False})
        new_g = node(G, "g", signature="void ()", body_hash="same", attrs={"is_const": True})
        base = model("a", f("void (int)"), old_g, edges=[calls(G, F)])
        head = model("b", f("void (double)"), new_g, edges=[calls(G, F)])
        (event,) = callers(derive(base, head))
        assert event.payload["caller_edited"] is False

    def test_a_function_calling_itself_counts_its_own_signature_change_as_an_edit(self) -> None:
        """F is both the caller and the callee of its own changed signature: `edited`
        must still pick up F's `NodeSignatureChanged`, not just a *different* node's."""
        base = model("a", f("void (int)"), edges=[calls(F, F)])
        head = model("b", f("void (double)"), edges=[calls(F, F)])
        (event,) = callers(derive(base, head))
        assert event.payload == {
            "caller": F,
            "callee": F,
            "old_signature": "void (int)",
            "new_signature": "void (double)",
            "caller_edited": True,
        }


class TestConfidenceCarriesModelCompleteness:
    """ARCH-001 §11: confidence is match tier *and* the completeness of both models.
    `DiffContext.confidence` threads this through `Confidence.derive`; nothing here pins a
    float -- only the `models_complete` flag the design actually specifies."""

    def test_a_failed_tu_on_base_marks_confidence_incomplete_even_with_a_complete_head(
        self,
    ) -> None:
        incomplete = Completeness(
            tus_total=2,
            tus=[
                TuResult(path="a.cpp", status=TuStatus.PARSED),
                TuResult(path="b.cpp", status=TuStatus.FAILED),
            ],
        )
        complete = Completeness(tus_total=1, tus=[TuResult(path="c.cpp", status=TuStatus.PARSED)])
        base = model("a", f("void (int)"), caller(G), edges=[calls(G, F)], completeness=incomplete)
        head = model("b", f("void (double)"), caller(G), edges=[calls(G, F)], completeness=complete)
        (event,) = callers(derive(base, head))
        # base incomplete, head complete: `and` gives False; `or` would wrongly give True.
        assert event.confidence.models_complete is False

    def test_a_failed_tu_on_head_marks_confidence_incomplete_even_with_a_complete_base(
        self,
    ) -> None:
        complete = Completeness(tus_total=1, tus=[TuResult(path="a.cpp", status=TuStatus.PARSED)])
        incomplete = Completeness(
            tus_total=2,
            tus=[
                TuResult(path="b.cpp", status=TuStatus.PARSED),
                TuResult(path="c.cpp", status=TuStatus.FAILED),
            ],
        )
        base = model("a", f("void (int)"), caller(G), edges=[calls(G, F)], completeness=complete)
        head = model(
            "b", f("void (double)"), caller(G), edges=[calls(G, F)], completeness=incomplete
        )
        (event,) = callers(derive(base, head))
        # base complete, head incomplete: `and` gives False, and only this direction catches
        # a `confidence` that reads base's completeness for both sides -- that mutant would
        # see base-complete twice and wrongly report True.
        assert event.confidence.models_complete is False

    def test_both_models_fully_parsed_marks_confidence_complete(self) -> None:
        base_complete = Completeness(
            tus_total=1, tus=[TuResult(path="a.cpp", status=TuStatus.PARSED)]
        )
        head_complete = Completeness(
            tus_total=2,
            tus=[
                TuResult(path="b.cpp", status=TuStatus.PARSED),
                TuResult(path="c.cpp", status=TuStatus.PARSED),
            ],
        )
        base = model(
            "a", f("void (int)"), caller(G), edges=[calls(G, F)], completeness=base_complete
        )
        head = model(
            "b", f("void (double)"), caller(G), edges=[calls(G, F)], completeness=head_complete
        )
        (event,) = callers(derive(base, head))
        assert event.confidence.models_complete is True


class TestTheStageOnlyAdds:
    def scenario(self) -> tuple[Model, Model]:
        base = model("a", f("void (int)"), caller(G), caller(H), edges=[calls(G, F), calls(H, F)])
        head = model(
            "b", f("void (double)"), caller(G), caller(H), edges=[calls(G, F), calls(H, F)]
        )
        return base, head

    def test_every_input_event_survives_unchanged(self) -> None:
        base, head = self.scenario()
        corr = match_models(base, head)
        events = diff_models(base, head, corr)
        derived = derive_events(events, base, head, corr)
        assert [e for e in derived if e.kind is not CALLER] == events
        assert len(callers(derived)) == 2

    def test_running_it_twice_adds_nothing(self) -> None:
        base, head = self.scenario()
        corr = match_models(base, head)
        once = derive_events(diff_models(base, head, corr), base, head, corr)
        assert derive_events(once, base, head, corr) == once

    def test_the_diff_stage_emits_no_derived_kind(self) -> None:
        """ADR-040: diff reads events off the models; deriving is a stage of its own."""
        base, head = self.scenario()
        assert not {e.kind for e in diff_models(base, head, match_models(base, head))} & (
            DERIVED_KINDS
        )
