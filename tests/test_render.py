"""The output contract -- ARCH-001 §10, and §6.3's collapse rule that only render obeys.

Every class below names a claim the design makes and defends *that*. Nothing here asserts a
layout, a colour or a phrase, because the terminal view is one consumer of the document and
the document is the product: a suite that pinned the view's wording would make the view the
thing under test and the contract the thing that quietly changed.

The claims that carry the most weight, in the order they would hurt most if broken:

1. **The document carries the complete event set even when the view collapses cascades.**
   §6.3 puts collapse at render precisely so this can be true, and it is the easiest thing in
   this stage to get wrong -- a collapse that filtered the list instead of nesting ids would
   look identical on screen and delete changes from the product.
2. **An event with no cause is never hidden.** §6.3: when cause attribution is uncertain,
   default to showing.
3. **The view derives everything from the document and nothing from the models.**
4. **The compression summary counts what it claims to count.**
5. **An incomplete model produces a visible warning, above the events.** §11 rule 2.
6. **The document round-trips**, because "JSON is the product" is otherwise aspirational.

Documents are built by hand. Nothing here needs libclang, a compile database or a fixture
clone -- the stage is a pure function of the previous stages' outputs (§3), following the
idiom of `test_diff.py` and `test_rank.py`.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from cpp_semantic_diff.acquire import RevPair
from cpp_semantic_diff.classify import classify_events
from cpp_semantic_diff.diff import diff_models
from cpp_semantic_diff.match import Correspondence, match_models
from cpp_semantic_diff.model import (
    AttrValue,
    Completeness,
    Confidence,
    Edge,
    EdgeKind,
    Event,
    EventId,
    EventKind,
    Location,
    MatchTier,
    Model,
    Node,
    NodeId,
    NodeKind,
    TuResult,
    TuStatus,
    Unpaired,
)
from cpp_semantic_diff.rank import rank_events
from cpp_semantic_diff.render import (
    SCHEMA_VERSION,
    SYSTEMATIC_LIMITS,
    DiffDocument,
    build_document,
    collapse,
    compression_sentence,
    render,
)
from cpp_semantic_diff.render.terminal import hidden_ids

PUBLIC = "include/lib/thing.h"

SURE = Confidence(tier=MatchTier.DECLARED, models_complete=True)
RESIDUE = Confidence(tier=None, unpaired=Unpaired.NONE, models_complete=True)


# -- builders ---------------------------------------------------------------------------


def node(usr: str, *, file: str = PUBLIC, kind: NodeKind = NodeKind.FUNCTION, **kw: Any) -> Node:
    name = kw.pop("name", usr.rsplit("@", 1)[-1] or usr)
    return Node(
        id=NodeId(usr),
        kind=kind,
        name=name,
        qualified_name=kw.pop("qualified_name", name),
        location=Location(file=file, line=kw.pop("line", 1)),
        **kw,
    )


def model(rev: str, *nodes: Node, edges: list[Edge] | None = None, **kw: Any) -> Model:
    m = Model(rev=rev, edges=list(edges or []), **kw)
    for n in nodes:
        m.add_node(n)
    return m


def edge(src: str, dst: str, kind: EdgeKind = EdgeKind.CALLS) -> Edge:
    return Edge(src=NodeId(src), dst=NodeId(dst), kind=kind)


def event(
    kind: EventKind, subject: str, confidence: Confidence = RESIDUE, **payload: AttrValue
) -> Event:
    return Event(kind=kind, subject=NodeId(subject), payload=payload, confidence=confidence)


REVS = RevPair(base_ref="main", head_ref="HEAD", base_sha="a" * 40, head_sha="b" * 40)


def document(
    base: Model,
    head: Model,
    events: list[Event],
    *,
    correspondence: Correspondence | None = None,
    lines_changed: int = 100,
    files_renamed: int = 0,
) -> DiffDocument:
    corr = correspondence if correspondence is not None else match_models(base, head)
    return build_document(
        repo="fixture",
        revs=REVS,
        base=base,
        head=head,
        correspondence=corr,
        events=events,
        ranking=rank_events(events, base, head),
        lines_changed=lines_changed,
        files_changed=3,
        cpp_files_changed=2,
        files_renamed=files_renamed,
    )


def pipeline(base: Model, head: Model, *, lines_changed: int = 100) -> DiffDocument:
    """The real stages, end to end, with no libclang -- models are the only input they need."""
    corr = match_models(base, head)
    events = classify_events(diff_models(base, head, corr), base, head, corr)
    return document(base, head, events, correspondence=corr, lines_changed=lines_changed)


def draw(doc: DiffDocument, **kw: Any) -> str:
    """The terminal view as plain text. Width is fixed so a wrap is never a test failure."""
    buffer = io.StringIO()
    render(doc, Console(file=buffer, width=200, no_color=True, highlight=False), **kw)
    return buffer.getvalue()


#: One toolchain for both sides of every comparison below. `diff` refuses two models whose
#: producers disagree (ARCH-001 §12.2), and a side left blank beside a side that names a
#: producer is a disagreement -- so the *coverage* asymmetry these tests are about has to be
#: built on identity the two sides share, exactly as a real run does.
PRODUCER = "libclang 18.1.3"


def incomplete(reason: str = "3 TUs failed") -> Completeness:
    return Completeness(
        tus_total=2,
        tus=[
            TuResult(path="src/a.cpp", status=TuStatus.PARSED),
            TuResult(path="src/b.cpp", status=TuStatus.FAILED, detail=reason),
        ],
        producer_version=PRODUCER,
    )


def clean() -> Completeness:
    """The other side of an `incomplete()` comparison: nothing missing, same producer."""
    return Completeness(producer_version=PRODUCER)


# -- 1 · the §6.3 invariant --------------------------------------------------------------


class TestDocumentCarriesEverything:
    """**The JSON is complete even when the view collapses.** ARCH-001 §6.3.

    This is the invariant the whole stage is arranged around, and the one whose violation
    is invisible: a collapse that filtered rather than nested would render identically and
    would delete changes from the product every other view reads.
    """

    def test_a_collapsed_consequence_is_still_in_the_document(self) -> None:
        # A removed class and the member it contained: exactly ARCH-001 §6.3's
        # contained-removal rule, produced by the real diff and classify stages.
        cls = node("c:@S@C", kind=NodeKind.CLASS, name="C")
        member = node("c:@S@C@F@m#", name="m", qualified_name="C::m")
        base = model("a", cls, member, edges=[edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS)])
        doc = pipeline(base, model("b"))

        caused = [e for e in doc.events if e.caused_by is not None]
        assert caused, "the fixture must produce a cascade or it is testing nothing"

        tree = collapse(doc, enabled=True)
        hidden = hidden_ids(tree)
        assert hidden, "the view must actually be nesting something"

        # The claim: nested in the view, present in the document.
        assert hidden <= {e.id for e in doc.events}
        assert doc.summary.events_reported == len(doc.events)

    def test_collapsing_changes_shape_but_never_the_set(self) -> None:
        """`--expand` and the default must differ in nesting only, never in content.

        A reviewer who distrusts the collapse has to be able to turn it off and know they
        are seeing the same delta rather than a second opinion about it.
        """
        cls = node("c:@S@C", kind=NodeKind.CLASS, name="C")
        member = node("c:@S@C@F@m#", name="m", qualified_name="C::m")
        base = model("a", cls, member, edges=[edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS)])
        doc = pipeline(base, model("b"))

        nested = collapse(doc, enabled=True)
        flat = collapse(doc, enabled=False)

        assert set(nested.flatten()) == set(flat.flatten())
        assert len(flat.roots) > len(nested.roots), "expanded must show more at top level"
        assert flat.hidden == 0

    def test_every_event_appears_exactly_once_in_the_collapse_tree(self) -> None:
        cls = node("c:@S@C", kind=NodeKind.CLASS, name="C")
        member = node("c:@S@C@F@m#", name="m", qualified_name="C::m")
        base = model("a", cls, member, edges=[edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS)])
        doc = pipeline(base, model("b"))

        flattened = collapse(doc).flatten()
        assert sorted(flattened) == sorted(e.id for e in doc.events)
        assert len(flattened) == len(set(flattened))


class TestNothingIsHiddenWithoutACause:
    """**When cause attribution is uncertain, show.** ARCH-001 §6.3.

    Three ways a link can be missing or unusable, and all three must surface the event at
    top level rather than losing it.
    """

    def _doc(self, events: list[Event]) -> DiffDocument:
        f = node("c:@F@f#", name="f")
        g = node("c:@F@g#", name="g")
        return document(model("a", f, g), model("b", f, g), events)

    def test_an_event_with_no_cause_is_top_level(self) -> None:
        doc = self._doc(
            [
                event(EventKind.NODE_BODY_CHANGED, "c:@F@f#", SURE),
                event(EventKind.NODE_BODY_CHANGED, "c:@F@g#", SURE),
            ]
        )
        tree = collapse(doc)
        assert {r.event_id for r in tree.roots} == {e.id for e in doc.events}
        assert tree.hidden == 0

    def test_a_dangling_cause_promotes_rather_than_hides(self) -> None:
        """A `caused_by` naming an event the document does not carry.

        It should be impossible -- ids are content-derived and classify never invents one --
        which is exactly why it must not be trusted. Burying the event under a cause that
        is not there would make it invisible in every view at once.
        """
        # `model_copy`, because `Event` is frozen (ARCH-001 §3): the way to get a modified
        # event is to make one, which is also what classify does to set this same field.
        orphan = event(EventKind.NODE_BODY_CHANGED, "c:@F@f#", SURE).model_copy(
            update={"caused_by": EventId("NodeRemoved:deadbeef")}
        )
        doc = self._doc([orphan])

        tree = collapse(doc)
        assert [r.event_id for r in tree.roots] == [orphan.id]
        assert tree.promoted == (orphan.id,), "the refusal must be reported, not silent"

    def test_a_cycle_promotes_both_rather_than_hiding_either(self) -> None:
        """Classify refuses cycles, but a document can arrive from a file or a later stage.

        A cycle is precisely the case where "which of these is the cause" has no answer, so
        §6.3 says show -- and showing both is the only reading that loses nothing.
        """
        first = event(EventKind.NODE_BODY_CHANGED, "c:@F@f#", SURE)
        second = event(EventKind.NODE_BODY_CHANGED, "c:@F@g#", SURE)
        # Frozen events, so the cycle is built by copying rather than by assignment. The
        # ids are unchanged by the copy -- they exclude `caused_by` on purpose -- which is
        # what makes each of these still point at the other.
        a = first.model_copy(update={"caused_by": second.id})
        b = second.model_copy(update={"caused_by": first.id})
        doc = self._doc([a, b])

        tree = collapse(doc)
        assert sorted(collapse(doc).flatten()) == sorted([a.id, b.id])
        assert {r.event_id for r in tree.roots} == {a.id, b.id}
        assert set(tree.promoted) == {a.id, b.id}

    def test_nesting_follows_the_chain_one_level_at_a_time(self) -> None:
        """Classify links to the *immediate* container so that render can nest (§6.3).

        A namespace containing a class containing a method, all removed, must read as three
        levels. A flat link to the outermost ancestor would have collapsed the structure
        into one, which is the thing `classify/rules.py` says it avoids *for this stage*.
        """
        ns = node("c:@N@n", kind=NodeKind.NAMESPACE, name="n")
        cls = node("c:@N@n@S@C", kind=NodeKind.CLASS, name="C", qualified_name="n::C")
        member = node("c:@N@n@S@C@F@m#", name="m", qualified_name="n::C::m")
        base = model(
            "a",
            ns,
            cls,
            member,
            edges=[
                edge("c:@N@n", "c:@N@n@S@C", EdgeKind.CONTAINS),
                edge("c:@N@n@S@C", "c:@N@n@S@C@F@m#", EdgeKind.CONTAINS),
            ],
        )
        doc = pipeline(base, model("b"))
        tree = collapse(doc)

        depths = {
            doc.by_id[n.event_id].subject: d
            for root in tree.roots
            for n, d in root.walk()
            if doc.by_id[n.event_id].kind is EventKind.NODE_REMOVED
        }
        assert (
            depths[NodeId("c:@N@n")]
            < depths[NodeId("c:@N@n@S@C")]
            < depths[NodeId("c:@N@n@S@C@F@m#")]
        )


# -- 2 · the view is a consumer -----------------------------------------------------------


class TestTheViewReadsOnlyTheDocument:
    """**JSON is the product; the terminal is a consumer.** ARCH-001 §10.

    Enforced structurally rather than by convention: the view is handed a document and
    nothing else, so a view that needs a `Model` cannot compile a call to get one.
    """

    def test_render_accepts_a_document_and_no_model(self) -> None:
        import inspect

        params = set(inspect.signature(render).parameters)
        assert "document" in params
        assert not params & {"base", "head", "model", "models", "correspondence", "ranking"}

    def test_a_document_read_back_from_json_renders_identically(self) -> None:
        """The strongest available proof that the view holds no analysis: a round-tripped
        document has no models behind it at all, so anything the view could still print
        came out of the JSON."""
        f = node("c:@F@f#", name="f")
        g = node("c:@F@g#", name="g")
        doc = pipeline(model("a", f), model("b", f, g))

        assert draw(DiffDocument.from_json(doc.to_json())) == draw(doc)

    def test_the_view_names_entities_it_was_given_summaries_for(self) -> None:
        f = node("c:@F@f#", name="frobnicate", qualified_name="lib::frobnicate")
        doc = pipeline(model("a", f), model("b"))
        assert "lib::frobnicate" in draw(doc)

    def test_the_view_explains_the_ranking_from_the_carried_factors(self) -> None:
        """ARCH-001 §7 and `RiskScore.factors`: `detail` exists so a view can say *why*
        something is first without recomputing a score. If the view stopped printing it,
        the ranking would become an unarguable assertion."""
        f = node("c:@F@f#", name="f")
        doc = pipeline(model("a", f), model("b"))
        out = draw(doc)

        details = {factor.detail for score in doc.scores.values() for factor in score.factors}
        assert any(detail and detail in out for detail in details)


# -- 3 · the metric -----------------------------------------------------------------------


class TestCompressionCountsWhatItClaims:
    """**§10's metric is the experiment this project runs on itself**, so it is computed
    honestly or it is worthless. The numerator is the complete event set: not the visible
    set, not the top-level set, not a filtered one."""

    def test_the_numerator_is_the_full_event_list_not_the_collapsed_one(self) -> None:
        cls = node("c:@S@C", kind=NodeKind.CLASS, name="C")
        member = node("c:@S@C@F@m#", name="m", qualified_name="C::m")
        base = model("a", cls, member, edges=[edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS)])
        doc = pipeline(base, model("b"), lines_changed=200)

        assert doc.summary.events_reported == len(doc.events)
        assert doc.summary.top_level_events < doc.summary.events_reported, (
            "the fixture must collapse something, or the distinction is untested"
        )
        assert doc.summary.compression_ratio == 200 / len(doc.events)
        assert str(len(doc.events)) in doc.summary.compression

    def test_the_denominator_is_the_text_diff_as_measured(self) -> None:
        """`lines_changed` is `acquire.diff_line_count` -- the number `cppsd plan` reports
        and the number a reviewer would otherwise have read. Derived here, it would be a
        different claim."""
        f = node("c:@F@f#", name="f")
        doc = pipeline(model("a", f), model("b"), lines_changed=2431)
        assert doc.summary.lines_changed == 2431
        assert "2,431 lines changed" in doc.summary.compression

    def test_high_risk_count_comes_from_the_rank_stage(self) -> None:
        f = node("c:@F@f#", name="f")
        base, head = model("a", f), model("b")
        events = classify_events(
            diff_models(base, head, match_models(base, head)),
            base,
            head,
            match_models(base, head),
        )
        ranking = rank_events(events, base, head)
        doc = document(base, head, events)
        assert doc.summary.high_risk_count == ranking.high_risk_count

    def test_no_events_is_reported_as_no_events_not_as_infinite_compression(self) -> None:
        """A large diff with an empty delta is the single most important negative result
        this tool can produce, and a sentinel ratio would let a consumer average over it."""
        f = node("c:@F@f#", name="f")
        doc = pipeline(model("a", f), model("b", f), lines_changed=900)
        assert doc.summary.events_reported == 0
        assert doc.summary.compression_ratio is None
        assert "no semantic change" in doc.summary.compression

    def test_the_sentence_has_the_shape_arch_10_prints(self) -> None:
        assert (
            compression_sentence(2431, 9, 2)
            == "2,431 lines changed -> 9 semantic events, 2 high-risk"
        )

    def test_the_ratio_does_not_straddle_two_universes(self) -> None:
        """Measured on leveldb: 52 lines changed, 3,794 events, 0.0 lines per event (commits
        `2651354` and `8046af5`, 2026-08-04).

        The denominator is lines of *text diff*, and git renders a submodule bump as two
        lines rather than the hundred thousand inside it. Dividing that by every modelled
        event compares a first-party denominator against a whole-world numerator, so the two
        sides have to be taken over the same universe or the number means nothing.
        """
        own = node("c:@F@own#", name="own", file="src/lib.cc")
        deps = [
            node(f"c:@F@d{i}#", name=f"d{i}", file="third_party/gtest/gtest.cc") for i in range(9)
        ]
        doc = pipeline(model("a", own, *deps), model("b", *deps), lines_changed=10)

        assert doc.summary.events_reported == 1, "one first-party removal"
        assert doc.summary.vendored_events == 0, "the dependency itself did not change here"
        assert doc.summary.compression_ratio == 10 / 1

    def test_a_dependency_bump_does_not_flatten_the_ratio(self) -> None:
        own = node("c:@F@own#", name="own", file="src/lib.cc")
        deps = [
            node(f"c:@F@d{i}#", name=f"d{i}", file="third_party/gtest/gtest.cc") for i in range(20)
        ]
        doc = pipeline(model("a", own, *deps), model("b"), lines_changed=10)

        assert doc.summary.vendored_events == 20
        assert doc.summary.events_reported == 21, "the complete set is still reported"
        assert doc.summary.compression_ratio == 10 / 1, (
            "10 lines over the one first-party event, not over all 21"
        )

    def test_a_bump_that_touches_nothing_of_yours_says_so(self) -> None:
        """Distinct from 'no semantic change': something did change, none of it yours."""
        deps = [
            node(f"c:@F@d{i}#", name=f"d{i}", file="third_party/gtest/gtest.cc") for i in range(4)
        ]
        doc = pipeline(model("a", *deps), model("b"), lines_changed=10)

        assert doc.summary.compression_ratio is None
        assert "bundled dependencies" in doc.summary.compression
        assert "no semantic change" not in doc.summary.compression

    def test_high_risk_does_not_count_what_the_reviewer_is_not_shown(self) -> None:
        """On leveldb 16 of 21 high-risk events were vendored (commit `562921c`, 2026-08-04). A
        headline promising sixteen dangerous changes the view never shows is worse than no
        headline."""
        dependents = [node(f"c:@F@x{i}#", file="third_party/gtest/gtest.cc") for i in range(40)]
        base = model(
            "a",
            node("c:@F@assert_helper#", file="third_party/gtest/gtest.cc"),
            *dependents,
            edges=[edge(d.id, "c:@F@assert_helper#") for d in dependents],
        )
        doc = pipeline(base, model("b", *dependents), lines_changed=10)

        assert doc.summary.vendored_events >= 1
        assert doc.summary.high_risk_count == 0

    def test_the_view_prints_the_sentence_the_document_carries(self) -> None:
        """One claim, one place. A view that composed its own could quietly disagree with
        the JSON about the only number the project is testing."""
        f = node("c:@F@f#", name="f")
        doc = pipeline(model("a", f), model("b"), lines_changed=2431)
        assert doc.summary.compression in draw(doc)


# -- 4 · honesty about the model ----------------------------------------------------------


class TestIncompletenessIsVisible:
    """ARCH-001 §11 rule 2: "any output built on an incomplete model states what was missing, at
    the top, not in a log." """

    def _doc(self, base_c: Completeness, head_c: Completeness) -> DiffDocument:
        f = node("c:@F@f#", name="f")
        return pipeline(
            model("a", f, completeness=base_c),
            model("b", completeness=head_c),
        )

    def test_a_failed_tu_is_warned_about_and_named(self) -> None:
        doc = self._doc(incomplete("stddef.h not found"), clean())
        assert doc.completeness.degraded
        out = draw(doc)
        assert "incomplete" in out
        assert "src/b.cpp" in out and "stddef.h not found" in out

    def test_the_reason_a_tu_failed_reaches_the_page_whole(self) -> None:
        """§11 rule 2 asks for what was missing, which makes a mangled reason worse than a loud
        one. A clang diagnostic quotes C++ and C++ is bracketed -- `[[nodiscard]]`, a
        template argument list, clang's own `[note]` -- and the terminal's markup parser
        drops a bracketed run it cannot style. So the disclosure arrived with the cause
        deleted and still looked like a disclosure. The claim held here is not about
        escaping: it is that the page carries the whole reason the document carries,
        whatever characters happen to be in it.
        """
        reason = "ignoring return value of function declared [[nodiscard]]; see [note] above"
        doc = self._doc(incomplete(reason), clean())

        carried = doc.completeness.base.failures[0].detail
        assert carried == reason, "the document must be the thing that is intact"
        assert carried in draw(doc), "the view printed less than the document gave it"

    def test_the_warning_precedes_the_events(self) -> None:
        """Order is the whole of §11 rule 2's claim: a reader who reaches the events first has
        already formed a belief the caveat then has to undo."""
        doc = self._doc(incomplete(), clean())
        out = draw(doc)
        assert out.index("incomplete") < out.index("NodeRemoved")

    def test_a_note_alone_is_enough_to_warn(self) -> None:
        """A note is the builder's channel for anything with no field of its own -- a cache
        that invalidated everything, entries that were not C++. `tus_complete` cannot see
        any of it, so the banner must not be driven by that boolean alone."""
        noted = Completeness(
            tus_total=1,
            tus=[TuResult(path="src/a.cpp", status=TuStatus.PARSED)],
            notes=["7 of 8 cached translation units were invalidated and reparsed"],
            producer_version="libclang 18.1.3",
        )
        assert noted.is_complete, "the gap being defended against must still exist"

        doc = self._doc(noted, noted)
        assert doc.completeness.base.tus_complete
        assert doc.completeness.degraded, "tus_complete alone must not decide the banner"
        assert "invalidated and reparsed" in draw(doc)


