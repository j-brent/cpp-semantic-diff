"""Ranking and attention -- ARCH-001 §7, resting on RESEARCH-002 §4.

Every class below names an **ordering claim the design makes** and defends that, not the
arithmetic that currently satisfies it. No test pins a float. The weights in
`rank/weights.py` exist to be calibrated against real repositories (STATUS §5 says so about
the tier-4 threshold and the same reasoning applies here), and a suite that asserts
`total == 0.377` fossilizes them: the next person to retune would have to choose between
the numbers and the tests, and would pick the tests.

The claims are stated as inequalities between events built to differ in exactly one factor,
which is what makes a failure legible -- it says which of §7's four inputs stopped working.

Models and events are built by hand. Nothing here needs libclang, a compile database or a
fixture clone, because the stage is a pure function of an event list and two models (§3).
"""

from __future__ import annotations

import importlib
import inspect
import re

import pytest

from cpp_semantic_diff.model import (
    Confidence,
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
)
from cpp_semantic_diff.rank import (
    Exposure,
    FactorName,
    Ranking,
    classify_path,
    exposure_map,
    rank_events,
)
from cpp_semantic_diff.rank import weights as w

PUBLIC = "include/fmt/format.h"
VENDORED = "test/gtest/gtest.h"
LIB_SOURCE = "src/format.cc"

SURE = Confidence(tier=MatchTier.DECLARED, models_complete=True)
UNSURE = Confidence(tier=MatchTier.SIMILARITY, models_complete=True)
RESIDUE = Confidence(tier=None, models_complete=True)


# -- builders ---------------------------------------------------------------------------


def node(usr: str, *, file: str = PUBLIC, kind: NodeKind = NodeKind.FUNCTION, **kw) -> Node:
    name = kw.pop("name", usr.rsplit("@", 1)[-1] or usr)
    return Node(
        id=NodeId(usr),
        kind=kind,
        name=name,
        qualified_name=kw.pop("qualified_name", name),
        location=Location(file=file, line=kw.pop("line", 1)),
        **kw,
    )


def model(rev: str, *nodes: Node, edges: list[Edge] | None = None) -> Model:
    m = Model(rev=rev, edges=list(edges or []))
    for n in nodes:
        m.add_node(n)
    return m


def edge(src: str, dst: str, kind: EdgeKind = EdgeKind.CALLS) -> Edge:
    return Edge(src=NodeId(src), dst=NodeId(dst), kind=kind)


def event(kind: EventKind, subject: str, confidence: Confidence = SURE, **payload) -> Event:
    return Event(kind=kind, subject=NodeId(subject), payload=payload, confidence=confidence)


def body(subject: str, confidence: Confidence = SURE) -> Event:
    return event(EventKind.NODE_BODY_CHANGED, subject, confidence)


def rank(events: list[Event], base: Model | None = None, head: Model | None = None) -> Ranking:
    return rank_events(events, base or model("a"), head or model("b"))


def position(ranking: Ranking, subject_event: Event) -> int:
    return ranking.order.index(subject_event.id)


def outranks(ranking: Ranking, first: Event, second: Event) -> bool:
    return position(ranking, first) < position(ranking, second)


# -- the four factors -------------------------------------------------------------------


