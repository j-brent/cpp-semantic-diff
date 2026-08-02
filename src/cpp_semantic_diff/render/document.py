"""The versioned JSON document: ARCH-001 §10's output contract, as types.

**JSON is the product; the terminal is a consumer.** Every view -- the terminal view in
`terminal.py`, and the HTML, PR annotation and editor plugin §10 anticipates -- reads this
and nothing else. That is what keeps delivery surfaces out of the analysis core, which
§10 names as the mistake that made CodeAtlas TypeScript. The practical test is stated in
`terminal.py` and worth repeating here: **if a view needs a number this document does not
carry, the fix is to put it here**, never to hand the view a `Model`.

§10 gives seven top-level keys and this module gives exactly those seven. Where §10 is a
sketch rather than a spec, the choice made is recorded on the field:

- **`ranking`** is `[event id]` verbatim, so a consumer that only wants reading order gets
  it without parsing scores. But a view that must *explain* the order needs the factors,
  and recomputing them in the view would put analysis back in the presentation layer. So
  `scores` sits beside it, keyed by event id -- an addition to §10, not a reinterpretation
  of it.
- **`entities`** carries only the nodes events reference. §10 says "for events to
  reference"; fmt's model has 10,574 nodes and a review touches tens of them, and a
  document that inlined the whole node set would be a model dump with a delta attached.
- **`completeness`** carries the two systematic limits of §11 as data rather than as prose
  in one view, because §11 requires them stated "in the output itself, every time" -- and
  the output is this document, not the terminal.
- **`summary`** carries §10's four keys plus the tier-1 residue. STATUS §6 makes measuring
  rename damage a v0 deliverable, and a measurement that lives only in a log is not one.

Nothing here computes anything from a `Model`. `build.py` assembles this from the stage
outputs; this module is the shape and the invariants.
"""

from __future__ import annotations

from functools import cached_property

from pydantic import BaseModel, ConfigDict, Field

from cpp_semantic_diff.model import (
    Completeness,
    Event,
    EventId,
    Location,
    MatchTier,
    NodeId,
    NodeKind,
    Side,
)
from cpp_semantic_diff.rank import Component, RiskScore

#: Bumped when a consumer that understood the previous version could misread this one.
#: Additive fields do not bump it; a changed meaning, a removed field or a changed
#: unit does. A document whose schema version a consumer does not recognise must be
#: refused rather than read optimistically -- that is the whole reason the field exists.
SCHEMA_VERSION = "1.0"

#: ARCH-001 §11: "Two systematic limits get stated in the output itself, every time."
#: They are data on the document rather than a line in one view, because "every time"
#: means every consumer, and a limit that lives in `terminal.py` is absent from the JSON
#: that §10 calls the product. Phrased as claims about *this* output, not as general
#: caveats, so that a reader who skips them has skipped something specific.
SYSTEMATIC_LIMITS: tuple[str, ...] = (
    "One model is one preprocessor configuration. Every claim here is conditional on the "
    "configuration that was analyzed; variant coverage is unsolved and is not implied.",
    "Static call graphs are unsound. Virtual dispatch, function pointers and inlining mean "
    "any reachability or blast-radius number is a lower bound, not a fact.",
)