class TestTheGraphBoundaryIsStatedNumerically:
    """**The gap this class exists for**, and the shape of the fix.

    `Completeness.is_complete` counts TU parse success *only*. On real fmt the builder dropped
    5,113 of 26,071 distinct edges per side, measured 2026-08-02 (v0 tracker §5,
    reports/2026-08-01--v0-build/STATUS.md) -- an endpoint that no declaration in the
    repository owns -- and `is_complete` is still True. That loss used to reach the output as
    one English sentence in `Completeness.notes`, which meant two things at once: a consumer
    could report it only by matching on prose (ARCH-001 §10 makes the JSON the product), and
    the terminal announced *every* fmt and spdlog run as "this model is incomplete", which is
    how a §11 signal stops being read.

    So it is carried as two integers and stated on every run, and it does not raise the
    coverage alarm. The distinction is the claim: a failed TU hides something that is there;
    a dropped edge points at something that was never in scope.
    """

    def _doc(self, base_c: Completeness, head_c: Completeness) -> DiffDocument:
        f = node("c:@F@f#", name="f")
        return pipeline(model("a", f, completeness=base_c), model("b", completeness=head_c))

    @staticmethod
    def _lossy(total: int = 26_071, dropped: int = 5_113) -> Completeness:
        return Completeness(
            tus_total=1,
            tus=[TuResult(path="src/a.cpp", status=TuStatus.PARSED)],
            edges_total=total,
            edges_dropped=dropped,
            producer_version="libclang 18.1.3",
        )

    def test_the_numbers_reach_the_document(self) -> None:
        """The property that makes every other consumer possible: a view, an HTML report or
        a PR annotation can state the fact arithmetically without re-reading a model."""
        report = self._doc(self._lossy(), self._lossy()).completeness.base
        assert (report.edges_total, report.edges_dropped) == (26_071, 5_113)
        assert not report.edges_closed
        assert 0.19 < report.edge_residue < 0.20

    def test_a_model_that_lost_a_fifth_of_its_edges_says_so_on_screen(self) -> None:
        out = draw(self._doc(self._lossy(), self._lossy()))
        assert "5,113 of 26,071" in out and "19.6%" in out

    def test_it_is_stated_on_a_run_that_is_otherwise_clean(self) -> None:
        """The half that a warning-shaped design gets wrong. Nothing failed here, so there
        is no banner -- and the reader still has to be told that a fifth of the call graph
        leads somewhere the model cannot see."""
        doc = self._doc(self._lossy(), self._lossy())
        assert not doc.completeness.degraded, "boundary residue is not a coverage failure"
        assert doc.completeness.boundaries, "and it must still be stated"
        assert "19.6%" in draw(doc)

    def test_it_does_not_drown_the_signal_that_something_went_wrong(self) -> None:
        """Every real repository calls `std::` something, so a residue-driven banner would
        fire on every run and the reader would learn to skip the block that also carries the
        TU that failed to parse."""
        assert not self._doc(self._lossy(), self._lossy()).completeness.degraded
        assert self._doc(incomplete(), self._lossy()).completeness.degraded

    def test_a_closed_graph_says_nothing(self) -> None:
        """No sentence when there is nothing to report -- otherwise the line is furniture
        and stops being read, which is the same failure one level down."""
        clean = Completeness(
            tus_total=1,
            tus=[TuResult(path="src/a.cpp", status=TuStatus.PARSED)],
            edges_total=12,
            edges_dropped=0,
        )
        doc = self._doc(clean, clean)
        assert doc.completeness.base.boundary is None
        assert doc.completeness.boundaries == []

    def test_the_percentage_is_what_makes_the_count_readable(self) -> None:
        """One dropped count is routine against a large graph and a catastrophe against a small
        one. There is deliberately no threshold at which this becomes a warning -- a threshold
        with no calibration behind it is a guess presented as a policy -- so the ratio has to
        carry that judgement to the reader instead."""
        out = draw(self._doc(self._lossy(total=6_000, dropped=5_113), self._lossy()))
        assert "85.2%" in out

    def test_it_survives_the_json_round_trip(self) -> None:
        """§10: a consumer reading the document off disk gets what the pipeline built."""
        doc = self._doc(self._lossy(), self._lossy())
        again = DiffDocument.from_json(doc.to_json())
        assert again.completeness.base.edges_dropped == 5_113
        assert again.completeness.boundaries == doc.completeness.boundaries

    def test_a_clean_model_still_states_the_systematic_limits(self) -> None:
        """§11: two limits "get stated in the output itself, every time" -- and the output
        is the document, so they are data rather than a line in one view."""
        doc = self._doc(Completeness(), Completeness())
        assert doc.completeness.limits == list(SYSTEMATIC_LIMITS)
        out = draw(doc)
        for limit in SYSTEMATIC_LIMITS:
            assert limit.split(".")[0] in out

    def test_a_clean_model_does_not_claim_to_be_incomplete(self) -> None:
        doc = self._doc(
            Completeness(tus_total=1, tus=[TuResult(path="a", status=TuStatus.PARSED)]),
            Completeness(tus_total=1, tus=[TuResult(path="a", status=TuStatus.PARSED)]),
        )
        assert not doc.completeness.degraded


