"""Ranking and attention -- ARCH-001 §7, resting on RESEARCH-002 §4.

Every class below names an **ordering claim the design makes** and defends that, not the
arithmetic that currently satisfies it. No test pins a float. The weights in `rank/weights.py`
exist to be calibrated against real repositories (ARCH-001 §14 says so about the tier-4
threshold and the same reasoning applies here), and a suite that asserts `total == 0.377`
fossilizes them: the next person to retune would have to choose between the numbers and the
tests, and would pick the tests.

The claims are stated as inequalities between events built to differ in exactly one factor,
which is what makes a failure legible -- it says which of §7's four inputs stopped working.

Models and events are built by hand. Nothing here needs libclang, a compile database or a
fixture clone, because the stage is a pure function of an event list and two models (§3).
"""

from __future__ import annotations

import importlib
import inspect
import re
from typing import Any

import pytest

from cpp_semantic_diff.model import (
    AttrValue,
    ChangeEvent,
    ChangeEventId,
    ChangeEventKind,
    Confidence,
    Edge,
    EdgeId,
    EdgeKind,
    Location,
    MatchTier,
    Model,
    Node,
    NodeId,
    NodeKind,
    Unpaired,
)
from cpp_semantic_diff.rank import (
    Exposure,
    FactorName,
    Ranking,
    classify_path,
    exposure_map,
    rank_events,
    subject_home,
)
from cpp_semantic_diff.rank import weights as w

PUBLIC = "include/fmt/format.h"
OWN_TEST = "test/format-test.cc"
LIB_SOURCE = "src/format.cc"

SURE = Confidence(tier=MatchTier.DECLARED, models_complete=True)
UNSURE = Confidence(tier=MatchTier.SIMILARITY, models_complete=True)
RESIDUE = Confidence(tier=None, unpaired=Unpaired.NONE, models_complete=True)
CANDIDATE = Confidence(tier=None, unpaired=Unpaired.CANDIDATE, models_complete=True)
AMBIGUOUS = Confidence(tier=None, unpaired=Unpaired.AMBIGUOUS, models_complete=True)


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


def model(rev: str, *nodes: Node, edges: list[Edge] | None = None) -> Model:
    m = Model(rev=rev, edges=list(edges or []))
    for n in nodes:
        m.add_node(n)
    return m


def edge(src: str, dst: str, kind: EdgeKind = EdgeKind.CALLS) -> Edge:
    return Edge(src=NodeId(src), dst=NodeId(dst), kind=kind)


def event(
    kind: ChangeEventKind, subject: str, confidence: Confidence = SURE, **payload: AttrValue
) -> ChangeEvent:
    return ChangeEvent(kind=kind, subject=NodeId(subject), payload=payload, confidence=confidence)


def body(subject: str, confidence: Confidence = SURE) -> ChangeEvent:
    return event(ChangeEventKind.NODE_BODY_CHANGED, subject, confidence)


def edge_event(
    src: str,
    dst: str,
    kind: ChangeEventKind = ChangeEventKind.EDGE_ADDED,
    *,
    edge_kind: EdgeKind = EdgeKind.CALLS,
    confidence: Confidence = RESIDUE,
    caused_by: ChangeEvent | None = None,
) -> ChangeEvent:
    """An event whose subject is an edge id -- `src|kind|dst` (ARCH-001 §4.2)."""
    return ChangeEvent(
        kind=kind,
        subject=EdgeId(f"{src}|{edge_kind.value}|{dst}"),
        caused_by=caused_by.id if caused_by is not None else None,
        confidence=confidence,
    )


def rank(
    events: list[ChangeEvent], base: Model | None = None, head: Model | None = None
) -> Ranking:
    return rank_events(events, base or model("a"), head or model("b"))


def position(ranking: Ranking, subject_event: ChangeEvent) -> int:
    return ranking.order.index(subject_event.id)