class TuFailure(BaseModel):
    """One translation unit that did not parse, with the reason (ARCH-001 §4.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    detail: str | None = None


class CompletenessReport(BaseModel):
    """One side's `Completeness` (ARCH-001 §4.4), flattened for a consumer.

    **`tus_complete` is named for what it counts, and it counts one thing.** The IR's
    `Completeness.is_complete` is true when every attempted TU parsed -- and nothing else.
    On real fmt the builder drops ~5,113 of 28,470 edges per side because an endpoint is
    not declared in the repository, and `is_complete` is still `True`; the only record of
    that loss is prose in `Completeness.notes`. So a banner driven by `tus_complete` alone
    would print "complete" over a model that lost a fifth of its edges.

    `notes` is therefore not decoration and is not a log. It is the only channel carrying
    that fact, so it is surfaced verbatim, at the top, by every view -- see `warnings`.
    """

    model_config = ConfigDict(extra="forbid")

    side: Side
    rev: str

    tus_total: int
    tus_parsed: int
    tus_failed: int
    tus_cached: int = Field(
        description="TUs served from the per-TU cache (ARCH-001 §9). Not a completeness "
        "fact -- a cached partial is as complete as the parse that produced it -- but it "
        "is what makes a fast run legible instead of suspicious."
    )

    build_available: bool = Field(
        description="Whether binary artifacts existed. Configure-only in v0 (§3.1), so "
        "this is False on every run and the evidence layer (§8) has nothing to join to."
    )
    producer_version: str
    producers: list[str] = Field(default_factory=list)

    tus_complete: bool = Field(
        description="Every attempted TU parsed. **This is a claim about parsing and about "
        "nothing else** -- see the class docstring. Never render it as 'the model is "
        "complete'."
    )
    failures: list[TuFailure] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list,
        description="`Completeness.notes` verbatim. Carries the unresolved-edge counts, "
        "which no boolean on this record covers.",
    )

    @classmethod
    def of(cls, side: Side, rev: str, completeness: Completeness) -> CompletenessReport:
        from cpp_semantic_diff.model import TuStatus

        return cls(
            side=side,
            rev=rev,
            tus_total=completeness.tus_total,
            tus_parsed=completeness.parsed,
            tus_failed=completeness.failed,
            tus_cached=sum(1 for t in completeness.tus if t.cache_hit),
            build_available=completeness.build_available,
            producer_version=completeness.producer_version,
            producers=list(completeness.producers),
            tus_complete=completeness.is_complete,
            failures=[
                TuFailure(path=t.path, detail=t.detail)
                for t in completeness.tus
                if t.status is TuStatus.FAILED
            ],
            notes=list(completeness.notes),
        )

    @property
    def warnings(self) -> list[str]:
        """What must appear above the events on this side (ARCH-001 §11).

        Both sources, never one: the parse shortfall that `tus_complete` sees, and the
        notes that carry everything it does not.
        """
        out: list[str] = []
        if self.tus_total == 0:
            out.append("no translation units were analyzed")
        elif not self.tus_complete:
            pct = 100.0 * self.tus_failed / self.tus_total
            out.append(
                f"{self.tus_failed} of {self.tus_total} translation units failed to parse "
                f"({pct:.1f}%); entities declared only in them are missing from this side"
            )
        out.extend(self.notes)
        return out


class CompletenessBlock(BaseModel):
    """§10's `completeness: { per-side completeness records }`, plus §11's standing limits."""

    model_config = ConfigDict(extra="forbid")

    base: CompletenessReport
    head: CompletenessReport
    limits: list[str] = Field(
        default_factory=lambda: list(SYSTEMATIC_LIMITS),
        description="ARCH-001 §11's two systematic limits, carried on every document so "
        "that every consumer states them rather than each remembering to.",
    )

    @property
    def warnings(self) -> list[tuple[Side, str]]:
        """Every side-specific caveat, base first. Empty means both sides parsed clean
        *and* neither builder had anything to report -- which is rarer than it sounds."""
        return [
            *((Side.BASE, w) for w in self.base.warnings),
            *((Side.HEAD, w) for w in self.head.warnings),
        ]

    @property
    def degraded(self) -> bool:
        """Whether §11's "any output built on an incomplete model must say so" applies.

        Deliberately **not** `base.tus_complete and head.tus_complete`. That boolean counts
        parse success only, and the dropped-edge loss it does not see arrives as a note.
        """
        return bool(self.warnings)


class EntitySummary(BaseModel):
    """§10's `entities: { node id -> node summary }` -- enough to name an event's subject.

    A summary, not a `Node`: attributes and body hashes are diffable state that the events
    already carry the changes to, and repeating them here would give a consumer two places
    to read the same fact from and two chances to read the stale one.
    """

    model_config = ConfigDict(extra="forbid")

    id: NodeId
    kind: NodeKind
    name: str
    qualified_name: str
    signature: str | None = None
    symbol: str | None = Field(
        default=None,
        description="The evidence-layer join key (ARCH-001 §8). Carried in v0 with no "
        "findings to join to, for the same reason the producer populates it.",
    )
    location: Location | None = None
    sides: list[Side] = Field(
        description="Which model(s) contain this id. A one-sided entity is exactly what a "
        "tier-1-only pipeline produces for a rename (STATUS §6), so the view can say "
        "'gone' or 'new' without re-deriving it from the event kind."
    )


class Finding(BaseModel):
    """ARCH-001 §8's uniform adapter output: evidence attached to a node.

    Empty on every v0 document -- §12 defers the evidence layer. It is in the schema
    because §10 puts it there and because the join key ships in v0: a consumer written
    against a document with no `findings` key would need changing rather than extending
    the day the first adapter lands.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    kind: str
    severity: str
    detail: str
    source_tool: str


class Timings(BaseModel):
    """§10's `timings`. Seconds, per pipeline stage (ARCH-001 §3).

    The one part of this document that is not a pure function of the two commits, which is
    why it is quarantined in `meta` and why nothing else may read it. ARCH-001 §9 makes the
    30 s warm budget a design constraint "measured continuously rather than asserted", and
    a per-stage breakdown is what makes a missed budget diagnosable.
    """

    model_config = ConfigDict(extra="forbid")

    acquire: float = 0.0
    produce_base: float = 0.0
    produce_head: float = 0.0
    match: float = 0.0
    diff: float = 0.0
    classify: float = 0.0
    rank: float = 0.0
    total: float = 0.0


