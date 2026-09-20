"""The risk score: ARCH-001 §7's four inputs, kept apart all the way to the output.

A score nobody can interrogate will not be trusted, and cannot be tuned. So the total is
never the product: every factor is carried alongside it with the raw measurement that
produced it, the weight applied, and the contribution that resulted, and `render` (§10) can
say *why* something is first without recomputing anything or knowing how it was computed.
That is also what makes calibration possible -- a ranking that looks wrong can be diagnosed
down to the term responsible rather than argued about as a whole.

Each factor is normalized to [0, 1] before weighting, and the weights sum to 1
(`weights.py`), so `total` is in [0, 1] and means the same thing across runs and across
repositories. Nothing here is stateful and nothing here mutates an input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cpp_semantic_diff.model import (
    Confidence,
    Event,
    EventId,
    EventKind,
    Model,
    NodeId,
)
from cpp_semantic_diff.rank import weights as w
from cpp_semantic_diff.rank.exposure import EXPOSURE_ORDER, Exposure, exposure_map
from cpp_semantic_diff.rank.graph import BlastGraph, subject_endpoints, subject_home


class FactorName(StrEnum):
    """The four inputs of ARCH-001 §7, named so consumers switch on a value rather than a
    string literal -- the same reason `EventKind` is a closed set (`events.py`)."""

    BLAST_RADIUS = "blast_radius"
    EXPOSURE = "exposure"
    NOVELTY = "novelty"
    UNCERTAINTY = "uncertainty"


class Factor(BaseModel):
    """One term of one score, with everything needed to explain or re-derive it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: FactorName
    value: float = Field(description="The normalized measurement, in [0, 1].", ge=0.0, le=1.0)
    weight: float = Field(description="From `weights.py`; the only tunable half.")
    contribution: float = Field(description="value * weight -- what this term added.")
    detail: str = Field(
        description="The raw measurement in words: '37 dependent(s), 4 direct', 'public', "
        "'NodeAttrChanged(virtual)', 'unmatched'. This is what a reviewer reads when the "
        "ranking surprises them, and it is deliberately not reconstructible from `value`."
    )


class RiskScore(BaseModel):
    """One event's place in the order, and the whole of the reason for it."""

    model_config = ConfigDict(extra="forbid")

    event_id: EventId
    total: float = Field(
        description="Sum of the contributions, in [0, 1] -- except where `bounded_by` "
        "names a cause, in which case it is the cause's total (see `ranker.py`)."
    )
    impact: float = Field(
        default=0.0,
        description="The three factors that measure the change itself -- blast radius, "
        "exposure, novelty -- rescaled to their own weight sum, so it is in [0, 1] and "
        "comparable to `total`. `total` decides the *order* and includes uncertainty, "
        "because ARCH-001 §7 wants doubt to rank up; `impact` decides `is_high_risk`, "
        "because a headline count of dangerous changes must not be inflated by our own "
        "ignorance. See `weights.IMPACT_FACTORS`.",
        ge=0.0,
        le=1.0,
    )
    factors: list[Factor]
    component: int = Field(
        default=0,
        description="Index of the connected component this event belongs to (ARCH-001 §7). "
        "Components are numbered by descending risk, so component 0 is the change a "
        "reviewer should read first.",
    )
    bounded_by: EventId | None = Field(
        default=None,
        description="The cause (§6.3) whose score capped this one. Recorded rather than "
        "applied silently: `total` is otherwise the sum of the contributions, and a number "
        "that stops being explained by its own factors has to say why.",
    )
    exposure: Exposure = Field(
        default=Exposure.INTERNAL,
        description="The subject's exposure class, carried as a field and not only inside "
        "`factors` because the order partitions on it: no vendored event outranks any "
        "first-party one, whatever the scores say (`ranker.py`).",
    )

    def factor(self, name: FactorName) -> Factor:
        for f in self.factors:
            if f.name is name:
                return f
        raise KeyError(f"score for {self.event_id} carries no {name.value} factor")

    @property
    def is_high_risk(self) -> bool:
        """ARCH-001 §10's `summary.high_risk_count`, one event at a time.

        On `impact`, not on `total`, and the reason is structural rather than a retune. In
        v0 (tier-1 identity only, §13) every add and every remove is unmatched residue, so
        the uncertainty factor is pinned at its maximum for that whole population -- a
        constant offset, carrying no information *within* it. Against a fixed threshold that
        offset is decisive: a public `NodeAdded` with no dependents at all scored
        0.25 + 0.2125 + 0.15 = 0.6125 and cleared 0.6 on the strength of being new. On fmt
        at `HEAD~100` that made 133 of 335 events (40 %) "high risk", which is a measurement
        of how much of the diff is new, not of how much of it is dangerous.

        Dropping uncertainty from the *count* is not the inversion §7 forbids. §7's rule is
        about attention -- uncertainty is a reason for a human to look -- and attention is
        the ordering, which still reads all four factors. Risk is a claim about
        consequences, and our doubt about a match is not one of them.
        """
        return self.impact >= w.HIGH_RISK