def outranks(ranking: Ranking, first: ChangeEvent, second: ChangeEvent) -> bool:
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

    def test_an_ambiguous_pairing_outranks_every_tier(self) -> None:
        """A tier that found partners and refused to choose is the most doubt there is: the
        pairing itself is what is in question (ADR-034)."""
        events = [
            body("c:@F@f#", SURE),
            body("c:@F@g#", UNSURE),
            body("c:@F@h#", AMBIGUOUS),
        ]
        head = model("m", node("c:@F@f#"), node("c:@F@g#"), node("c:@F@h#"))
        ranking = rank(events, head, head)
        assert ranking.order == [events[2].id, events[1].id, events[0].id]

    def test_a_plain_addition_is_as_certain_as_a_declared_match(self) -> None:
        """Issue #17. Every genuine addition has no partner by definition, so having none is
        not doubt: with nothing on the other side that could be its partner, an addition
        carries the uncertainty of a tier-1 match, not the maximum."""
        added = event(ChangeEventKind.NODE_ADDED, "c:@F@f#", RESIDUE)
        score = rank([added], self.side(), self.side()).score_for(added.id)
        uncertainty = score.factor(FactorName.UNCERTAINTY)
        assert uncertainty.value == w.UNCERTAINTY_BY_TIER[MatchTier.DECLARED] == 0.0
        assert uncertainty.detail == "unmatched, no candidate"

    def test_a_candidate_on_the_other_side_is_doubt_and_ranks_up(self) -> None:
        """An addition with a same-kind sibling removed beside it may be a rename tier 1
        cannot see, and that is worth a human's eye."""
        plain = event(ChangeEventKind.NODE_ADDED, "c:@F@f#", RESIDUE)
        maybe_renamed = event(ChangeEventKind.NODE_ADDED, "c:@F@g#", CANDIDATE)
        ranking = rank([plain, maybe_renamed], self.side(), self.side())
        assert outranks(ranking, maybe_renamed, plain)
        detail = ranking.score_for(maybe_renamed.id).factor(FactorName.UNCERTAINTY).detail
        assert detail == "unmatched, candidate on the other side"

    def test_the_tier_ladder_runs_the_same_direction_as_arch_5(self) -> None:
        """Each successive tier is a weaker claim that two entities are the same entity,
        so each carries more uncertainty than the one above it (§5's table, read down)."""
        from cpp_semantic_diff.diff.context import TIER_ORDER

        assert set(TIER_ORDER) == set(MatchTier)
        ladder = [w.UNCERTAINTY_BY_TIER[t] for t in TIER_ORDER]
        assert ladder == sorted(ladder), "uncertainty must increase as the tier weakens"
        assert len(set(ladder)) == len(ladder)

    def test_a_name_pairing_is_doubtful_but_less_than_a_candidate(self) -> None:
        """ADR-036: the name tier pairs only where the name is one-to-one, so it carries some
        doubt -- more than a USR match -- but less than an entity left unpaired beside a
        possible partner."""
        name = w.UNCERTAINTY_BY_TIER[MatchTier.NAME]
        assert w.UNCERTAINTY_BY_TIER[MatchTier.DECLARED] < name
        assert name < w.UNCERTAINTY_BY_UNPAIRED[Unpaired.CANDIDATE]

    def test_the_unpaired_ladder_runs_the_same_direction_as_the_diff_stage_orders_it(
        self,
    ) -> None:
        """`diff.context.UNPAIRED_ORDER` picks an edge event's most doubtful endpoint; the
        weights must agree with it about which that is, or an edge would report its less
        doubtful endpoint."""
        from cpp_semantic_diff.diff.context import UNPAIRED_ORDER

        ladder = [w.UNCERTAINTY_BY_UNPAIRED[r] for r in UNPAIRED_ORDER]
        assert ladder == sorted(ladder) and len(set(ladder)) == len(ladder)
        assert set(UNPAIRED_ORDER) == set(Unpaired)
        assert w.UNCERTAINTY_BY_UNPAIRED[Unpaired.AMBIGUOUS] == max(
            *w.UNCERTAINTY_BY_TIER.values(), *w.UNCERTAINTY_BY_UNPAIRED.values()
        )

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
        edges at all (§13) and does emit `inherits`, which propagates change with no
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

        Reverse reachability that followed `contains` *downward* would give every entity in a
        large namespace the blast radius of the whole namespace -- on fmt, most of the library
        sits under `namespace fmt`. Containment is registered child -> container only: a
        changed member reaches its class, and nothing ever reaches a sibling.
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
        removed = event(ChangeEventKind.NODE_REMOVED, "c:@F@gone#", RESIDUE)
        score = rank([removed], base, model("b")).score_for(removed.id)
        detail = score.factor(FactorName.BLAST_RADIUS).detail
        assert "9 dependent(s)" in detail and "base model" in detail