class TestUncertaintyRanksUpNotDown:
    """ARCH-001 §7: "a low-confidence event ranks *up*, not down. Uncertainty is a reason
    for a human to look, not a reason to hide."

    This is the single most important test in the file, because the failure it guards
    against has no symptom. Invert this factor's sign and the tool still emits a plausible
    ordering, still explains itself, still looks correct -- and systematically buries the
    events it is least sure about, which are exactly the ones only a human can settle. It
    would be a silent conversion of "we could not tell" into "nothing to see", the §11
    failure mode this project exists to argue against.

    `Confidence` keeps `tier` and `models_complete` as raw inputs rather than a scalar
    (`events.py`) specifically so this stage applies the inversion once, here.
    """

    def side(self) -> Model:
        # Two entities identical in every respect the other three factors read: same file,
        # same exposure, no dependents, no container.
        return model("m", node("c:@F@f#"), node("c:@F@g#"))

    def test_a_weaker_match_tier_outranks_a_stronger_one_all_else_equal(self) -> None:
        certain = body("c:@F@f#", SURE)
        doubtful = body("c:@F@g#", UNSURE)
        ranking = rank([certain, doubtful], self.side(), self.side())
        assert outranks(ranking, doubtful, certain)

    def test_unmatched_residue_outranks_every_tier(self) -> None:
        """None is the maximum, not a missing value. In v0 (tier 1 only, §12) it is also
        every add, remove and rename -- the population STATUS §6 wants measured."""
        events = [
            body("c:@F@f#", SURE),
            body("c:@F@g#", UNSURE),
            body("c:@F@h#", RESIDUE),
        ]
        head = model("m", node("c:@F@f#"), node("c:@F@g#"), node("c:@F@h#"))
        ranking = rank(events, head, head)
        assert ranking.order == [events[2].id, events[1].id, events[0].id]

    def test_the_tier_ladder_runs_the_same_direction_as_arch_5(self) -> None:
        """Each successive tier is a weaker claim that two entities are the same entity,
        so each carries more uncertainty than the one above it (§5's table, read down)."""
        ladder = [
            w.UNCERTAINTY_BY_TIER[MatchTier.DECLARED],
            w.UNCERTAINTY_BY_TIER[MatchTier.STRUCTURAL],
            w.UNCERTAINTY_BY_TIER[MatchTier.POSITIONAL],
            w.UNCERTAINTY_BY_TIER[MatchTier.SIMILARITY],
            w.UNCERTAINTY_BY_TIER[None],
        ]
        assert ladder == sorted(ladder), "uncertainty must increase as the tier weakens"

    def test_an_incomplete_model_adds_uncertainty_rather_than_removing_it(self) -> None:
        """§11: output built on an incomplete model must say so. Saying so by ranking the
        event *down* would be the same inversion in a second place."""
        complete = body("c:@F@f#", Confidence(tier=MatchTier.DECLARED, models_complete=True))
        degraded = body("c:@F@g#", Confidence(tier=MatchTier.DECLARED, models_complete=False))
        ranking = rank([complete, degraded], self.side(), self.side())
        assert outranks(ranking, degraded, complete)
        detail = ranking.score_for(degraded.id).factor(FactorName.UNCERTAINTY).detail
        assert "incomplete" in detail