@dataclass(frozen=True)
class ScoringContext:
    """Everything the four factors read, derived once for the whole event list.

    Built from the two models and nothing else, so scoring stays a pure function of the
    stage's inputs (ARCH-001 §3).
    """

    base: Model
    head: Model
    head_blast: BlastGraph
    base_blast: BlastGraph
    exposure: dict[NodeId, Exposure]

    @classmethod
    def of(cls, base: Model, head: Model) -> ScoringContext:
        # Head first, then base, so a node present on both sides is classified by what it
        # is *now*: a helper moved out of a public header during this change is internal.
        merged = exposure_map(base) | exposure_map(head)
        return cls(
            base=base,
            head=head,
            head_blast=BlastGraph.of(head),
            base_blast=BlastGraph.of(base),
            exposure=merged,
        )


def score_event(event: Event, ctx: ScoringContext) -> RiskScore:
    """One event -> one score. No ordering, no components; `ranker.py` owns both."""
    factors = [
        _blast_radius(event, ctx),
        _exposure(event, ctx),
        _novelty(event),
        _uncertainty(event.confidence),
    ]
    total = sum(f.contribution for f in factors)
    measured = sum(f.contribution for f in factors if f.name.value in w.IMPACT_FACTORS)
    return RiskScore(
        event_id=event.id,
        total=total,
        impact=measured / w.IMPACT_WEIGHT,
        factors=factors,
        exposure=exposure_of(event, ctx),
    )


def _factor(name: FactorName, value: float, detail: str) -> Factor:
    value = min(1.0, max(0.0, value))
    weight = w.FACTOR_WEIGHTS[name.value]
    return Factor(name=name, value=value, weight=weight, contribution=value * weight, detail=detail)


# -- 1 · blast radius -------------------------------------------------------------------


def _blast_radius(event: Event, ctx: ScoringContext) -> Factor:
    """How much depends on what changed, bounded (ARCH-001 §7).

    Computed on the **head** model as §7 says, with one documented exception: a subject that
    does not exist at head -- every `NodeRemoved` and most `EdgeRemoved` are in base
    coordinates by construction (`diff/nodes.py`) -- is measured on the base model instead.
    The alternative is scoring every removal at zero, which would systematically bury the
    one event kind that can break code outside the diff. The side used is named in `detail`
    rather than assumed, because "40 things used to depend on this" and "40 things depend on
    this" are different claims.

    An edge subject is measured from its **source only** -- `graph.subject_home`, which
    carries the argument. Taking the larger of the two endpoints, as this did, answers a
    question nobody asked: for `EdgeAdded(A -> B)` the thing that changed is A's behaviour,
    and B's fan-in is a fact about B that this change did not touch. On fmt at `HEAD~100`
    that put ten `EdgeAdded` events at 0.84-0.57 above the `NodeAdded(fmt::v12::vprintln)`
    at 0.70 that *caused* them, with the risk attributed to `detail::buffer::size` -- an
    entity not in the diff. `_bound_by_cause` in `ranker.py` is the second half of the fix
    and covers the case where classify has linked the consequence; this half has to work
    when it has not, because rank must not depend on classify having run.
    """
    home = subject_home(event.subject)

    if home in ctx.head.nodes:
        side, graph = "head", ctx.head_blast
    elif home in ctx.base.nodes:
        side, graph = "base", ctx.base_blast
    else:
        # Neither model has the subject. That is not a measurement of zero -- it is the
        # absence of one, and §11 forbids reporting the two the same way.
        return _factor(FactorName.BLAST_RADIUS, 0.0, "subject not in either model")

    result = graph.radius([home], max_depth=w.BLAST_MAX_DEPTH, visit_cap=w.BLAST_VISIT_CAP)
    return _factor(
        FactorName.BLAST_RADIUS,
        _saturate(result.count),
        f"{result.detail}, {side} model",
    )


def _saturate(count: int) -> float:
    """Logarithmic, saturating at `BLAST_SATURATION`. See `weights.py` for why log."""
    return math.log1p(count) / math.log1p(w.BLAST_SATURATION)


# -- 2 · exposure -----------------------------------------------------------------------