class TestAnEdgeEventIsAboutItsSourceNotItsBusiestEndpoint:
    """The bug the first real run found, and the one that mattered most.

    Blast radius took the *larger* of an edge subject's two endpoint radii, so a new call into
    a popular utility inherited the utility's fan-in. On fmt at `HEAD~100` that put `EdgeAdded`
    events *above* the `NodeAdded(fmt::v12::vprintln)` that caused them, with the risk
    attributed to `detail::buffer::size` -- an entity that did not change and is not in the
    diff. 174 of 279 cause-linked events outranked their cause (commit `ef2daff`, 2026-08-02).

    ARCH-001 §7's question is "how much of the codebase does this change put at risk", and
    for `EdgeAdded(A -> B)` the thing that changed is A's behaviour. `graph.subject_home`
    answers with the source, which is the same answer `classify/rules.py` already gives when
    it picks a cause: an edge is written in its source's text.
    """

    def graph(self) -> Model:
        """One popular utility, one leaf that gains a call into it."""
        callers = [node(f"c:@F@c{i}#", file=LIB_SOURCE) for i in range(20)]
        return model(
            "m",
            node("c:@F@hot#", file=PUBLIC),
            node("c:@F@newcomer#", file=LIB_SOURCE),
            *callers,
            edges=[
                *[edge(c.id, "c:@F@hot#") for c in callers],
                edge("c:@F@newcomer#", "c:@F@hot#"),
            ],
        )

    def test_a_new_call_does_not_inherit_the_callees_fan_in(self) -> None:
        head = self.graph()
        call = edge_event("c:@F@newcomer#", "c:@F@hot#")
        detail = rank([call], head, head).score_for(call.id).factor(FactorName.BLAST_RADIUS).detail
        # The caller is a leaf; the callee has 21 dependents. Reporting the callee's number
        # would be attributing the utility's exposure to a change that did not touch it.
        assert detail.startswith("0 dependent(s)"), detail

    def test_the_entity_that_changed_outranks_the_edge_that_records_it(self) -> None:
        """Even with no `caused_by` link at all -- rank must not depend on classify."""
        head = self.graph()
        changed = event(ChangeEventKind.NODE_ADDED, "c:@F@newcomer#", RESIDUE)
        call = edge_event("c:@F@newcomer#", "c:@F@hot#")
        assert call.caused_by is None
        assert outranks(rank([changed, call], head, head), changed, call)

    def test_the_home_of_a_node_subject_is_the_node_itself(self) -> None:
        assert subject_home(NodeId("c:@F@f#")) == "c:@F@f#"
        assert subject_home(EdgeId("c:@F@f#|calls|c:@F@g#")) == "c:@F@f#"