class TestBlastRadiusIsWhatMakesThisWorthBuilding:
    """RESEARCH-002 §3.6: nothing in the landscape says "this 3-line change has 400
    transitive dependents; that 300-line change has 2". ARCH-001 §7 makes it the first input.
    """

    def graph(self, callers: int) -> Model:
        nodes = [node("c:@F@hot#"), node("c:@F@leaf#")]
        edges = []
        for i in range(callers):
            nodes.append(node(f"c:@F@c{i}#", file=LIB_SOURCE))
            edges.append(edge(f"c:@F@c{i}#", "c:@F@hot#"))
        return model("m", *nodes, edges=edges)

    def test_a_change_to_a_widely_called_function_outranks_a_change_to_a_leaf(self) -> None:
        head = self.graph(callers=20)
        hot, leaf = body("c:@F@hot#"), body("c:@F@leaf#")
        assert outranks(rank([hot, leaf], head, head), hot, leaf)

    def test_the_count_is_reported_not_just_consumed(self) -> None:
        """§7: "the count matters more than the set". The count is what a reviewer is
        shown, so it has to survive into the score rather than only into the arithmetic."""
        head = self.graph(callers=20)
        score = rank([body("c:@F@hot#")], head, head).score_for(body("c:@F@hot#").id)
        assert "20 dependent(s)" in score.factor(FactorName.BLAST_RADIUS).detail

    def test_inheritance_propagates_a_change_the_way_calls_do(self) -> None:
        """ARCH-001 §7 names `calls` + `contains` + `uses`. The v0 producer emits no `uses`
        edges at all (§12) and does emit `inherits`, which propagates change with no
        interpretation required: touch a base class and every derived class is touched."""
        derived = [node(f"c:@S@D{i}", kind=NodeKind.CLASS) for i in range(6)]
        head = model(
            "m",
            node("c:@S@Base", kind=NodeKind.CLASS),
            node("c:@S@Lonely", kind=NodeKind.CLASS),
            *derived,
            edges=[edge(d.id, "c:@S@Base", EdgeKind.INHERITS) for d in derived],
        )
        base_evt, lonely = body("c:@S@Base"), body("c:@S@Lonely")
        assert outranks(rank([base_evt, lonely], head, head), base_evt, lonely)

    def test_overriding_propagates_a_change_the_way_calls_do(self) -> None:
        """The other edge kind the producer emits and the doc's word list omits. A virtual
        method's contract binds every override; changing it is a change to all of them."""
        overrides = [node(f"c:@S@D{i}@F@m#", kind=NodeKind.METHOD) for i in range(6)]
        head = model(
            "m",
            node("c:@S@B@F@m#", kind=NodeKind.METHOD),
            node("c:@S@B@F@other#", kind=NodeKind.METHOD),
            *overrides,
            edges=[edge(o.id, "c:@S@B@F@m#", EdgeKind.OVERRIDES) for o in overrides],
        )
        virtual, plain = body("c:@S@B@F@m#"), body("c:@S@B@F@other#")
        assert outranks(rank([virtual, plain], head, head), virtual, plain)

    def test_containment_does_not_make_every_member_of_a_namespace_catastrophic(self) -> None:
        """The hazard §7's word "contains" creates, and the reason the walk has a direction.

        Reverse reachability that followed `contains` *downward* would give every entity in
        a large namespace the blast radius of the whole namespace -- on fmt, 10,574 nodes
        under `namespace fmt`. Containment is registered child -> container only: a changed
        member reaches its class, and nothing ever reaches a sibling.
        """
        members = [node(f"c:@N@ns@F@f{i}#") for i in range(50)]
        head = model(
            "m",
            node("c:@N@ns", kind=NodeKind.NAMESPACE),
            *members,
            edges=[edge("c:@N@ns", m.id, EdgeKind.CONTAINS) for m in members],
        )
        one_member = body("c:@N@ns@F@f0#")
        score = rank([one_member], head, head).score_for(one_member.id)
        detail = score.factor(FactorName.BLAST_RADIUS).detail
        assert detail.startswith("1 dependent(s)"), detail

    def test_a_removed_entity_is_measured_against_the_side_that_still_has_it(self) -> None:
        """`NodeRemoved` is in base coordinates by construction (`diff/nodes.py`), so the
        head model has no such node. Scoring it against head alone would put every removal
        at zero blast radius -- burying the one event kind that can break code outside the
        diff -- so the fallback is measured on base and the side is named in the detail."""
        callers = [node(f"c:@F@c{i}#") for i in range(9)]
        base = model(
            "a",
            node("c:@F@gone#"),
            *callers,
            edges=[edge(c.id, "c:@F@gone#") for c in callers],
        )
        removed = event(EventKind.NODE_REMOVED, "c:@F@gone#", RESIDUE)
        score = rank([removed], base, model("b")).score_for(removed.id)
        detail = score.factor(FactorName.BLAST_RADIUS).detail
        assert "9 dependent(s)" in detail and "base model" in detail