class TestTheResidueIsReported:
    """ARCH-001 §13: v0 ships tier-1 identity only, and *measuring the damage* is the
    deliverable. A measurement that appears only as an inflated event count is not one."""

    def test_a_rename_reads_as_a_removal_plus_an_addition_and_says_so(self) -> None:
        before = node("c:@F@old#", name="old")
        after = node("c:@F@new#", name="new")
        doc = pipeline(model("a", before), model("b", after))

        kinds = sorted(e.kind for e in doc.events)
        assert kinds == [EventKind.NODE_ADDED, EventKind.NODE_REMOVED]

        r = doc.summary.residue
        assert (r.unmatched_base, r.unmatched_head) == (1, 1)
        assert r.tiers_run == [MatchTier.DECLARED]
        assert r.pairable_residue == 1, "the upper bound on what tiers 2-4 could remove"

        out = draw(doc)
        assert "tier declared only" in out
        assert "removal plus an addition" in out

    def test_bundled_dependency_churn_is_named_not_hidden(self) -> None:
        """A dependency bump can be 96 % of the delta (leveldb, measured 2026-08-04, v0 tracker
        §2, reports/2026-08-01--v0-build/STATUS.md). Ranking puts it last, so a top-N view
        stops showing it -- which means the count has to be stated, or "almost nothing changed"
        and "almost everything was set aside" look identical."""
        own = node("c:@F@own#", name="own", file="src/lib.cc")
        dep = node("c:@F@dep#", name="dep", file="third_party/gtest/gtest.cc")
        doc = pipeline(model("a", own, dep), model("b", own))

        assert doc.summary.vendored_events == 1
        assert doc.summary.events_reported == 1, "the numerator is still the whole set"
        assert "bundled dependencies" in doc.summary.compression

    def test_the_numerator_never_shrinks_when_a_dependency_moves(self) -> None:
        """This is the one figure nobody can audit from outside. Naming vendored churn beside
        it is honest; subtracting it would be the redefinition that stops the metric measuring
        anything."""
        deps = [
            node(f"c:@F@d{i}#", name=f"d{i}", file="third_party/gtest/gtest.cc") for i in range(5)
        ]
        doc = pipeline(model("a", *deps), model("b"))
        assert doc.summary.events_reported == 5
        assert doc.summary.vendored_events == 5

    def test_resolved_renames_reach_the_document(self) -> None:
        """ARCH-001 §5's one identity input that comes from outside the models.

        Without it in the JSON, two runs that differ only in whether a rename was seen are
        indistinguishable to a consumer -- and they produce very different deltas.
        """
        f = node("c:@F@f#", name="f")
        doc = document(model("a", f), model("b", f), [], files_renamed=2)
        assert doc.summary.files_renamed == 2

    def test_the_rename_caveat_is_stated_in_the_direction_that_is_true(self) -> None:
        """A caveat that is false where it matters most teaches a reader to discount it.

        Renames are resolved from git and applied now, so "a rename reads as a removal plus
        an addition" must not be printed on a run where git found one and the matcher used
        it. The claim flips instead of being dropped: what is still true is that a rename
        git *could not* detect reads that way.
        """
        f = node("c:@F@f#", name="f")
        base, head = model("a", f), model("b", f)

        applied = draw(document(base, head, [], files_renamed=1))
        assert "1 file rename resolved from git" in applied
        assert "removal plus an addition" not in applied

        none_found = draw(document(base, head, [], files_renamed=0))
        assert "git found no renames" in none_found
        assert "removal plus an addition" in none_found