class Meta(BaseModel):
    """§10's `meta: { tool version, schema version, revs, timings }`.

    No wall-clock timestamp, deliberately. §10 names four things and a timestamp is not one
    of them, and adding one would make two documents for the same two commits differ --
    which would break the cheapest regression test this pipeline has (`diff` the JSON) for
    no reviewer-visible gain. Timings are the only non-reproducible field and are labelled.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = "cpp-semantic-diff"
    tool_version: str
    schema_version: str = SCHEMA_VERSION

    repo: str
    base_ref: str
    head_ref: str
    base_rev: str = Field(description="Resolved SHA of the merge base, never a ref name.")
    head_rev: str

    timings: Timings = Field(default_factory=Timings)


class Residue(BaseModel):
    """What tier 1 could not pair -- the measurement STATUS §6 calls a v0 deliverable.

    v0 ships tier-1 identity only, so a renamed function is a removal plus an addition and
    the tool looks worse than it is. That is deliberate: the damage is supposed to be
    *measured* before tiers 2-4 are built. A measurement that appears only as an inflated
    event count is not a measurement, so the counts are stated as their own fact, with the
    tiers that ran, so a reader can tell an honest limitation from a bug.
    """

    model_config = ConfigDict(extra="forbid")

    matched: int
    unmatched_base: int = Field(description="Base nodes no tier claimed: every removal.")
    unmatched_head: int = Field(description="Head nodes no tier claimed: every addition.")
    internal_linkage_residue: int = Field(
        description="Residue whose USR is file-scoped (ARCH-001 §5). Non-zero on a review "
        "that renamed no files means the tier-1 key is wrong, not that code changed."
    )
    tiers_run: list[MatchTier] = Field(
        description="Which tiers ran. `unmatched` means something different for each "
        "possible value of this, so a consumer that reports the counts must report this."
    )

    @property
    def pairable_residue(self) -> int:
        """The most tiers 2-4 could remove: pairs, so the smaller side bounds it.

        An upper bound and named as one. Every base residue node that a later tier paired
        with a head residue node collapses two events into one; nothing else can.
        """
        return min(self.unmatched_base, self.unmatched_head)


class Summary(BaseModel):
    """§10's `summary`, and the sentence the whole project is pointed at.

    **`compression` is computed honestly or not at all.** `events_reported` is the length
    of `events` -- the complete, uncollapsed set, before any view hides anything. §10 is
    blunt about why: if the ratio is not dramatic on real commits then RESEARCH-002's
    thesis is wrong, and the design should surface that in week one rather than month six.
    A numerator improved by filtering, bucketing or suppressing would destroy the only
    experiment this tool runs on itself.

    `top_level_events` is reported *beside* it and is never the numerator. It is a property
    of one view's collapse policy (§6.3), not of the delta.
    """

    model_config = ConfigDict(extra="forbid")

    lines_changed: int = Field(
        description="Added + deleted lines from the text diff -- `acquire.diff_line_count`, "
        "the same number `cppsd plan` reports. The denominator, and it is measured rather "
        "than derived so that it is the number a reviewer would otherwise have read."
    )
    files_changed: int
    cpp_files_changed: int

    events_reported: int = Field(
        description="`len(events)`. The complete set. Not the visible set, not the "
        "top-level set, not a filtered set."
    )
    compression: str = Field(
        description="§10's sentence, ready to print: '2,431 lines changed -> 9 semantic "
        "events, 2 high-risk'. Rendered here so every view prints the same claim."
    )
    compression_ratio: float | None = Field(
        description="lines_changed / events_reported. None when there are no events, "
        "because 'infinite compression' and 'nothing to review' are different facts and a "
        "sentinel would let a consumer average over them."
    )
    high_risk_count: int = Field(description="Scores at or above `rank.HIGH_RISK`.")

    top_level_events: int = Field(
        description="Events with no `caused_by` -- what a collapsing view shows at depth 0. "
        "A view statistic, reported for honesty about what the terminal does, and never "
        "the compression numerator."
    )
    components: int = Field(
        description="Independently reviewable units (ARCH-001 §7). RESEARCH-002 §4: 'these "
        "2,000 lines are one mechanical rename; these 40 are the actual change'."
    )

    residue: Residue


class DiffDocument(BaseModel):
    """ARCH-001 §10's document. The product.

    The invariant that outranks every other property of this type: **`events` is complete
    and uncollapsed**. `caused_by` is a link and never a deletion (§6.3, STATUS §6), so a
    view may nest but the document always carries every event the diff produced. A view
    that hides a consequence is a choice a reader can undo; a document that never carried
    it is a change nobody can recover.
    """

    model_config = ConfigDict(extra="forbid")

    meta: Meta
    completeness: CompletenessBlock
    entities: dict[NodeId, EntitySummary] = Field(default_factory=dict)
    events: list[Event] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    ranking: list[EventId] = Field(
        default_factory=list,
        description="§10 verbatim: event ids, highest risk first. Reading order.",
    )
    scores: dict[EventId, RiskScore] = Field(
        default_factory=dict,
        description="Beside `ranking`, not instead of it. `RiskScore.factors` carries the "
        "per-factor `detail` that lets a view say *why* something is first without "
        "recomputing a score -- which is the difference between a view and a second "
        "implementation of the rank stage.",
    )
    components: list[Component] = Field(
        default_factory=list,
        description="The §7 partition, numbered by descending risk. Component 0 is what to "
        "read first.",
    )
    summary: Summary

    # -- serialization -------------------------------------------------------------------

    def to_json(self, *, indent: int | None = 2) -> str:
        """The product. Round-trips through `from_json` without loss, which is the property
        that makes "JSON is the product" true rather than aspirational: a view reading a
        document off disk must get exactly what the pipeline handed the view in-process."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, raw: str | bytes) -> DiffDocument:
        return cls.model_validate_json(raw)

    # -- accessors a view needs, so no view re-derives them ------------------------------

    def event(self, event_id: EventId) -> Event:
        return self.by_id[event_id]

    @property
    def by_id(self) -> dict[EventId, Event]:
        # Recomputed per access rather than cached: `DiffDocument` is not frozen (pydantic
        # would have to deep-freeze `Event`, which classify copies), and a cached index on a
        # mutable model is the kind of staleness this project spends its whole design
        # budget avoiding. Documents are built once and read tens of times; the cost is a
        # dict comprehension over a list a reviewer could read.
        return {e.id: e for e in self.events}

    def ranked(self) -> list[Event]:
        """Events in reading order (ARCH-001 §7). Anything unranked follows, in document
        order -- an event omitted from `ranking` is a defect, but dropping it here would
        turn a broken ranking into a missing change."""
        index = self.by_id
        seen = set(self.ranking)
        return [
            *(index[i] for i in self.ranking if i in index),
            *(e for e in self.events if e.id not in seen),
        ]

    def score(self, event_id: EventId) -> RiskScore | None:
        return self.scores.get(event_id)

    def label(self, subject: str) -> str:
        """A human-readable name for an event's subject, from `entities` alone.

        Falls back to the raw id rather than to a `Model` lookup. A view that could reach a
        model would eventually read one, and §10's separation would be a convention rather
        than a fact.

        The empty qualified name is a real case, not a defensive one: the global namespace
        is a node with no name, and it is the container of every file-scope entity, so
        every `contains` edge out of it would otherwise render as `" -> f"` -- a leading
        blank a reader has to guess the meaning of. Naming it is the honest rendering.
        """
        from cpp_semantic_diff.rank import subject_endpoints

        parts = subject_endpoints(subject)  # type: ignore[arg-type]
        return " -> ".join(self.entity_name(p) for p in parts)

    def entity_name(self, node_id: NodeId) -> str:
        return self.entity_names.get(node_id, node_id)

    @cached_property
    def entity_names(self) -> dict[NodeId, str]:
        """Display names for every entity, disambiguated where a name is not unique.

        C++ overloads share a qualified name -- two `log_msg_buffer::log_msg_buffer`
        constructors are two entities -- and a view that printed the bare name would show
        the same line twice and read as a bug in the tool rather than as two real changes.
        Identity is the USR (§5), so the ambiguity is only ever in the *label*; the fix is
        therefore a labelling one, and it appends the signature that made them distinct
        rather than an index that means nothing.

        Cached because it is O(entities) and the view asks per event; safe to cache for the
        same reason `Correspondence.by_base` is -- `entities` is written once by `build.py`
        and read thereafter.
        """
        by_name: dict[str, list[NodeId]] = {}
        for node_id, entity in self.entities.items():
            by_name.setdefault(self._plain_name(entity), []).append(node_id)

        out: dict[NodeId, str] = {}
        for name, ids in by_name.items():
            for node_id in ids:
                signature = self.entities[node_id].signature
                # Bracketed rather than concatenated: `Node.signature` is a whole function
                # type (`void (const log_msg &)`), so juxtaposing it against the name reads
                # as a malformed declaration rather than as a disambiguator.
                out[node_id] = f"{name} [{signature}]" if len(ids) > 1 and signature else name
        return out

    @staticmethod
    def _plain_name(entity: EntitySummary) -> str:
        # The global namespace is a node with no name and is the container of every
        # file-scope entity, so its `contains` edges would otherwise render with a leading
        # blank the reader has to guess at.
        return entity.qualified_name or entity.name or "(global scope)"