class TestNoveltyPrefersStructureOverBodies:
    """ARCH-001 §7: "added and removed outrank modified; attribute changes to `virtual`,
    `const`, access, and signature outrank body changes"."""

    def side(self) -> Model:
        return model(
            "m", node("c:@F@f#"), node("c:@F@g#"), node("c:@S@C@F@m#", kind=NodeKind.METHOD)
        )

    def test_an_added_entity_outranks_a_modified_body(self) -> None:
        # Both given the same confidence so the comparison isolates novelty. In real output
        # an add also carries no tier, which pushes it further up -- the two factors agree.
        added = event(EventKind.NODE_ADDED, "c:@F@f#", SURE)
        modified = body("c:@F@g#", SURE)
        assert outranks(rank([added, modified], self.side(), self.side()), added, modified)

    def test_a_removed_entity_outranks_an_added_one(self) -> None:
        """An added entity is a new promise; a removed one is a broken promise, and only the
        second can break code that is not in the diff."""
        assert w.NOVELTY_BY_KIND[EventKind.NODE_REMOVED] > w.NOVELTY_BY_KIND[EventKind.NODE_ADDED]

    def test_an_interface_attribute_change_outranks_a_body_change(self) -> None:
        virtual = event(
            EventKind.NODE_ATTR_CHANGED, "c:@S@C@F@m#", SURE, attr="virtual", old=False, new=True
        )
        modified = body("c:@F@g#", SURE)
        assert outranks(rank([virtual, modified], self.side(), self.side()), virtual, modified)

    def test_the_attribute_is_named_in_the_score_not_only_the_kind(self) -> None:
        """`virtual: false -> true` and `mutable: false -> true` are the same `EventKind`
        and different amounts of risk, so the score has to say which attribute it read."""
        changed = event(
            EventKind.NODE_ATTR_CHANGED, "c:@S@C@F@m#", SURE, attr="const", old=True, new=False
        )
        score = rank([changed], self.side(), self.side()).score_for(changed.id)
        assert score.factor(FactorName.NOVELTY).detail == "NodeAttrChanged(const)"

    def test_an_unknown_attribute_falls_back_above_a_body_change(self) -> None:
        """A producer that starts emitting a new attribute must not have it silently scored
        as nothing. An attribute with no rule is still a specific, named semantic change,
        and §7's rule that uncertainty is a reason to look applies to our own ignorance."""
        assert (
            w.NOVELTY_BY_KIND[EventKind.NODE_ATTR_CHANGED]
            > w.NOVELTY_BY_KIND[EventKind.NODE_BODY_CHANGED]
        )
        assert "surprise" not in w.NOVELTY_BY_ATTR
        unknown = event(
            EventKind.NODE_ATTR_CHANGED, "c:@S@C@F@m#", SURE, attr="surprise", old=1, new=2
        )
        modified = body("c:@F@g#", SURE)
        assert outranks(rank([unknown, modified], self.side(), self.side()), unknown, modified)

    def test_every_event_kind_has_a_weight(self) -> None:
        """`events.py` makes the event set closed and gives every kind an entry in
        `REQUIRED_PAYLOAD` so that adding one is a deliberate act. Same rule here: a kind
        with no weight would raise at scoring time, or worse, default to zero."""
        assert set(w.NOVELTY_BY_KIND) == set(EventKind)


