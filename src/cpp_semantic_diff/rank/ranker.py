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
    def high_risk_count(self) -> int:
        """The `summary.high_risk_count` of §10; the threshold lives in `weights.py`."""
        return sum(1 for s in self.scores if s.is_high_risk)

    def score_for(self, event_id: EventId) -> RiskScore:
        for s in self.scores:
            if s.event_id == event_id:
                return s
        raise KeyError(f"no score for {event_id}")


def rank_events(events: Sequence[Event], base: Model, head: Model) -> Ranking:
    """Order the delta by risk and partition it into independent changes.

    Ties break on the event id, which is content-derived (`events.py`) and therefore stable
    across runs. Sorting on the score alone would leave the order of equally-risky events --
    the common case, since novelty and exposure are drawn from small tables -- decided by
    whatever order the diff stage happened to emit them in.
    """
    ctx = ScoringContext.of(base, head)
    scored = [score_event(event, ctx) for event in events]

    scored.sort(key=lambda s: (-s.total, s.event_id))
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
