"""The rank stage (ARCH-001 §3, §7): events plus two models in, an order out.

RESEARCH-002 §4 is the argument this stage rests on: every question in §3 assumes the
reviewer is already looking at the right thing, and *the higher-value output is telling them
what to look at*. That is why ranking is a stage rather than a sort in the renderer -- the
ordering and the decomposition are analysis, derived from the two models, and `cli.py` must
never be in a position to know how a score was computed.

A pure function of its three inputs. It reads no files, mutates nothing it is given, and
produces the same document for the same inputs on every run -- which is not a nicety: the
JSON is the product (§10), and a `ranking` array that reshuffles between runs makes every
diff of the output meaningless and every regression test flaky.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from cpp_semantic_diff.model import Event, EventId, Model
from cpp_semantic_diff.rank import weights as w
from cpp_semantic_diff.rank.components import Component, components
from cpp_semantic_diff.rank.exposure import Exposure
from cpp_semantic_diff.rank.score import RiskScore, ScoringContext, score_event


class Ranking(BaseModel):
    """What §10's output document needs: an order, the reason for it, and the partition."""

    model_config = ConfigDict(extra="forbid")

    scores: list[RiskScore] = Field(
        description="Highest risk first. One entry per input event, none dropped -- "
        "attention allocation is an ordering, and an event this stage decided to omit is "
        "an event no view can restore."
    )
    components: list[Component] = Field(
        description="The delta partitioned into independently reviewable units (§7), "
        "numbered by descending risk."
    )

    @property
    def order(self) -> list[EventId]:
        """ARCH-001 §10's `ranking: [event id]`."""
        return [s.event_id for s in self.scores]

    @property
    def vendored_count(self) -> int:
        """Events in bundled dependencies, which sort last as a block.

        Reported so the partition is visible. A view that shows the top N and says nothing
        would be silently dropping 96 % of the delta on a dependency bump, and "the tool
        found almost nothing" and "the tool set almost everything aside" have to be told
        apart from the output alone.
        """
        return sum(1 for s in self.scores if s.exposure is Exposure.VENDORED)

    @property
    def high_risk_count(self) -> int:
        """The `summary.high_risk_count` of §10; the threshold lives in `weights.py`.

        **What the number means, in one sentence:** the count of events whose measured
        consequences -- blast radius, exposure and the kind of change -- clear the high-risk
        threshold on their own, with the tool's uncertainty about the match kept out of the
        count even though it is kept in the order (`RiskScore.is_high_risk`).
        """
        return sum(1 for s in self.scores if s.is_high_risk)

    def score_for(self, event_id: EventId) -> RiskScore:
        for s in self.scores:
            if s.event_id == event_id:
                return s
        raise KeyError(f"no score for {event_id}")


def _cause_depth(events: Sequence[Event]) -> dict[EventId, int]:
    """How far down a cascade each event sits: 0 for a cause or an unlinked event.

    Used only as a tie-break. `_bound_by_cause` can leave a consequence scoring *exactly*
    what its cause scores, and at equal totals the id decides -- which would put the
    consequence first half the time and undo the fix in the one place a reviewer sees it.
    Depth says the obvious thing instead: at equal risk, read the cause before the thing it
    caused.
    """
    cause_of = {e.id: e.caused_by for e in events if e.caused_by is not None}
    known = {e.id for e in events}
    depth: dict[EventId, int] = {}

    def resolve(event_id: EventId, guard: frozenset[EventId]) -> int:
        if event_id in depth:
            return depth[event_id]
        cause = cause_of.get(event_id)
        # A link out of the list is ignored, not raised on -- the same rule `components.py`
        # applies, and for the same reason: rank must hold no opinion about how the list was
        # assembled. `guard` covers a cycle, which classify should not produce and which
        # would otherwise recurse until the interpreter stopped.
        result = (
            0
            if cause is None or cause not in known or cause in guard
            else 1 + resolve(cause, guard | {event_id})
        )
        depth[event_id] = result
        return result

    for event in events:
        resolve(event.id, frozenset({event.id}))
    return depth