class TestTheAttributeKeysAreAContractWithTheProducer:
    """ARCH-001 §7 names four attributes in prose. `produce/extract.py` writes actual keys.
    Where the two disagree, the producer is right and the doc is a paraphrase -- so the
    weight table is written against the producer and this test is what keeps it there.
    """

    def producer_keys(self) -> set[str]:
        extract = importlib.import_module("cpp_semantic_diff.produce.extract")
        source = inspect.getsource(extract._attributes)
        keys = set(re.findall(r'attrs\["(\w+)"\]', source))
        assert keys, "the producer's attribute keys are no longer readable from _attributes()"
        return keys

    def test_every_attribute_the_producer_writes_has_a_weight(self) -> None:
        missing = self.producer_keys() - set(w.NOVELTY_BY_ATTR)
        assert not missing, f"producer writes {sorted(missing)} with no novelty weight"

    def test_the_three_attributes_arch_names_and_can_exist_are_weighted_high(self) -> None:
        for attr in ("virtual", "const", "access"):
            assert w.NOVELTY_BY_ATTR[attr] > w.NOVELTY_BY_KIND[EventKind.NODE_BODY_CHANGED]

    def test_there_is_no_signature_attribute_and_return_type_stands_in_for_it(self) -> None:
        """§7's fourth named attribute does not exist and cannot.

        `signature` is a field on `Node` (§4.1), never an entry in `attrs`, so the
        mechanical attribute diff can never emit `NodeAttrChanged(signature)`. In v0 a
        changed signature also changes the USR, which is tier-1 identity (§5), so the pair
        is never matched and the change arrives as remove + add. What *does* survive as an
        attribute is `return_type` -- C++ cannot overload on it, so the USR does not encode
        it, so it is the one part of a signature that changes while identity holds.
        """
        assert "signature" not in self.producer_keys()
        assert "signature" not in w.NOVELTY_BY_ATTR
        assert w.NOVELTY_BY_ATTR["return_type"] >= max(w.NOVELTY_BY_ATTR.values())


# -- exposure: the factor that decides whether this works on a real repository ------------


class TestVendoredAndTestCodeDoNotDrownTheLibrary:
    """The measured problem, not a hypothetical one.

    The model built from `fmt` is 80.6 % test code -- 48.9 % `test/`, 31.7 % vendored
    `test/gtest` (3,347 nodes) -- and 19.2 % library. Extraction is right to include it.
    A ranking that puts a change to bundled googletest above a change to `fmt::format` is
    useless, and a reviewer shown that once will not read the second page.
    """

    def test_a_public_api_change_outranks_an_identical_change_to_vendored_test_code(
        self,
    ) -> None:
        head = model("m", node("c:@F@format#", file=PUBLIC), node("c:@F@helper#", file=VENDORED))
        public, vendored = body("c:@F@format#"), body("c:@F@helper#")
        assert outranks(rank([public, vendored], head, head), public, vendored)

    def test_one_library_change_beats_a_page_of_vendored_ones(self) -> None:
        """The realistic shape. It is not enough that public > vendored for one identical
        pair: on fmt the vendored events *outnumber* the library ones four to one, so the
        library change has to reach the top of a mixed list, not merely beat its twin."""
        helpers = [node(f"c:@F@h{i}#", file=VENDORED) for i in range(10)]
        callers = [node(f"c:@F@c{i}#", file=LIB_SOURCE) for i in range(12)]
        head = model(
            "m",
            node("c:@F@format#", file=PUBLIC),
            *helpers,
            *callers,
            edges=[edge(c.id, "c:@F@format#") for c in callers],
        )
        library = body("c:@F@format#")
        noise = [event(EventKind.NODE_REMOVED, h.id, RESIDUE) for h in helpers]
        ranking = rank([*noise, library], head, head)
        assert ranking.order[0] == library.id

    def test_test_code_ranks_above_vendored_code(self) -> None:
        """First-party tests are worth diffing -- a deleted assertion is RESEARCH-002 §3.9's
        most review-worthy event. Somebody else's test framework is not."""
        head = model(
            "m", node("c:@F@t#", file="test/format-test.cc"), node("c:@F@v#", file=VENDORED)
        )
        own_test, vendored = body("c:@F@t#"), body("c:@F@v#")
        assert outranks(rank([own_test, vendored], head, head), own_test, vendored)

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("include/fmt/format.h", Exposure.PUBLIC),
            ("fmt/core.h", Exposure.PUBLIC),
            ("src/format.cc", Exposure.INTERNAL),
            ("include/spdlog/details/os.h", Exposure.INTERNAL),
            ("test/format-test.cc", Exposure.TEST),
            ("test/gtest/gtest.h", Exposure.VENDORED),
            ("third_party/zlib/zlib.h", Exposure.VENDORED),
            ("examples/demo.cc", Exposure.TEST),
            ("support/tools_test.cc", Exposure.TEST),
        ],
    )
    def test_the_path_convention_reads_the_layouts_it_claims_to(
        self, path: str, expected: Exposure
    ) -> None:
        """The heuristic is directory convention and nothing else -- the model carries no
        install rules, no target interfaces and no `includes` edges (§4.2 declares them,
        §12 puts them out of v0). These are the layouts it is calibrated for; anything else
        is the documented failure mode in `exposure.py`, not a silent misread."""
        assert classify_path(path) is expected

    def test_a_member_is_as_public_as_the_class_that_declares_it(self) -> None:
        """§7 asks whether the subject is "reachable from a public header". An out-of-line
        method definition lives in a `.cpp`; what a client compiles against is the class."""
        head = model(
            "m",
            node("c:@S@C", file=PUBLIC, kind=NodeKind.CLASS),
            node("c:@S@C@F@m#", file=LIB_SOURCE, kind=NodeKind.METHOD),
            edges=[edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS)],
        )
        assert exposure_map(head)[NodeId("c:@S@C@F@m#")] is Exposure.PUBLIC

    def test_promotion_never_lifts_vendored_code_into_the_library(self) -> None:
        """The demotion has to win, or bundled googletest's own class hierarchy would
        promote its members straight back onto the first page."""
        head = model(
            "m",
            node("c:@S@Test", file=VENDORED, kind=NodeKind.CLASS),
            node("c:@S@Test@F@m#", file=VENDORED, kind=NodeKind.METHOD),
            edges=[edge("c:@S@Test", "c:@S@Test@F@m#", EdgeKind.CONTAINS)],
        )
        assert exposure_map(head)[NodeId("c:@S@Test@F@m#")] is Exposure.VENDORED