# -- 5 · the contract itself ---------------------------------------------------------------


class TestTheDocumentIsTheContract:
    def test_it_has_exactly_the_keys_arch_10_names(self) -> None:
        f = node("c:@F@f#", name="f")
        doc = pipeline(model("a", f), model("b"))
        keys = set(doc.model_dump().keys())
        assert {
            "meta",
            "completeness",
            "entities",
            "events",
            "findings",
            "ranking",
            "summary",
        } <= keys

    def test_it_round_trips(self) -> None:
        """ "JSON is the product" is aspirational unless a document read off disk is the
        document the pipeline produced -- byte for byte, not merely equivalent."""
        cls = node("c:@S@C", kind=NodeKind.CLASS, name="C")
        member = node("c:@S@C@F@m#", name="m", qualified_name="C::m")
        base = model("a", cls, member, edges=[edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS)])
        doc = pipeline(base, model("b", node("c:@F@g#", name="g")))

        again = DiffDocument.from_json(doc.to_json())
        assert again.to_json() == doc.to_json()
        assert again == doc
        assert [e.caused_by for e in again.events] == [e.caused_by for e in doc.events]

    def test_it_is_a_function_of_the_two_commits_and_not_of_the_clock(self) -> None:
        """Two runs over the same inputs must produce the same bytes, timings aside. §10's
        product is diffable only if it is reproducible, and `rank/ranker.py` makes the same
        promise one stage earlier."""
        f = node("c:@F@f#", name="f")
        base, head = model("a", f), model("b")
        first, second = pipeline(base, head), pipeline(base, head)
        assert first.model_dump(exclude={"meta"}) == second.model_dump(exclude={"meta"})

    def test_findings_is_present_and_empty_in_v0(self) -> None:
        """§8 is deferred (§13); the key exists so that a consumer written today extends
        rather than changes the day the first adapter lands."""
        doc = pipeline(model("a"), model("b"))
        assert doc.findings == []

    def test_it_declares_a_schema_version(self) -> None:
        doc = pipeline(model("a"), model("b"))
        assert doc.meta.schema_version == SCHEMA_VERSION

    def test_ranking_is_the_reading_order(self) -> None:
        """ARCH-001 §7, RESEARCH-002 §4: telling the reviewer what to look at first may be
        worth more than any individual diff, so the order is a first-class output."""
        f = node("c:@F@f#", name="f")
        g = node("c:@F@g#", name="g")
        doc = pipeline(model("a", f, g), model("b", f))

        assert sorted(doc.ranking) == sorted(e.id for e in doc.events)
        totals = [doc.scores[i].total for i in doc.ranking]
        assert totals == sorted(totals, reverse=True)
        assert [e.id for e in doc.ranked()] == doc.ranking

    def test_entities_cover_every_subject_the_models_carry(self) -> None:
        """An event whose subject cannot be named renders as a raw USR. Both endpoints of
        an edge subject count -- a view that could name the edge but neither end of it is
        not a view."""
        f = node("c:@F@f#", name="f")
        g = node("c:@F@g#", name="g")
        base = model("a", f, g, edges=[edge("c:@F@f#", "c:@F@g#")])
        doc = pipeline(base, model("b", f, g))

        from cpp_semantic_diff.rank import subject_endpoints

        for e in doc.events:
            for endpoint in subject_endpoints(e.subject):
                assert endpoint in doc.entities, f"{e.kind.value} names an entity nobody can"

    def test_an_entity_says_which_side_it_is_on(self) -> None:
        f = node("c:@F@f#", name="f")
        gone = node("c:@F@gone#", name="gone")
        added = node("c:@F@added#", name="added")
        doc = pipeline(model("a", f, gone), model("b", f, added))

        assert doc.entities[NodeId("c:@F@gone#")].sides == ["base"]
        assert doc.entities[NodeId("c:@F@added#")].sides == ["head"]

    def test_overloads_are_labelled_apart(self) -> None:
        """Two entities sharing a qualified name is ordinary C++, and a view that printed
        the bare name twice would read as a bug in the tool rather than as two changes."""
        one = node("c:@F@f#i#", name="f", signature="void (int)")
        two = node("c:@F@f#d#", name="f", signature="void (double)")
        doc = pipeline(model("a", one, two), model("b"))

        labels = {doc.entity_name(NodeId(n.id)) for n in (one, two)}
        assert len(labels) == 2
        assert all("f" in label for label in labels)