def exposure_of(event: Event, ctx: ScoringContext) -> Exposure:
    """The subject's exposure class -- `exposure.py` owns the heuristic.

    **An edge takes the maximum of its two endpoints, and deliberately does not follow
    `graph.subject_home` the way blast radius and decomposition now do.** That asymmetry was
    raised as a defect; it is kept, and measured rather than argued. `subject_home` answers a
    question about *attribution* -- whose text changed, whose behaviour changed, what is
    coupled to what -- and the source is the right answer to all three. ARCH-001 §7 asks
    exposure a different question: "is the subject reachable from a public header". That is a
    property of the pair an edge names, not of one end of it, and for a `contains` edge the
    source is the *container* -- the one node whose location this model cannot pin down.

    Measured on all three fixtures, max against source-only:

    - **spdlog `HEAD~30` (9 edge events) and leveldb `HEAD~10` (2,351) are identical.** Not
      one event changes class, score, rank, component or high-risk status. On leveldb every
      edge that crosses the vendored boundary crosses *into* it -- 114 `test -> vendored` and
      2 `public -> vendored`, the repository's own tests calling googletest -- where the
      source is already the more exposed end and the two rules agree by construction.
    - **fmt `HEAD~12` holds the only two divergences, and they are the argument for max.**
      Both are the `contains` edges that put this commit's two new partial specializations
      into `namespace fmt::v12::detail`; the specializations are declared in
      `include/fmt/base.h`, and the namespace's deduped node carries one arbitrary location,
      which here is `test/scan.h` -- precisely the instability `exposure.py` says it will not
      rely on. Source-only therefore reads "a public API entity was added" as TEST and drops
      the two events from ranks 5-6 to 9-10, below four test-code removals, while the
      `NodeAdded` they belong to stays PUBLIC at ranks 1-2. One change, split across the
      demotion, on the strength of which header happened to reopen a namespace last.

    So of STATUS §8's two decisions, *"low confidence ranks up"* wins over *"the reviewer is
    reviewing their own code"*. The second is a claim about attribution and it is already
    spent -- `subject_home` applies it everywhere the question is what changed. Exposure is
    not that question, and max is the reading that cannot hide: it only ever moves an event
    toward the reviewer, never out of sight.

    **The one direction max can err in, named rather than guarded.** It never pulls a
    first-party event into `ranker.py`'s vendored block, but the reverse is open: an
    `EdgeAdded(gtest_helper -> leveldb::Status)` would score PUBLIC and escape the partition
    on the strength of an endpoint its source did not write. There are **zero** such events
    across the three fixtures -- no `vendored -> {test, internal, public}` edge event on
    leveldb, the only one with a moving vendored dependency -- so flooring the partition on
    the source class while the weighted factor kept the max would be a second mechanism
    defending a hazard nothing has produced. The trigger to revisit is an observed instance,
    and it is cheap to watch for: it is a divergence whose source is VENDORED.

    A subject in neither model is INTERNAL rather than VENDORED: the partition in `ranker.py`
    hides vendored events, and an unclassifiable subject must not be hidden by accident.
    """
    endpoints = subject_endpoints(event.subject)
    classes = [ctx.exposure[n] for n in endpoints if n in ctx.exposure]
    if not classes:
        return Exposure.INTERNAL
    return max(classes, key=EXPOSURE_ORDER.index)


def _exposure(event: Event, ctx: ScoringContext) -> Factor:
    exposure = exposure_of(event, ctx)
    known = any(n in ctx.exposure for n in subject_endpoints(event.subject))
    detail = exposure.value if known else f"{exposure.value} (subject not in either model)"
    return _factor(FactorName.EXPOSURE, w.EXPOSURE_WEIGHT[exposure], detail)


# -- 3 · novelty ------------------------------------------------------------------------


def _novelty(event: Event) -> Factor:
    """What kind of change it is (ARCH-001 §7), refined by which attribute for attr events.

    The attribute table is a contract with the producer rather than with the doc's prose --
    see `weights.NOVELTY_BY_ATTR`, which is where the mismatch is spelled out.
    """
    if event.kind is EventKind.NODE_ATTR_CHANGED:
        attr = event.payload.get("attr")
        key = attr if isinstance(attr, str) else ""
        value = w.NOVELTY_BY_ATTR.get(key, w.NOVELTY_BY_KIND[event.kind])
        return _factor(FactorName.NOVELTY, value, f"{event.kind.value}({key})")
    return _factor(FactorName.NOVELTY, w.NOVELTY_BY_KIND[event.kind], event.kind.value)


# -- 4 · confidence, inverted -----------------------------------------------------------


def _uncertainty(confidence: Confidence) -> Factor:
    """**A low-confidence event ranks up, not down** (ARCH-001 §7).

    This is the one factor whose sign is opposite to intuition, and getting it backwards is
    invisible: the output is still a plausible ordering, it just systematically buries the
    events the tool is least sure about -- which are exactly the ones only a human can
    settle. `Confidence` deliberately keeps its two raw inputs rather than collapsing them
    to a scalar (`events.py`) precisely so that this inversion is applied once, here, by the
    stage that owns the weighting.
    """
    value = w.UNCERTAINTY_BY_TIER[confidence.tier]
    detail = confidence.tier.value if confidence.tier else "unmatched"
    if not confidence.models_complete:
        value += w.UNCERTAINTY_INCOMPLETE_MODELS
        detail = f"{detail}, incomplete models"
    return _factor(FactorName.UNCERTAINTY, value, detail)