# -- decomposition ----------------------------------------------------------------------


class TestThreeDisconnectedChangesAreThreeChanges:
    """ARCH-001 §7 and RESEARCH-002 §3.11: "A PR whose delta has three disconnected
    components is three PRs." The intent-conformance signal at nearly zero cost.

    What connects two events is the whole design, and the failure is asymmetric: connect too
    much and the signal becomes a constant one-component answer that still *looks* like it
    works.
    """

    def three_islands(self) -> tuple[Model, list[Event]]:
        head = model(
            "m",
            node("c:@F@a1#"),
            node("c:@F@a2#"),
            node("c:@F@b1#"),
            node("c:@F@b2#"),
            node("c:@F@c1#"),
            edges=[edge("c:@F@a1#", "c:@F@a2#"), edge("c:@F@b1#", "c:@F@b2#")],
        )
        events = [body(n) for n in ("c:@F@a1#", "c:@F@a2#", "c:@F@b1#", "c:@F@b2#", "c:@F@c1#")]
        return head, events

    def test_a_delta_with_three_islands_decomposes_into_three_components(self) -> None:
        head, events = self.three_islands()
        ranking = rank(events, head, head)
        assert len(ranking.components) == 3
        assert sorted(c.size for c in ranking.components) == [1, 2, 2]

    def test_every_event_lands_in_exactly_one_component(self) -> None:
        head, events = self.three_islands()
        ranking = rank(events, head, head)
        placed = [e for c in ranking.components for e in c.events]
        assert sorted(placed) == sorted(e.id for e in events)

    def test_a_call_between_two_changed_entities_makes_them_one_change(self) -> None:
        head, events = self.three_islands()
        joined = model(
            "m",
            *head.nodes.values(),
            edges=[*head.edges, edge("c:@F@a2#", "c:@F@b1#")],
        )
        assert len(rank(events, joined, joined).components) == 2

    def test_two_methods_of_one_class_are_one_change(self) -> None:
        head = model(
            "m",
            node("c:@S@C", kind=NodeKind.CLASS),
            node("c:@S@C@F@m#", kind=NodeKind.METHOD),
            node("c:@S@C@F@n#", kind=NodeKind.METHOD),
            edges=[
                edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS),
                edge("c:@S@C", "c:@S@C@F@n#", EdgeKind.CONTAINS),
            ],
        )
        events = [body("c:@S@C@F@m#"), body("c:@S@C@F@n#")]
        assert len(rank(events, head, head).components) == 1

    def test_sharing_a_namespace_is_not_sharing_a_change(self) -> None:
        """The rule the whole partition depends on. Everything in fmt is inside
        `namespace fmt`; if containment through a namespace counted, every review would be
        exactly one component and the signal would be off while appearing to work."""
        head = model(
            "m",
            node("c:@N@ns", kind=NodeKind.NAMESPACE),
            node("c:@N@ns@F@f#"),
            node("c:@N@ns@F@g#"),
            edges=[
                edge("c:@N@ns", "c:@N@ns@F@f#", EdgeKind.CONTAINS),
                edge("c:@N@ns", "c:@N@ns@F@g#", EdgeKind.CONTAINS),
            ],
        )
        events = [body("c:@N@ns@F@f#"), body("c:@N@ns@F@g#")]
        assert len(rank(events, head, head).components) == 2

    def test_a_cause_link_joins_its_consequence(self) -> None:
        """`caused_by` is taken as data (§6.3). classify is a separate stage that may not
        have run, so this stage reads the link when it is there and never requires it."""
        head = model("m", node("c:@F@a1#"), node("c:@F@c1#"))
        cause = body("c:@F@a1#")
        effect = Event(
            kind=EventKind.NODE_BODY_CHANGED,
            subject=NodeId("c:@F@c1#"),
            caused_by=cause.id,
            confidence=SURE,
        )
        assert len(rank([cause, effect], head, head).components) == 1

    def test_a_cause_link_pointing_outside_the_list_is_ignored(self) -> None:
        """Rank must not acquire an opinion about how the event list was assembled."""
        head = model("m", node("c:@F@a1#"), node("c:@F@c1#"))
        dangling = Event(
            kind=EventKind.NODE_BODY_CHANGED,
            subject=NodeId("c:@F@c1#"),
            caused_by="NodeAdded:deadbeefdeadbeef",
            confidence=SURE,
        )
        assert len(rank([body("c:@F@a1#"), dangling], head, head).components) == 2

    def test_components_are_numbered_by_risk_so_the_first_one_is_read_first(self) -> None:
        head = model("m", node("c:@F@a1#", file=VENDORED), node("c:@F@c1#", file=PUBLIC))
        low, high = body("c:@F@a1#"), body("c:@F@c1#")
        ranking = rank([low, high], head, head)
        assert ranking.components[0].events == [high.id]
        assert ranking.score_for(high.id).component == 0