# -- 6 · a label is not an identity, and must not pretend to be one ------------------------

#: The two fmt entities a misfiled defect was once written from (v0 tracker §5,
#: reports/2026-08-01--v0-build/STATUS.md): a class template and a partial specialization of
#: it, with the `inherits` edge between them. Real USRs, because the distinction between them
#: survives in the USR and nowhere else on the summary -- both are `struct`, both have no
#: signature, and `qualified_name` is the same string.
TEMPLATE_USR = "c:@N@fmt@N@v12@N@detail@ST>1#T@is_named_arg"
PARTIAL_USR = "c:@N@fmt@N@v12@N@detail@SP>1#T@is_named_arg>#1t0.0"
SHARED_NAME = "fmt::v12::detail::is_named_arg"


def record(usr: str, *, line: int) -> Node:
    return node(
        usr,
        kind=NodeKind.STRUCT,
        name="is_named_arg",
        qualified_name=SHARED_NAME,
        line=line,
    )


def specialization_document(*nodes: Node) -> DiffDocument:
    """Both records added on the head side, with the `inherits` edge the producer emits."""
    head = model("b", *nodes, edges=[edge(PARTIAL_USR, TEMPLATE_USR, EdgeKind.INHERITS)])
    return pipeline(model("a"), head)


class TestALabelNamesOneEntity:
    """**Two entities never share a display name.**

    The claim is about the *design of the rule*, not about the words it produces, so nothing
    below pins a formatted label. What is defended is that the rule separates entities the
    previous rule collapsed, that it separates them from facts the document already carries,
    and -- the part that would be a worse defect than the one it fixes -- that it never
    separates them by the order they happened to arrive in.
    """

    def test_records_sharing_a_qualified_name_are_labelled_apart(self) -> None:
        """A class template and a partial specialization of it share a qualified name and
        have no signature, so the overload rule -- the only one there used to be -- left
        both printing as the same string, and the `inherits` edge between them rendered as
        a self-edge. That was filed against the producer, which was right all along."""
        doc = specialization_document(
            record(TEMPLATE_USR, line=1056), record(PARTIAL_USR, line=1062)
        )

        labels = {doc.entity_name(NodeId(usr)) for usr in (TEMPLATE_USR, PARTIAL_USR)}
        assert len(labels) == 2
        assert all(SHARED_NAME in label for label in labels)

    def test_the_edge_between_them_no_longer_reads_as_a_self_edge(self) -> None:
        """The surface the defect was reported from. `label` resolves both endpoints of an
        edge subject, so this is the rendering a reviewer actually sees."""
        doc = specialization_document(
            record(TEMPLATE_USR, line=1056), record(PARTIAL_USR, line=1062)
        )
        edges = [e for e in doc.events if e.kind is EventKind.EDGE_ADDED]
        rendered = [doc.label(e.subject) for e in edges]

        self_edges = [r for r in rendered if len(set(r.split(" -> "))) == 1]
        assert not self_edges, f"an edge between two entities renders as one: {self_edges}"

    def test_the_label_does_not_depend_on_the_order_entities_arrived(self) -> None:
        """Two identical runs must render identically, here and on another machine. A
        disambiguator read off insertion order or a list index would satisfy "distinct" and
        break that -- which is a worse defect than the one it fixed, because a tool that
        disagrees with itself cannot be diffed against its own output."""
        first = specialization_document(
            record(TEMPLATE_USR, line=1056), record(PARTIAL_USR, line=1062)
        )
        reversed_arrival = specialization_document(
            record(PARTIAL_USR, line=1062), record(TEMPLATE_USR, line=1056)
        )

        for usr in (TEMPLATE_USR, PARTIAL_USR):
            assert first.entity_name(NodeId(usr)) == reversed_arrival.entity_name(NodeId(usr))

    def test_a_name_nothing_shares_is_printed_as_itself(self) -> None:
        """Disambiguation is a response to an ambiguity, not a house style. Annotating a
        name no other entity has would push the thing the reader came for off the line."""
        f = node("c:@F@f#", name="f", qualified_name="f")
        doc = pipeline(model("a", f), model("b"))
        assert doc.entity_name(NodeId(f.id)) == "f"

    def test_entities_that_share_everything_visible_are_still_separated(self) -> None:
        """ARCH-001 §5's internal-linkage hazard, seen from the view: two file-scoped
        entities can share a qualified name *and* a signature, so a rule that gave up when
        nothing semantic distinguished them would reintroduce the defect one case over.
        It falls back instead."""
        one = node("c:src/a.cpp@F@helper#", file="src/a.cpp", name="helper", signature="void ()")
        two = node("c:src/b.cpp@F@helper#", file="src/b.cpp", name="helper", signature="void ()")
        doc = pipeline(model("a", one, two), model("b"))

        assert doc.entity_name(NodeId(one.id)) != doc.entity_name(NodeId(two.id))

    def test_nothing_that_varies_by_host_reaches_a_label(self) -> None:
        """The extractor once leaked an absolute worktree path into identity, and worktrees are
        named by SHA, so nodes differed on every comparison of any two revisions. A label
        derived from a path must be derived from the repository-relative one, or that shape
        comes back through the view."""
        doc = specialization_document(
            record(TEMPLATE_USR, line=1056), record(PARTIAL_USR, line=1062)
        )
        for usr in (TEMPLATE_USR, PARTIAL_USR):
            assert "/home/" not in doc.entity_name(NodeId(usr))
            assert not doc.entity_name(NodeId(usr)).startswith("/")

    def test_the_disambiguated_name_survives_the_trip_to_the_page(self) -> None:
        """Disambiguating in the document is only half of it: the view has to print what it
        was given. It did not, and had not since overloads were first bracketed -- the
        annotation is bracketed and a bracket is rich's markup delimiter, so the markup
        parser ate `[void (int)]` and `[partial specialization]` alike and the terminal
        showed the ambiguous name the document had taken care to disambiguate. A defect
        with no visible symptom except the one being fixed, so it is held here rather than
        left to the next person to rediscover.
        """
        records = specialization_document(
            record(TEMPLATE_USR, line=1056), record(PARTIAL_USR, line=1062)
        )
        one = node("c:@F@f#i#", name="f", signature="void (int)")
        two = node("c:@F@f#d#", name="f", signature="void (double)")
        overloads = pipeline(model("a", one, two), model("b"))

        for doc, ids in ((records, (TEMPLATE_USR, PARTIAL_USR)), (overloads, (one.id, two.id))):
            out = draw(doc, limit=0)
            for usr in ids:
                assert doc.entity_name(NodeId(usr)) in out