def _bound_by_cause(scores: list[RiskScore], events: Sequence[Event]) -> list[RiskScore]:
    """No consequence outscores its cause (ARCH-001 §6.3, §7).

    §6.3 says a rename with 200 call-edge consequences is *one* event with 200
    consequences, and RESEARCH-002 §4 says the ordering is the product's core claim. An
    event classify has already labelled as a consequence therefore cannot be the reason a
    reviewer starts reading: the cause is. Scoring is per-event by design (`score_event`
    cannot see the list), so the relation is applied here, over the set.

    A cap rather than a re-derivation, and both halves matter. The cap only ever moves a
    score *down*, so it cannot manufacture importance; and it is recorded in `bounded_by`,
    so a total that is no longer the sum of its own factors says which event explains it.

    Taken as **data**, exactly as `components.py` takes it: `caused_by` may be unpopulated,
    because classify is a separate stage that may not have run, and a link pointing outside
    the list is ignored rather than raised on. Nothing here fires when `caused_by` is empty,
    which is why `_blast_radius` had to be fixed independently rather than relying on this.
    """
    cause_of = {e.id: e.caused_by for e in events if e.caused_by is not None}
    by_id = {s.event_id: s for s in scores}
    bounded: dict[EventId, RiskScore] = {}

    def resolve(event_id: EventId, guard: frozenset[EventId]) -> RiskScore:
        if event_id in bounded:
            return bounded[event_id]
        score = by_id[event_id]
        cause_id = cause_of.get(event_id)
        if cause_id is not None and cause_id in by_id and cause_id not in guard:
            cause = resolve(cause_id, guard | {event_id})
            if cause.total < score.total or cause.impact < score.impact:
                score = score.model_copy(
                    update={
                        "total": min(score.total, cause.total),
                        "impact": min(score.impact, cause.impact),
                        "bounded_by": cause_id,
                    }
                )
        bounded[event_id] = score
        return score

    return [resolve(s.event_id, frozenset({s.event_id})) for s in scores]


def rank_events(events: Sequence[Event], base: Model, head: Model) -> Ranking:
    """Order the delta by risk and partition it into independent changes.

    **Vendored code sorts after everything first-party, whatever the scores say.** A reviewer
    is reviewing their own code; a bumped dependency is context, not the subject. Exposure was
    a weighted term and that was not enough -- measured on leveldb, where a submodule bump made
    the top 12 events and 44 of the top 50 vendored googletest. The arithmetic: exposure is
    capped at `W_EXPOSURE` (0.25), so the VENDORED-to-PUBLIC gap is worth 0.2375 of score,
    while blast radius (0.35) and novelty (0.25) together are 0.60 -- so a removed gtest node
    with 241 dependents (0.76) beat a changed public attribute (~0.44).

    A partition rather than a bigger weight, deliberately. Raising `W_EXPOSURE` until leveldb
    looked right would fossilize a number against one repository (STATUS §9) and would still
    lose to a large enough blast radius on the next one. The claim being encoded is not
    "vendored is 4x less interesting", it is "vendored is not what is being reviewed", and
    that is an ordering, not a quantity. The weight stays for ordering vendored events
    *among themselves*.

    Only VENDORED is partitioned. The project's own tests are the reviewer's code and keep
    their weighted demotion -- `db/db_test.cc` is theirs to fix, `third_party/googletest` is
    not.

    Ties break on the cascade depth and then the event id, which is content-derived
    (`events.py`) and therefore stable across runs. Sorting on the score alone would leave
    the order of equally-risky events -- the common case, since novelty and exposure are
    drawn from small tables -- decided by whatever order the diff stage happened to emit
    them in.
    """
    ctx = ScoringContext.of(base, head)
    scored = _bound_by_cause([score_event(event, ctx) for event in events], events)

    depth = _cause_depth(events)
    scored.sort(
        key=lambda s: (s.exposure is Exposure.VENDORED, -s.total, depth[s.event_id], s.event_id)
    )
    order = [s.event_id for s in scored]
    totals = {s.event_id: s.total for s in scored}

    parts, index_of = components(list(events), base, head, order, totals)
    # `component` is the one field written after scoring: it is a property of the event
    # *set*, which `score_event` deliberately cannot see. Rebuilt rather than assigned in
    # place, so that a `RiskScore` handed out by this function is never observed twice with
    # different contents.
    scored = [s.model_copy(update={"component": index_of[s.event_id]}) for s in scored]

    return Ranking(scores=scored, components=parts)


#: Re-exported so a consumer asking "is this high risk" reads one threshold, not two.
HIGH_RISK = w.HIGH_RISK