# -- the stage's own contract ------------------------------------------------------------


class TestTheScoreIsInspectableNotAMagicNumber:
    """A ranking nobody can interrogate will not be trusted and cannot be tuned. §10 makes
    JSON the product, so the reason has to be *in* the document -- not recomputable by a
    view that would then have to know how the score was built."""

    def test_every_score_carries_all_four_of_arch_7s_inputs(self) -> None:
        head = model("m", node("c:@F@f#"))
        score = rank([body("c:@F@f#")], head, head).score_for(body("c:@F@f#").id)
        assert [f.name for f in score.factors] == list(FactorName)

    def test_the_total_is_the_sum_of_the_contributions_and_nothing_else(self) -> None:
        head = model("m", node("c:@F@f#"), node("c:@F@g#"), edges=[edge("c:@F@g#", "c:@F@f#")])
        for score in rank([body("c:@F@f#")], head, head).scores:
            assert score.total == pytest.approx(sum(f.contribution for f in score.factors))
            assert 0.0 <= score.total <= 1.0

    def test_each_factor_separates_the_measurement_from_the_weight(self) -> None:
        """Value is what was measured, weight is what we chose. Keeping them apart is what
        lets a bad ranking be diagnosed down to the term responsible."""
        head = model("m", node("c:@F@f#"))
        score = rank([body("c:@F@f#")], head, head).score_for(body("c:@F@f#").id)
        for factor in score.factors:
            assert factor.contribution == pytest.approx(factor.value * factor.weight)
            assert factor.weight == w.FACTOR_WEIGHTS[factor.name.value]
            assert factor.detail

    def test_the_weights_are_a_partition_of_one_so_totals_compare_across_runs(self) -> None:
        """`summary.high_risk_count` (§10) prints on every run against a fixed threshold. A
        count against a scale that moves with the weights is not a measurement."""
        assert sum(w.FACTOR_WEIGHTS.values()) == pytest.approx(1.0)
        assert 0.0 < w.HIGH_RISK < 1.0