class TestAConsequenceNeverOutranksItsCause:
    """ARCH-001 §6.3 says a change with 200 consequences is one event with 200
    consequences, and RESEARCH-002 §4 says the ordering is the product's core claim. So an
    event classify has already labelled a consequence cannot be the reason a reviewer starts
    reading -- the cause is.

    `_blast_radius` fixes the attribution; this bounds what is left. The two are separate on
    purpose: this one fires only when classify has run, and the other has to work when it
    has not.
    """

    def side(self) -> Model:
        """The real shape: a new test body that calls into public API."""
        return model(
            "m",
            node("c:@S@Test@F@TestBody#", file="test/format-test.cc", kind=NodeKind.METHOD),
            node("c:@F@format#", file=PUBLIC),
            edges=[edge("c:@S@Test@F@TestBody#", "c:@F@format#")],
        )

    def cascade(self) -> tuple[ChangeEvent, ChangeEvent]:
        cause = event(ChangeEventKind.NODE_ADDED, "c:@S@Test@F@TestBody#", RESIDUE)
        return cause, edge_event("c:@S@Test@F@TestBody#", "c:@F@format#", caused_by=cause)

    def test_the_cause_is_read_first(self) -> None:
        cause, effect = self.cascade()
        assert outranks(rank([cause, effect], self.side(), self.side()), cause, effect)

    def test_the_bound_says_which_event_explains_it(self) -> None:
        """A total that is no longer the sum of its own factors has to name the reason;
        the score is the product's explanation of itself (`score.py`)."""
        cause, effect = self.cascade()
        ranking = rank([cause, effect], self.side(), self.side())
        assert ranking.score_for(effect.id).bounded_by == cause.id
        assert ranking.score_for(cause.id).bounded_by is None

    def test_the_bound_only_ever_lowers_a_score(self) -> None:
        """It must not be able to manufacture importance: a consequence of a big change is
        not thereby a big change."""
        cause, effect = self.cascade()
        ranking = rank([cause, effect], self.side(), self.side())
        for score in ranking.scores:
            assert score.total <= sum(f.contribution for f in score.factors) + 1e-9

    def test_an_unbounded_score_is_still_exactly_the_sum_of_its_factors(self) -> None:
        cause, _ = self.cascade()
        score = rank([cause], self.side(), self.side()).score_for(cause.id)
        assert score.total == pytest.approx(sum(f.contribution for f in score.factors))

    def test_the_bound_follows_the_chain_not_just_one_link(self) -> None:
        """§6.3's cascades nest -- a method links to its class, the class to its namespace --
        so a consequence two links down is still bounded by the event at the top."""
        head = model(
            "m",
            node("c:@S@C", file=LIB_SOURCE, kind=NodeKind.CLASS),
            node("c:@S@C@F@m#", file=LIB_SOURCE, kind=NodeKind.METHOD),
            node("c:@F@format#", file=PUBLIC),
            edges=[
                edge("c:@S@C", "c:@S@C@F@m#", EdgeKind.CONTAINS),
                edge("c:@S@C@F@m#", "c:@F@format#"),
            ],
        )
        root = event(ChangeEventKind.NODE_ADDED, "c:@S@C", RESIDUE)
        middle = ChangeEvent(
            kind=ChangeEventKind.NODE_ADDED,
            subject=NodeId("c:@S@C@F@m#"),
            caused_by=root.id,
            confidence=RESIDUE,
        )
        leaf = edge_event("c:@S@C@F@m#", "c:@F@format#", caused_by=middle)
        ranking = rank([root, middle, leaf], head, head)
        assert ranking.order == [root.id, middle.id, leaf.id]
        assert ranking.score_for(leaf.id).total <= ranking.score_for(root.id).total

    def test_a_cause_link_pointing_outside_the_list_bounds_nothing(self) -> None:
        """The same rule `components.py` applies: rank holds no opinion about how the list
        was assembled, so a dangling link is ignored rather than raised on."""
        head = model("m", node("c:@F@f#", file=PUBLIC))
        dangling = ChangeEvent(
            kind=ChangeEventKind.NODE_BODY_CHANGED,
            subject=NodeId("c:@F@f#"),
            caused_by=ChangeEventId("NodeAdded:deadbeefdeadbeef"),
            confidence=SURE,
        )
        score = rank([dangling], head, head).score_for(dangling.id)
        assert score.bounded_by is None
        assert score.total == pytest.approx(sum(f.contribution for f in score.factors))


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
        added = event(ChangeEventKind.NODE_ADDED, "c:@F@f#", SURE)
        modified = body("c:@F@g#", SURE)
        assert outranks(rank([added, modified], self.side(), self.side()), added, modified)

    def test_a_removed_entity_outranks_an_added_one(self) -> None:
        """An added entity is a new promise; a removed one is a broken promise, and only the
        second can break code that is not in the diff."""
        assert (
            w.NOVELTY_BY_KIND[ChangeEventKind.NODE_REMOVED]
            > w.NOVELTY_BY_KIND[ChangeEventKind.NODE_ADDED]
        )

    def test_an_interface_attribute_change_outranks_a_body_change(self) -> None:
        virtual = event(
            ChangeEventKind.NODE_ATTR_CHANGED,
            "c:@S@C@F@m#",
            SURE,
            attr="virtual",
            old=False,
            new=True,
        )
        modified = body("c:@F@g#", SURE)
        assert outranks(rank([virtual, modified], self.side(), self.side()), virtual, modified)

    def test_the_attribute_is_named_in_the_score_not_only_the_kind(self) -> None:
        """`virtual: false -> true` and `mutable: false -> true` are the same `ChangeEventKind`
        and different amounts of risk, so the score has to say which attribute it read."""
        changed = event(
            ChangeEventKind.NODE_ATTR_CHANGED,
            "c:@S@C@F@m#",
            SURE,
            attr="const",
            old=True,
            new=False,
        )
        score = rank([changed], self.side(), self.side()).score_for(changed.id)
        assert score.factor(FactorName.NOVELTY).detail == "NodeAttrChanged(const)"

    def test_an_unknown_attribute_falls_back_above_a_body_change(self) -> None:
        """A producer that starts emitting a new attribute must not have it silently scored
        as nothing. An attribute with no rule is still a specific, named semantic change,
        and §7's rule that uncertainty is a reason to look applies to our own ignorance."""
        assert (
            w.NOVELTY_BY_KIND[ChangeEventKind.NODE_ATTR_CHANGED]
            > w.NOVELTY_BY_KIND[ChangeEventKind.NODE_BODY_CHANGED]
        )
        assert "surprise" not in w.NOVELTY_BY_ATTR
        unknown = event(
            ChangeEventKind.NODE_ATTR_CHANGED, "c:@S@C@F@m#", SURE, attr="surprise", old=1, new=2
        )
        modified = body("c:@F@g#", SURE)
        assert outranks(rank([unknown, modified], self.side(), self.side()), unknown, modified)

    def test_every_event_kind_has_a_weight(self) -> None:
        """`events.py` makes the event set closed and gives every kind an entry in
        `REQUIRED_PAYLOAD` so that adding one is a deliberate act. Same rule here: a kind
        with no weight would raise at scoring time, or worse, default to zero."""
        assert set(w.NOVELTY_BY_KIND) == set(ChangeEventKind)


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
            assert w.NOVELTY_BY_ATTR[attr] > w.NOVELTY_BY_KIND[ChangeEventKind.NODE_BODY_CHANGED]

    def test_there_is_no_signature_attribute_and_return_type_stands_in_for_it(self) -> None:
        """§7's fourth named attribute does not exist and cannot.

        `signature` is a field on `Node` (§4.1), never an entry in `attrs`, so the
        mechanical attribute diff can never emit `NodeAttrChanged(signature)`. In v0 a
        changed signature also changes the USR, which is tier-1 identity (§5), so the pair
        is never matched and the change arrives as remove + add. What *does* survive as an
        attribute is `return_type`; where it changes with the signature it arrives as
        `NodeSignatureChanged` instead (ADR-035), weighted level with it.
        """
        assert "signature" not in self.producer_keys()
        assert "signature" not in w.NOVELTY_BY_ATTR
        assert w.NOVELTY_BY_ATTR["return_type"] >= max(w.NOVELTY_BY_ATTR.values())
        assert (
            w.NOVELTY_BY_KIND[ChangeEventKind.NODE_SIGNATURE_CHANGED]
            == w.NOVELTY_BY_ATTR["return_type"]
        )