class TestTheViewIsHonestAboutItself:
    def test_truncation_announces_itself(self) -> None:
        """A page a reviewer will not read is the problem ranking exists to solve, so
        truncating is right -- but truncation nobody is told about is §11's failure in
        miniature."""
        nodes = [node(f"c:@F@f{i}#", name=f"f{i}") for i in range(12)]
        doc = pipeline(model("a", *nodes), model("b"))
        out = draw(doc, limit=3)
        assert "more top-level events" in out

    def test_no_truncation_shows_everything(self) -> None:
        nodes = [node(f"c:@F@f{i}#", name=f"f{i}") for i in range(6)]
        doc = pipeline(model("a", *nodes), model("b"))
        out = draw(doc, limit=0)
        for n in nodes:
            assert n.qualified_name in out

    def test_an_empty_delta_renders_without_pretending_otherwise(self) -> None:
        f = node("c:@F@f#", name="f")
        out = draw(pipeline(model("a", f), model("b", f), lines_changed=40))
        assert "no semantic change" in out

    @pytest.mark.parametrize("collapse_cascades", [True, False])
    def test_every_event_reaches_the_page_in_both_modes(self, collapse_cascades: bool) -> None:
        cls = node("c:@S@C", kind=NodeKind.CLASS, name="Widget")
        member = node("c:@S@C@F@m#", name="m", qualified_name="Widget::m")
        base = model("a", cls, member, edges=[edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS)])
        doc = pipeline(base, model("b"))
        out = draw(doc, collapse_cascades=collapse_cascades, limit=0)
        assert "Widget::m" in out and "Widget" in out