class TestTheStageIsPureAndDeterministic:
    """ARCH-001 §3 makes every stage a pure function of the previous stage's output, and §10
    makes the JSON the product. An order that depends on input order or on a dict traversal
    produces a different document for the same two commits, which makes every diff of the
    output meaningless and every regression test flaky."""

    def fixture(self) -> tuple[Model, list[Event]]:
        head = model(
            "m",
            node("c:@F@f#", file=PUBLIC),
            node("c:@F@g#", file=LIB_SOURCE),
            node("c:@F@h#", file=VENDORED),
            edges=[edge("c:@F@g#", "c:@F@f#"), edge("c:@F@h#", "c:@F@g#")],
        )
        events = [
            body("c:@F@f#"),
            event(EventKind.NODE_ADDED, "c:@F@g#", RESIDUE),
            event(EventKind.NODE_REMOVED, "c:@F@h#", RESIDUE),
        ]
        return head, events

    def test_the_same_inputs_produce_the_same_document(self) -> None:
        head, events = self.fixture()
        first = rank(events, head, head).model_dump_json()
        second = rank(events, head, head).model_dump_json()
        assert first == second

    def test_the_order_of_the_input_list_does_not_change_the_output(self) -> None:
        head, events = self.fixture()
        forward = rank(events, head, head)
        backward = rank(list(reversed(events)), head, head)
        assert forward.order == backward.order
        assert forward.model_dump_json() == backward.model_dump_json()

    def test_events_scoring_identically_still_have_one_fixed_order(self) -> None:
        """The common case, not a corner: novelty and exposure come from small tables, so
        ties are everywhere. Ties break on the content-derived event id (`events.py`)."""
        head = model("m", *(node(f"c:@F@f{i}#") for i in range(6)))
        events = [body(f"c:@F@f{i}#") for i in range(6)]
        ranking = rank(events, head, head)
        assert len({rank(events, head, head).order[0] for _ in range(5)}) == 1
        assert ranking.order == sorted(ranking.order)  # equal totals, so id decides

    def test_ranking_does_not_mutate_its_inputs(self) -> None:
        """Not tidiness. `Event` is mutable, `caused_by` is written by a different stage,
        and a rank that quietly stamped a score onto an event would make the pipeline order
        load-bearing and the stage untestable in isolation."""
        head, events = self.fixture()
        before_events = [e.model_dump_json() for e in events]
        before_model = head.model_dump_json()
        rank(events, head, head)
        assert [e.model_dump_json() for e in events] == before_events
        assert head.model_dump_json() == before_model

    def test_no_event_is_dropped(self) -> None:
        """Attention allocation is an ordering. An event this stage decided to omit is an
        event no view can restore, which is §6.3's rule about collapse in a second place."""
        head, events = self.fixture()
        ranking = rank(events, head, head)
        assert sorted(ranking.order) == sorted(e.id for e in events)

    def test_an_event_about_an_entity_in_neither_model_still_scores(self) -> None:
        """A subject the models do not carry is missing evidence, not evidence of a leaf
        (§11). It must not raise, and it must say so rather than reporting a measured zero."""
        stray = body("c:@F@nowhere#")
        score = rank([stray]).score_for(stray.id)
        assert "not in either model" in score.factor(FactorName.BLAST_RADIUS).detail
