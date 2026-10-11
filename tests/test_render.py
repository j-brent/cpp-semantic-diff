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

from cpp_semantic_diff.acquire import (
    RenameLimitHit,
    RevPair,
    ThirdPartyEntry,
    ThirdPartySet,
    ThirdPartySource,
)
from cpp_semantic_diff.acquire.tokens import ExcludedFile
from cpp_semantic_diff.classify import classify_events
from cpp_semantic_diff.diff import diff_models
from cpp_semantic_diff.diff.coverage import Coverage, UncoveredFile
from cpp_semantic_diff.match import Correspondence, match_models
from cpp_semantic_diff.model import (
    AttrValue,
    ChangeEvent,
    ChangeEventId,
    ChangeEventKind,
    Completeness,
    Confidence,
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
from cpp_semantic_diff.rank import rank_events
from cpp_semantic_diff.render import (
    SCHEMA_VERSION,
    SYSTEMATIC_LIMITS,
    DiffDocument,
    RenameDetection,
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
    kind: ChangeEventKind, subject: str, confidence: Confidence = RESIDUE, **payload: AttrValue
) -> ChangeEvent:
    return ChangeEvent(kind=kind, subject=NodeId(subject), payload=payload, confidence=confidence)


REVS = RevPair(base_ref="main", head_ref="HEAD", base_sha="a" * 40, head_sha="b" * 40)


def document(
    base: Model,
    head: Model,
    events: list[ChangeEvent],
    *,
    correspondence: Correspondence | None = None,
    lines_changed: int = 100,
    files_renamed: int = 0,
    rename_limit: RenameLimitHit | None = None,
    third_party: ThirdPartySet | None = None,
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
        rename_limit=rename_limit,
        third_party=third_party,
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

    def test_hidden_count_is_the_whole_subtree_not_just_direct_children(self) -> None:
        """`CollapseNode.hidden`'s own claim: "the whole subtree below it, at every depth."

        A one-level cascade cannot tell a correct recursive sum from a wrong direct-child
        count -- both equal 1. A removed namespace containing a removed class containing a
        removed method (plus the two contains edges the endpoint rule folds in beside them)
        can: the namespace's two *direct* children are not its whole subtree, and `hidden`
        must count the subtree, not the children, or `_why`'s "N consequence(s) nested
        below" undercounts what the view actually hid.
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

        by_subject = {
            doc.by_id[n.event_id].subject: n for root in tree.roots for n, _ in root.walk()
        }
        ns_node = by_subject[NodeId("c:@N@n")]

        # Independent of `hidden`'s own arithmetic: every node `walk()` reaches below the
        # namespace, counted the same way `test_nesting_follows_the_chain_one_level_at_a_time`
        # trusts `walk()` to recurse correctly.
        descendants = sum(1 for _, depth in ns_node.walk() if depth > 0)
        assert ns_node.hidden == descendants
        assert ns_node.hidden > len(ns_node.children), (
            "the fixture must nest more than one level deep, or this cannot tell a "
            "recursive sum from a direct-child count"
        )

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

    def _doc(self, events: list[ChangeEvent]) -> DiffDocument:
        f = node("c:@F@f#", name="f")
        g = node("c:@F@g#", name="g")
        return document(model("a", f, g), model("b", f, g), events)

    def test_an_event_with_no_cause_is_top_level(self) -> None:
        doc = self._doc(
            [
                event(ChangeEventKind.NODE_BODY_CHANGED, "c:@F@f#", SURE),
                event(ChangeEventKind.NODE_BODY_CHANGED, "c:@F@g#", SURE),
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
        # `model_copy`, because `ChangeEvent` is frozen (ARCH-001 §3): the way to get a modified
        # event is to make one, which is also what classify does to set this same field.
        orphan = event(ChangeEventKind.NODE_BODY_CHANGED, "c:@F@f#", SURE).model_copy(
            update={"caused_by": ChangeEventId("NodeRemoved:deadbeef")}
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
        first = event(ChangeEventKind.NODE_BODY_CHANGED, "c:@F@f#", SURE)
        second = event(ChangeEventKind.NODE_BODY_CHANGED, "c:@F@g#", SURE)
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

    def test_a_cycle_reached_through_an_acyclic_lead_in_promotes_only_the_cycle(self) -> None:
        """`_on_a_cycle`'s own claim: a node reached while still on the current path closes
        a cycle, and *the tail of the path from that node* is the cycle -- not the whole
        path. A chain that merely walks into a cycle without being part of it has an
        answer to "which of these is the cause" (its own link, unbroken); only the cycle's
        members do not. The two-node fixture above cannot tell "promote the cycle" apart
        from "promote everything reachable from it", because there is nothing upstream to
        wrongly sweep in -- this fixture adds that upstream link.

        **Why `doc.ranking` is forced rather than left as `rank_events` produced it.**
        `_on_a_cycle` walks `parent` in the order `collapse()` built it from, which is
        ranking order. `rank/ranker.py` caps a consequence's score at its cause's
        (`_bound_by_cause`) and breaks ties on cascade depth, so a lead-in whose
        `caused_by` points *into* a cycle always ranks after both cycle members -- its
        score is capped by theirs and its depth is strictly greater. That means the walk
        that first discovers the cycle always starts *on* the cycle itself in practice,
        where the path and its cycle-tail are the same slice and the trim this test is
        about never has anything to cut. A mutant that swept the whole walked path instead
        of just the tail (`on_cycle.update(path)` for
        `on_cycle.update(path[path.index(current):])`) passed this fixture unmodified,
        which is exactly that: a real document built through ranking cannot exercise the
        line ARCH-001 §6.3 and this module's docstring both rest on. `collapse.py`'s own
        docstring says a document "can arrive from a file, from a test, or from a future
        stage" -- not only from today's ranker -- so the walk order has to be forced to
        the one shape ranking will not produce, and the claim has to hold there too.
        `DiffDocument` is not frozen (`by_id`'s own comment), so reassigning `ranking` is
        the same kind of by-hand construction the rest of this module already uses.
        """
        f = node("c:@F@f#", name="f")
        g = node("c:@F@g#", name="g")
        h = node("c:@F@h#", name="h")

        lead_in = event(ChangeEventKind.NODE_BODY_CHANGED, "c:@F@f#", SURE)
        a = event(ChangeEventKind.NODE_BODY_CHANGED, "c:@F@g#", SURE)
        b = event(ChangeEventKind.NODE_BODY_CHANGED, "c:@F@h#", SURE)
        # lead_in -> a <-> b: a cycle between a and b, fed by a lead-in that is not on it.
        lead_in = lead_in.model_copy(update={"caused_by": a.id})
        a = a.model_copy(update={"caused_by": b.id})
        b = b.model_copy(update={"caused_by": a.id})
        doc = document(model("x", f, g, h), model("y", f, g, h), [lead_in, a, b])
        # Force the walk that discovers the cycle to start from the lead-in, not from a
        # cycle member -- see the docstring above for why ranking alone never does this.
        doc.ranking = [lead_in.id, a.id, b.id]

        tree = collapse(doc)
        assert set(tree.promoted) == {a.id, b.id}, "only the cycle has no usable cause"
        assert lead_in.id not in tree.promoted, "the lead-in's own link is still usable"
        assert lead_in.id in hidden_ids(tree), "and it must still be nested under the cycle"
        assert sorted(tree.flatten()) == sorted([lead_in.id, a.id, b.id])

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
            if doc.by_id[n.event_id].kind is ChangeEventKind.NODE_REMOVED
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
            == "2,431 lines changed -> 9 change events, 2 high-risk"
        )

    def test_the_sentence_carries_the_changes_no_event_explains(self) -> None:
        """Alone, the ratio improves when the model sees less; beside the uncovered count it
        cannot (ADR-042)."""
        assert compression_sentence(580, 181, 2, uncovered=(37, 412)) == (
            "580 lines changed -> 181 change events, 2 high-risk · 37 of 412 changed C++ lines "
            "explained by no change event"
        )
        assert compression_sentence(10, 0, 0, uncovered=(4, 4)).endswith(
            "(no semantic change the model can see) · 4 of 4 changed C++ lines explained by no "
            "change event"
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

    def test_a_truncated_rename_map_is_warned_about(self) -> None:
        """Both models can parse clean while git cut the rename map short at
        `diff.renameLimit`; the removals and additions that follow look ordinary unless the
        page says why. Carried in the JSON, so every consumer can state it."""
        f = node("c:@F@f#", name="f")
        base = model("a", f, completeness=self._parsed())
        head = model("b", completeness=self._parsed())
        corr = match_models(base, head)
        events = classify_events(diff_models(base, head, corr), base, head, corr)
        doc = document(
            base, head, events, correspondence=corr, rename_limit=RenameLimitHit(needed=1234)
        )
        assert doc.completeness.renames == RenameDetection(complete=False, limit_needed=1234)
        assert doc.completeness.warnings == [], "both sides parsed clean"
        assert doc.completeness.degraded
        out = draw(doc)
        assert "diff.renameLimit" in out and "1,234" in out
        assert out.index("diff.renameLimit") < out.index("NodeRemoved")
        again = DiffDocument.from_json(doc.to_json())
        assert again.completeness.renames == doc.completeness.renames

    def test_a_truncated_rename_map_with_no_known_limit_still_warns_and_does_not_say_none(
        self,
    ) -> None:
        """`RenameLimitHit.needed` is `None` whenever git's stderr hit the limit without
        naming the number that would have sufficed (`acquire/git.py`'s own regex can miss) --
        a real, reachable shape this stage must not mishandle. `RenameDetection.warning`
        branches on it to drop the advice clause entirely; the untested path is exactly
        where a `None` formatted with `:,` would raise, or where an unconditional sentence
        would print the word "None" where a number was supposed to be.
        """
        f = node("c:@F@f#", name="f")
        base = model("a", f, completeness=self._parsed())
        head = model("b", completeness=self._parsed())
        corr = match_models(base, head)
        events = classify_events(diff_models(base, head, corr), base, head, corr)
        doc = document(
            base, head, events, correspondence=corr, rename_limit=RenameLimitHit(needed=None)
        )
        assert doc.completeness.renames == RenameDetection(complete=False, limit_needed=None)
        assert doc.completeness.degraded
        out = draw(doc)
        assert "diff.renameLimit" in out
        assert "None" not in out
        assert "would find them" not in out, "no number to advise setting the limit to"

    @staticmethod
    def _parsed() -> Completeness:
        return Completeness(
            tus_total=1,
            tus=[TuResult(path="src/a.cpp", status=TuStatus.PARSED)],
            producer_version=PRODUCER,
        )

    def test_a_complete_rename_map_says_nothing(self) -> None:
        doc = self._doc(self._parsed(), self._parsed())
        assert doc.completeness.renames.complete
        assert not doc.completeness.degraded
        assert "renameLimit" not in draw(doc)


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

    def test_what_the_producer_cannot_see_is_stated_once_beside_the_limits(self) -> None:
        """ADR-029: a limit of the producer is stated on every run, the way §11's two are --
        once, though both sides state it."""
        unseen = ["Implicit special members are not visible.", "Macros are not modelled."]
        doc = self._doc(Completeness(not_visible=unseen), Completeness(not_visible=unseen))
        assert doc.completeness.limits == [*SYSTEMATIC_LIMITS, *unseen]
        out = draw(doc)
        assert out.count("Implicit special members are not visible") == 1

    def test_uncovered_changes_are_stated_and_survive_the_json_round_trip(self) -> None:
        f = node("c:@F@f#", name="f")
        parsed = Completeness(tus_total=1, tus=[TuResult(path="a", status=TuStatus.PARSED)])
        base, head = model("a", f, completeness=parsed), model("b", f, completeness=parsed)
        coverage = Coverage(
            changed_lines=6,
            uncovered_lines=2,
            uncovered_tokens=9,
            moved_tokens=16,
            files=(UncoveredFile(Side.HEAD, "orphan.h", (3, 4), 9),),
            excluded_files=(ExcludedFile("blob.h", "binary"),),
            renames_complete=False,
        )
        doc = build_document(
            repo="fixture",
            revs=REVS,
            base=base,
            head=head,
            correspondence=match_models(base, head),
            events=[],
            ranking=rank_events([], base, head),
            lines_changed=10,
            files_changed=2,
            cpp_files_changed=2,
            coverage=coverage,
        )
        again = DiffDocument.from_json(doc.to_json())
        assert (again.summary.uncovered_lines, again.summary.changed_cpp_lines) == (2, 6)
        assert again.completeness.uncovered is not None
        assert again.completeness.uncovered.files[0].lines == [3, 4]
        assert not again.completeness.degraded, "a standing measure, not a failure"
        out = draw(again)
        assert "2 of 6 changed C++ lines explained by no change event" in out
        assert "orphan.h: 9 token(s) on 2 line(s)" in out
        assert "16 token(s) moved unchanged" in out
        assert "not compared: blob.h (binary)" in out
        assert "skipped inexact rename detection for this count" in out
        assert not again.completeness.uncovered.renames_complete

    def test_unmeasured_coverage_is_none_not_zero(self) -> None:
        """Zero would claim every change was explained."""
        doc = self._doc(Completeness(), Completeness())
        assert doc.summary.uncovered_lines is None
        assert doc.completeness.uncovered is None
        assert "explained by no change event" not in draw(doc)

    def test_a_clean_model_does_not_claim_to_be_incomplete(self) -> None:
        doc = self._doc(
            Completeness(tus_total=1, tus=[TuResult(path="a", status=TuStatus.PARSED)]),
            Completeness(tus_total=1, tus=[TuResult(path="a", status=TuStatus.PARSED)]),
        )
        assert not doc.completeness.degraded


class TestTheThirdPartySetIsReported:
    """ADR-025: the set and the folder-name hints are data on the document, and the view
    states both. Not yet applied to extraction."""

    TP = ThirdPartySet(
        entries=(
            ThirdPartyEntry(
                path="third_party/lib",
                sources=frozenset({ThirdPartySource.SUBMODULE, ThirdPartySource.DECLARED}),
            ),
            ThirdPartyEntry(path="vendor/zlib", sources=frozenset({ThirdPartySource.DECLARED})),
        ),
        hints=("include/x/fmt/bundled",),
    )

    def _doc(self, third_party: ThirdPartySet | None) -> DiffDocument:
        parsed = Completeness(tus_total=1, tus=[TuResult(path="a", status=TuStatus.PARSED)])
        f = node("c:@F@f#", name="f")
        base = model("a", f, completeness=parsed)
        head = model("b", f, completeness=parsed)
        return document(base, head, [], third_party=third_party)

    def test_the_set_and_hints_are_in_the_json(self) -> None:
        doc = self._doc(self.TP)
        data = doc.model_dump(mode="json")
        assert data["third_party"] == [
            {"path": "third_party/lib", "sources": ["declared", "submodule"]},
            {"path": "vendor/zlib", "sources": ["declared"]},
        ]
        assert data["third_party_hints"] == [
            {
                "path": "include/x/fmt/bundled",
                "message": "`include/x/fmt/bundled` looks like third-party code; pass "
                "`--third-party include/x/fmt/bundled` if it is.",
            }
        ]
        assert DiffDocument.from_json(doc.to_json()) == doc

    def test_the_view_states_the_set_and_the_hints(self) -> None:
        out = draw(self._doc(self.TP))
        assert "third-party third_party/lib (declared, submodule) · vendor/zlib (declared)" in out
        assert "note `include/x/fmt/bundled` looks like third-party code" in out

    def test_nothing_is_said_when_there_is_nothing_to_say(self) -> None:
        doc = self._doc(None)
        assert doc.third_party == [] and doc.third_party_hints == []
        out = draw(doc)
        assert "third-party" not in out

    def test_a_hint_does_not_degrade_the_run(self) -> None:
        assert not self._doc(self.TP).completeness.degraded


class TestTheResidueIsReported:
    """ARCH-001 §13: v0 ships tier-1 identity only, and *measuring the damage* is the
    deliverable. A measurement that appears only as an inflated event count is not one."""

    def test_a_rename_reads_as_a_removal_plus_an_addition_and_says_so(self) -> None:
        before = node("c:@F@old#", name="old")
        after = node("c:@F@new#", name="new")
        doc = pipeline(model("a", before), model("b", after))

        kinds = sorted(e.kind for e in doc.events)
        assert kinds == [ChangeEventKind.NODE_ADDED, ChangeEventKind.NODE_REMOVED]

        r = doc.summary.residue
        assert (r.unmatched_base, r.unmatched_head) == (1, 1)
        assert r.tiers_run == [MatchTier.DECLARED, MatchTier.INLINE_NAMESPACE, MatchTier.NAME]
        assert r.pairable_residue == 1, "the upper bound on what tiers 2-4 could remove"

        out = draw(doc)
        assert "tiers declared, inline-namespace, name only" in out
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

    def test_a_moved_nodes_old_and_new_parent_are_both_nameable(self) -> None:
        """`build.py`'s own claim about `NodeMoved`: "a parent may have no event of its
        own. A view that cannot name the new container is reporting a move to nowhere."

        `NodeMoved` is unreachable through tier-1 identity today (`diff/nodes.py`: a node
        whose parent differs from its match's would not have matched at all), but it is a
        declared member of §6.1's closed event set, and render has to handle every member
        of that set correctly regardless of how often diff currently produces one -- the
        same reasoning `EdgeAttrChanged`'s tests apply to a kind that is merely *rare*.
        Built by hand, in the idiom this module's docstring names for exactly this reason.
        """
        old_parent = node("c:@N@a", kind=NodeKind.NAMESPACE, name="a", qualified_name="a")
        new_parent = node("c:@N@b", kind=NodeKind.NAMESPACE, name="b", qualified_name="b")
        moved_node = node("c:@F@m#", name="m", qualified_name="m")
        moved = event(
            ChangeEventKind.NODE_MOVED,
            "c:@F@m#",
            SURE,
            old_parent="c:@N@a",
            new_parent="c:@N@b",
        )
        base = model("a", old_parent, new_parent, moved_node)
        head = model("b", old_parent, new_parent, moved_node)
        doc = document(base, head, [moved])

        assert NodeId("c:@N@a") in doc.entities, "the old container has no event of its own"
        assert NodeId("c:@N@b") in doc.entities, "neither does the new one"
        # Not just present in `entities`: nameable through the same path the view uses,
        # and rendering must not raise reaching for a parent with no event of its own.
        draw(doc)
        assert doc.entity_name(NodeId("c:@N@a")) == "a"
        assert doc.entity_name(NodeId("c:@N@b")) == "b"

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


class TestASignatureChangeShowsItsCallers:
    """ADR-035: a paired function's calls map through the correspondence and produce no
    change events, so the document files its head-side callers under the signature change,
    where a reviewer asks whether every caller followed."""

    def doc(self) -> DiffDocument:
        def side(rev: str, signature: str, *callers: str) -> Model:
            f = node("c:@F@f#", name="f", signature=signature)
            nodes = [node(c, name=c.split("@")[-1].rstrip("#")) for c in callers]
            return model(rev, f, *nodes, edges=[edge(c, "c:@F@f#") for c in callers])

        return pipeline(
            side("a", "int ()", "c:@F@g#", "c:@F@h#"),
            side("b", "long ()", "c:@F@g#", "c:@F@h#", "c:@F@k#"),
        )

    def test_the_document_lists_every_head_caller(self) -> None:
        doc = self.doc()
        (changed,) = (e for e in doc.events if e.kind is ChangeEventKind.NODE_SIGNATURE_CHANGED)
        assert doc.callers == {changed.id: ["c:@F@g#", "c:@F@h#", "c:@F@k#"]}
        assert {"c:@F@g#", "c:@F@h#", "c:@F@k#"} <= doc.entities.keys()

    def test_the_view_names_them_under_the_event(self) -> None:
        out = draw(self.doc(), collapse_cascades=False)
        assert "called by 3: g, h, k" in out

    def test_it_survives_the_json_round_trip(self) -> None:
        doc = self.doc()
        assert DiffDocument.from_json(doc.to_json()).callers == doc.callers


class TestANamePairedFunctionIsOnBothSides:
    """ADR-036: a function the name tier paired has one id at base and another at head, and
    each id is in one model only. Both are one entity both sides hold, so neither may read as
    added or removed -- `sides` goes through the correspondence, not the ids."""

    OLD = "c:@F@f#I#"
    NEW = "c:@F@f#I#I#"

    def doc(self) -> DiffDocument:
        g = node("c:@F@g#", name="g")
        base = model(
            "a",
            node(self.OLD, name="f", signature="void (int)"),
            g,
            edges=[edge("c:@F@g#", self.OLD)],
        )
        head = model("b", node(self.NEW, name="f", signature="void (int, int)"), g)
        return pipeline(base, head)

    def test_the_signature_change_does_not_mark_its_subject_added(self) -> None:
        doc = self.doc()
        assert doc.entities[NodeId(self.NEW)].sides == ["base", "head"]
        out = draw(doc, collapse_cascades=False)
        line = next(x for x in out.splitlines() if "NodeSignatureChanged" in x)
        assert "+ " not in line and " f" in line

    def test_the_callee_of_a_removed_call_does_not_read_as_removed(self) -> None:
        doc = self.doc()
        (removed,) = (e for e in doc.events if e.kind is ChangeEventKind.EDGE_REMOVED)
        assert removed.subject == f"c:@F@g#|calls|{self.OLD}"
        assert doc.entities[NodeId(self.OLD)].sides == ["base", "head"]
        assert "- " not in draw(doc, collapse_cascades=False)


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
        edges = [e for e in doc.events if e.kind is ChangeEventKind.EDGE_ADDED]
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
        assert "more top-level change events" in out

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