# -- exposure: the factor that decides whether this works on a real repository ------------


class TestTestCodeDoesNotDrownTheLibrary:
    """The measured problem, not a hypothetical one.

    The model built from `fmt` was 80.6 % test code, measured 2026-08-02 (v0 tracker §5,
    reports/2026-08-01--v0-build/STATUS.md). Extraction is right to include it. A ranking that
    puts a change to a test helper above a change to `fmt::format` is useless, and a reviewer
    shown that once will not read the second page.
    """

    def test_a_public_api_change_outranks_an_identical_change_to_test_code(self) -> None:
        head = model("m", node("c:@F@format#", file=PUBLIC), node("c:@F@helper#", file=OWN_TEST))
        public, test = body("c:@F@format#"), body("c:@F@helper#")
        assert outranks(rank([public, test], head, head), public, test)

    def test_one_library_change_beats_a_page_of_test_changes(self) -> None:
        """The realistic shape. It is not enough that public > test for one identical pair:
        on fmt the test events *outnumber* the library ones, so the library change has to
        reach the top of a mixed list, not merely beat its twin."""
        helpers = [node(f"c:@F@h{i}#", file=OWN_TEST) for i in range(10)]
        callers = [node(f"c:@F@c{i}#", file=LIB_SOURCE) for i in range(12)]
        head = model(
            "m",
            node("c:@F@format#", file=PUBLIC),
            *helpers,
            *callers,
            edges=[edge(c.id, "c:@F@format#") for c in callers],
        )
        library = body("c:@F@format#")
        noise = [event(ChangeEventKind.NODE_REMOVED, h.id, RESIDUE) for h in helpers]
        ranking = rank([*noise, library], head, head)
        assert ranking.order[0] == library.id

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("include/fmt/format.h", Exposure.PUBLIC),
            ("fmt/core.h", Exposure.PUBLIC),
            ("src/format.cc", Exposure.INTERNAL),
            ("include/spdlog/details/os.h", Exposure.INTERNAL),
            ("test/format-test.cc", Exposure.TEST),
            # Folder names exclude nothing (ADR-025): a library that is not declared
            # third-party is ranked as the repository's own code.
            ("test/gtest/gtest.h", Exposure.TEST),
            ("third_party/zlib/zlib.h", Exposure.PUBLIC),
            ("examples/demo.cc", Exposure.TEST),
            ("support/tools_test.cc", Exposure.TEST),
            # leveldb: tests live *beside* the code they test, with no `test/` directory
            # anywhere. fmt and spdlog both segregate, so until leveldb was added nothing
            # exercised the filename-stem half of the rule on a real layout.
            ("db/db_impl.cc", Exposure.INTERNAL),
            ("db/db_test.cc", Exposure.TEST),
            ("util/coding_test.cc", Exposure.TEST),
            ("include/leveldb/db.h", Exposure.PUBLIC),
            ("benchmarks/db_bench.cc", Exposure.TEST),
            ("third_party/googletest/googletest/src/gtest.cc", Exposure.INTERNAL),
        ],
    )
    def test_the_path_convention_reads_the_layouts_it_claims_to(
        self, path: str, expected: Exposure
    ) -> None:
        """The heuristic is directory convention and nothing else -- the model carries no
        install rules, no target interfaces and no `includes` edges (§4.2 declares them,
        §13 puts them out of v0). These are the layouts it is calibrated for; anything else
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


class TestAnEdgeIsAsExposedAsEitherOfItsEnds:
    """Exposure takes the **max** over an edge's endpoints where blast radius and
    decomposition take the source (`graph.subject_home`). The asymmetry is the decision, not
    an oversight, and `score.exposure_of` carries the measurement behind it.

    The short form: `subject_home` answers *attribution* -- whose text changed -- and the
    source is right for that. ARCH-001 §7 asks exposure whether the subject is reachable from a
    public header, which is a property of the pair an edge names. Of two settled decisions,
    "low confidence ranks up" wins over "the reviewer is reviewing their own code": the latter
    is a claim about attribution, and `subject_home` already spends it.
    """

    NAMESPACE = "c:@N@fmt@N@v12@N@detail"
    ADDED = "c:@N@fmt@N@v12@N@detail@SP>1#T@is_named_arg"
    OWN_TEST = "c:@F@check_format_as#"

    def side(self, *, with_added: bool) -> Model:
        """fmt `HEAD~12`'s shape: a namespace reopened in a test header, a new entity
        declared in a public one, and a test-code entity that went away."""
        namespace = node(self.NAMESPACE, file="test/scan.h", kind=NodeKind.NAMESPACE)
        if not with_added:
            return model("base", namespace, node(self.OWN_TEST, file="test/format-test.cc"))
        return model(
            "head",
            namespace,
            node(self.ADDED, file=PUBLIC, kind=NodeKind.CLASS),
            edges=[edge(self.NAMESPACE, self.ADDED, EdgeKind.CONTAINS)],
        )

    def test_adding_a_public_entity_to_a_namespace_is_a_public_change(self) -> None:
        """The measured case, and the reason the source is the wrong end here.

        A `contains` edge's source is the *container*, and a namespace is reopened per file
        so its deduped node keeps one arbitrary location (`exposure.py` says so and declines
        to promote through it). On fmt `HEAD~12` that location is `test/scan.h`, so reading
        the source alone calls the addition of `include/fmt/base.h` entities test code --
        and splits one change across the demotion on the strength of which header happened
        to reopen the namespace last.
        """
        base, head = self.side(with_added=False), self.side(with_added=True)
        containment = edge_event(self.NAMESPACE, self.ADDED, edge_kind=EdgeKind.CONTAINS)
        ranking = rank([containment], base, head)

        # Without this the test would pass on a source-only rule the day the namespace node
        # happened to be deduped to a public header, which is not a property anything can
        # rely on. The source *is* test code; the claim is that it does not decide.
        assert exposure_map(head)[NodeId(self.NAMESPACE)] is Exposure.TEST
        assert ranking.score_for(containment.id).exposure is Exposure.PUBLIC

    def test_it_outranks_unrelated_test_churn_that_the_source_rule_would_put_above_it(
        self,
    ) -> None:
        """The ordering claim, which is what a reviewer sees. fmt `HEAD~12`: reading the source
        drops these two events from ranks 5-6 to 9-10, below four test-code removals, while the
        `NodeAdded` they belong to stays public at ranks 1-2 (measured 2026-09-20, v0 tracker
        §6, reports/2026-08-01--v0-build/STATUS.md)."""
        base, head = self.side(with_added=False), self.side(with_added=True)
        containment = edge_event(self.NAMESPACE, self.ADDED, edge_kind=EdgeKind.CONTAINS)
        churn = event(ChangeEventKind.NODE_REMOVED, self.OWN_TEST, RESIDUE)
        ranking = rank([churn, containment], base, head)
        assert outranks(ranking, containment, churn)


# -- decomposition ----------------------------------------------------------------------


class TestThreeDisconnectedChangesAreThreeChanges:
    """ARCH-001 §7 and RESEARCH-002 §3.11: "A PR whose delta has three disconnected
    components is three PRs." The intent-conformance signal at nearly zero cost.

    What connects two events is the whole design, and the failure is asymmetric: connect too
    much and the signal becomes a constant one-component answer that still *looks* like it
    works.
    """

    def three_islands(self) -> tuple[Model, list[ChangeEvent]]:
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
        effect = ChangeEvent(
            kind=ChangeEventKind.NODE_BODY_CHANGED,
            subject=NodeId("c:@F@c1#"),
            caused_by=cause.id,
            confidence=SURE,
        )
        assert len(rank([cause, effect], head, head).components) == 1

    def test_a_cause_link_pointing_outside_the_list_is_ignored(self) -> None:
        """Rank must not acquire an opinion about how the event list was assembled."""
        head = model("m", node("c:@F@a1#"), node("c:@F@c1#"))
        dangling = ChangeEvent(
            kind=ChangeEventKind.NODE_BODY_CHANGED,
            subject=NodeId("c:@F@c1#"),
            caused_by=ChangeEventId("NodeAdded:deadbeefdeadbeef"),
            confidence=SURE,
        )
        assert len(rank([body("c:@F@a1#"), dangling], head, head).components) == 2

    def hub(self) -> Model:
        """One untouched utility that two unrelated new functions both start calling.

        The measured shape. On fmt at `HEAD~100` the giant component was held together through
        vendored `test/gtest`, because every new `TEST_F` body calls
        `testing::internal::AssertHelper` and overrides `testing::Test::TestBody` -- so
        independent test additions across many files arrived as one change.
        """
        return model(
            "m",
            node("c:@F@util#", file=PUBLIC),
            node("c:@F@a#", file=LIB_SOURCE),
            node("c:@F@b#", file=LIB_SOURCE),
            edges=[edge("c:@F@a#", "c:@F@util#"), edge("c:@F@b#", "c:@F@util#")],
        )

    def test_two_new_calls_into_one_unchanged_utility_are_two_changes(self) -> None:
        """The rule the whole partition depended on and did not have. An entity with no event
        of its own did not change; that two changes both reach it is a fact about the utility,
        not evidence that they are one change. 78 % of events landed in one component on fmt at
        `HEAD~100` for want of this (commit `ef2daff`, 2026-08-02), and the numbers said so
        while the output still looked like a partition."""
        head = self.hub()
        events = [
            edge_event("c:@F@a#", "c:@F@util#"),
            edge_event("c:@F@b#", "c:@F@util#"),
        ]
        assert len(rank(events, head, head).components) == 2

    def test_but_a_utility_that_also_changed_still_joins_them(self) -> None:
        """The other half, and the reason the rule is "both ends have an event" rather than
        "edge events only count for their source". If the callee changed too, the caller and
        the callee are one change -- which is what makes a shared-utility edit read as one
        unit instead of N call sites."""
        head = self.hub()
        events = [
            edge_event("c:@F@a#", "c:@F@util#"),
            edge_event("c:@F@b#", "c:@F@util#"),
            body("c:@F@util#"),
        ]
        assert len(rank(events, head, head).components) == 1

    def test_an_edge_event_lands_with_the_entity_whose_text_changed(self) -> None:
        """`EdgeAdded(f -> g)` is a change to f. It groups with f's other events whether or
        not g has any."""
        head = self.hub()
        events = [edge_event("c:@F@a#", "c:@F@util#"), body("c:@F@a#")]
        assert len(rank(events, head, head).components) == 1

    def test_a_shared_class_does_not_re_import_the_bridging(self) -> None:
        """Rule 4 reads the same one home entity per event as the rest. Reading it over
        every entity an event *mentions* let one class carry back exactly the connection
        rule 1 had just given up -- which is how the giant component survived the first
        attempt at this fix, dropping from 245 events to 89 at rule 3 and back to 245 at
        rule 4."""
        head = model(
            "m",
            node("c:@S@Util", file=PUBLIC, kind=NodeKind.CLASS),
            node("c:@S@Util@F@m#", file=PUBLIC, kind=NodeKind.METHOD),
            node("c:@S@Util@F@n#", file=PUBLIC, kind=NodeKind.METHOD),
            node("c:@F@a#", file=LIB_SOURCE),
            node("c:@F@b#", file=LIB_SOURCE),
            edges=[
                edge("c:@S@Util", "c:@S@Util@F@m#", EdgeKind.CONTAINS),
                edge("c:@S@Util", "c:@S@Util@F@n#", EdgeKind.CONTAINS),
                edge("c:@F@a#", "c:@S@Util@F@m#"),
                edge("c:@F@b#", "c:@S@Util@F@n#"),
            ],
        )
        events = [
            edge_event("c:@F@a#", "c:@S@Util@F@m#"),
            edge_event("c:@F@b#", "c:@S@Util@F@n#"),
        ]
        assert len(rank(events, head, head).components) == 2

    def test_components_are_numbered_by_risk_so_the_first_one_is_read_first(self) -> None:
        head = model("m", node("c:@F@a1#", file=OWN_TEST), node("c:@F@c1#", file=PUBLIC))
        low, high = body("c:@F@a1#"), body("c:@F@c1#")
        ranking = rank([low, high], head, head)
        assert ranking.components[0].events == [high.id]
        assert ranking.score_for(high.id).component == 0


# -- the stage's own contract ------------------------------------------------------------


class TestHighRiskCountsConsequencesNotNovelty:
    """ARCH-001 §10 prints `summary.high_risk_count` on every run, so it has to be a
    measurement. As shipped it was 133 of 335 events (40 %) on fmt at `HEAD~100` (commit
    `ef2daff`, 2026-08-02), and that was structural rather than a mis-tuned threshold: in v0
    (tier-1 identity only, §13) *every* add and remove is unmatched residue, so the uncertainty
    factor sits at its maximum for that whole population -- a constant 0.15 offset, not a
    measurement -- and it is exactly what lifted a public addition with no dependents at all
    from 0.4625 over the 0.6 line. The number meant "public and new".

    The fix is not to stop counting uncertainty, which §7 forbids: uncertainty still ranks
    an event **up**, and `TestUncertaintyRanksUpNotDown` still passes unchanged. It is to
    count high risk on `impact` -- blast radius, exposure and novelty, rescaled -- because
    risk is a claim about consequences and our doubt about a match is not one of them.
    """

    def side(self) -> Model:
        return model("m", node("c:@F@fresh#", file=PUBLIC), node("c:@F@other#", file=PUBLIC))

    def test_a_public_addition_that_nothing_depends_on_is_not_high_risk(self) -> None:
        """The 40 % population, one event at a time. It is worth reading -- it is at the top
        of a small delta -- but "high risk" has to mean something a reviewer can act on."""
        added = event(ChangeEventKind.NODE_ADDED, "c:@F@fresh#", RESIDUE)
        score = rank([added], self.side(), self.side()).score_for(added.id)
        assert score.factor(FactorName.BLAST_RADIUS).value == 0.0
        assert not score.is_high_risk

    def test_the_same_change_with_dependents_is_high_risk(self) -> None:
        """The term that has to be doing the work is the one a text diff cannot compute."""
        callers = [node(f"c:@F@c{i}#", file=LIB_SOURCE) for i in range(40)]
        head = model(
            "m",
            node("c:@F@fresh#", file=PUBLIC),
            *callers,
            edges=[edge(c.id, "c:@F@fresh#") for c in callers],
        )
        added = event(ChangeEventKind.NODE_ADDED, "c:@F@fresh#", RESIDUE)
        assert rank([added], head, head).score_for(added.id).is_high_risk

    def test_the_matcher_does_not_decide_what_is_dangerous(self) -> None:
        """The property the old threshold lacked: an event's risk must not move when a tier
        that has nothing to do with it starts or stops running. Tiers 2-4 may land later, and
        `high_risk_count` must not swing when they do."""
        confidences = [
            AMBIGUOUS,
            CANDIDATE,
            RESIDUE,
            *(
                Confidence(tier=t, models_complete=True)
                for t in (MatchTier.SIMILARITY, MatchTier.POSITIONAL, MatchTier.DECLARED)
            ),
        ]
        verdicts = {
            rank([body("c:@F@fresh#", c)], self.side(), self.side())
            .score_for(body("c:@F@fresh#").id)
            .is_high_risk
            for c in confidences
        }
        assert len(verdicts) == 1

    def test_an_incomplete_model_does_not_inflate_the_count_either(self) -> None:
        """§4.4's flag is global today, so it would shift every event equally -- straight
        through a fixed threshold, turning one degraded run into a page of red."""
        degraded = body("c:@F@fresh#", Confidence(tier=MatchTier.DECLARED, models_complete=False))
        complete = body("c:@F@other#", Confidence(tier=MatchTier.DECLARED, models_complete=True))
        ranking = rank([degraded, complete], self.side(), self.side())
        # It still ranks up -- that is §7 -- and it still is not a claim about consequences.
        assert outranks(ranking, degraded, complete)
        assert ranking.score_for(degraded.id).is_high_risk == (
            ranking.score_for(complete.id).is_high_risk
        )

    def test_impact_is_on_the_same_scale_as_the_total(self) -> None:
        """One threshold serves both, so the subset has to be rescaled by its own weight sum
        rather than compared against a number calibrated for four factors."""
        assert pytest.approx(sum(w.FACTOR_WEIGHTS[n] for n in w.IMPACT_FACTORS)) == w.IMPACT_WEIGHT
        assert set(w.FACTOR_WEIGHTS) > w.IMPACT_FACTORS
        assert "uncertainty" not in w.IMPACT_FACTORS
        head = model("m", node("c:@F@fresh#", file=PUBLIC))
        for score in rank([body("c:@F@fresh#")], head, head).scores:
            assert 0.0 <= score.impact <= 1.0


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

    def fixture(self) -> tuple[Model, list[ChangeEvent]]:
        head = model(
            "m",
            node("c:@F@f#", file=PUBLIC),
            node("c:@F@g#", file=LIB_SOURCE),
            node("c:@F@h#", file=OWN_TEST),
            edges=[edge("c:@F@g#", "c:@F@f#"), edge("c:@F@h#", "c:@F@g#")],
        )
        events = [
            body("c:@F@f#"),
            event(ChangeEventKind.NODE_ADDED, "c:@F@g#", RESIDUE),
            event(ChangeEventKind.NODE_REMOVED, "c:@F@h#", RESIDUE),
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
        """Not tidiness. `ChangeEvent` is mutable, `caused_by` is written by a different stage,
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
