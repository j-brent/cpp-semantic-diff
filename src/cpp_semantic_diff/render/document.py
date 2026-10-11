"""The versioned JSON document: ARCH-001 §10's output contract, as types.

**JSON is the product; the terminal is a consumer.** Every view -- the terminal view in
`terminal.py`, and the HTML, PR annotation and editor plugin §10 anticipates -- reads this
and nothing else. That is what keeps delivery surfaces out of the analysis core, which
§10 names as the mistake that made CodeAtlas TypeScript. The practical test is stated in
`terminal.py` and worth repeating here: **if a view needs a number this document does not
carry, the fix is to put it here**, never to hand the view a `Model`.

§10 gives seven top-level keys and this module gives those seven, plus `scores` and
`components`. Where §10 is a sketch rather than a spec, the choice made is
recorded on the field:

- **`ranking`** is `[event id]` verbatim, so a consumer that only wants reading order gets
  it without parsing scores. But a view that must *explain* the order needs the factors,
  and recomputing them in the view would put analysis back in the presentation layer. So
  `scores` sits beside it, keyed by event id -- an addition to §10, not a reinterpretation
  of it.
- **`entities`** carries only the nodes events reference. §10 says "for events to
  reference"; a real model has thousands of nodes and a review touches tens of them, and a
  document that inlined the whole node set would be a model dump with a delta attached.
- **`completeness`** carries the systematic limits of §11 as data rather than as prose
  in one view, because §11 requires them stated "in the output itself, every time" -- and
  the output is this document, not the terminal.
- **`summary`** carries §10's four keys plus others, the tier-1 residue among them. ARCH-001
  §13 makes measuring rename damage a v0 deliverable, and a measurement that lives only in a
  log is not one.

Nothing here computes anything from a `Model`. `build.py` assembles this from the stage
outputs; this module is the shape and the invariants.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import cached_property

from pydantic import BaseModel, ConfigDict, Field

from cpp_semantic_diff.acquire import ThirdPartySource
from cpp_semantic_diff.model import (
    ChangeEvent,
    ChangeEventId,
    Completeness,
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
#:
#: 1.0 -> 1.1: the dropped-edge count moved out of `completeness.*.notes`, where it was an
#: English sentence, into `edges_total`/`edges_dropped`. The new fields are additive, but the
#: prose leaving `notes` is not: a 1.0 consumer that scraped it would now find nothing there
#: and report a model that lost a fifth of its edges as having lost none -- §11's failure
#: exactly, and the reason this is a bump rather than an addition. `EdgeAttrChanged` also
#: joins the event kinds in this version.
#:
#: 1.1 -> 1.2: `compression_ratio` and `high_risk_count` are taken over *first-party* events
#: rather than every modelled one, and `vendored_events` names what was set aside. A bump and
#: not an addition because both existing fields keep their names and change their meaning: a
#: 1.1 consumer would read the new ratio as the old one and see a repository's compression
#: apparently improve on precisely the commit where a dependency was bumped. `events_reported`
#: is untouched and still the complete set -- the one number that may never quietly shrink.
#:
#: 1.2 -> 2.0.dev: 2.0 is in development on `dev` for milestone v0. Every v0 change to the
#: output lands under it, so a consumer sees one bump for the milestone rather than one per
#: change. It becomes "2.0" when `dev` merges to `main`; until then the `.dev` suffix keeps a
#: document from a moving schema from passing for a finished one. So far it removes
#: `summary.vendored_events`: third-party code is not in the model (ADR-025), so every
#: change event is first-party and `compression_ratio` and `high_risk_count` are taken over
#: all of them.
SCHEMA_VERSION = "2.0.dev"

#: ARCH-001 §11: systematic limits "get stated in the output itself, every time". These are the
#: two that hold of any static model of one configuration; the third, what this producer cannot
#: see, comes from the models (`Completeness.not_visible`) and is appended by `build.py`.
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

    **The two kinds of incompleteness are carried separately, because they mean different
    things and warrant different reactions.**

    *Coverage* -- `tus_complete` -- is sources the producer could not read. Entities that
    exist read as absent, which is §11's failure exactly, so it drives the banner and it
    taints `Confidence.models_complete` on every event.

    *Boundary* -- `edges_dropped` of `edges_total` -- is relations whose endpoint no
    declaration in the repository owns: `std::`, libc, implicit special members. It is a
    sizeable share of a real model's edges on a completely healthy run, so it is **stated on
    every run and alarming on none**: an alarm that is always on is one nobody reads, and the
    reader who needs to know how much of the graph is missing needs the number, not an
    adjective. There is deliberately no threshold at which it becomes a warning -- a knob with
    no calibration data behind it would be a guess presented as a policy. The percentage is
    printed instead, so a residue of 90 % reads as 90 %.

    Both are numbers a consumer can quote. That is the point: this record used to carry the
    edge loss only as an English sentence in `notes`, which meant every view either parsed
    prose or silently claimed a completeness the model did not have.
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

    edges_total: int = Field(
        default=0,
        description="Distinct edges the builder considered on this side: kept plus dropped.",
    )
    edges_dropped: int = Field(
        default=0,
        description="Distinct edges dropped because an endpoint is not declared in this "
        "repository. The model's boundary, quantified -- see the class docstring.",
    )

    build_available: bool = Field(
        description="Whether binary artifacts existed. Configure-only in v0 (§3.1), so "
        "this is False on every run and the evidence layer (§8) has nothing to join to."
    )
    producer_version: str
    producers: list[str] = Field(default_factory=list)
    language_modes: list[str] = Field(
        default_factory=list,
        description="`-std=` values this side was parsed at, sorted. Empty means the build "
        "states none and the parser's default was used. Compare the two sides: a difference "
        "here explains additions and removals that no source change accounts for.",
    )

    tus_complete: bool = Field(
        description="Every attempted TU parsed. **This is a claim about parsing and about "
        "nothing else** -- see the class docstring. Never render it as 'the model is "
        "complete'."
    )
    failures: list[TuFailure] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list,
        description="`Completeness.notes` verbatim: what the builder had to say that has "
        "no field of its own. No longer the channel for the edge loss, which is two "
        "integers above.",
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
            edges_total=completeness.edges_total,
            edges_dropped=completeness.edges_dropped,
            build_available=completeness.build_available,
            producer_version=completeness.producer_version,
            producers=list(completeness.producers),
            language_modes=list(completeness.language_modes),
            tus_complete=completeness.is_complete,
            failures=[
                TuFailure(path=t.path, detail=t.detail)
                for t in completeness.tus
                if t.status is TuStatus.FAILED
            ],
            notes=list(completeness.notes),
        )

    @property
    def edges_closed(self) -> bool:
        """Whether the graph on this side is closed over its own nodes. `Completeness`
        owns the definition; this record only carries the counts it is computed from."""
        return self.edges_dropped == 0

    @property
    def edge_residue(self) -> float:
        return (self.edges_dropped / self.edges_total) if self.edges_total else 0.0

    @property
    def boundary(self) -> str | None:
        """The boundary sentence, or None when nothing was dropped (ARCH-001 §11).

        Rendered here rather than in a view for the same reason `summary.compression` is:
        every consumer must state the same fact in the same words, and a sentence a view
        composes is one a view can quietly soften. Not part of `warnings` -- it is stated
        whether or not anything is wrong.
        """
        if self.edges_closed:
            return None
        return (
            f"{self.edges_dropped:,} of {self.edges_total:,} distinct edges "
            f"({100.0 * self.edge_residue:.1f}%) lead outside the model: an endpoint is "
            f"declared nowhere in this repository (external declarations, implicit special "
            f"members). Expected, and a lower bound on the call graph, not a defect."
        )

    @property
    def warnings(self) -> list[str]:
        """What must appear above the events on this side (ARCH-001 §11, rule 2).

        Coverage only, plus whatever the builder put in `notes`. The boundary residue is
        *not* here: it is on every real run, and a caveat that fires every time trains the
        reader to skip the block that also carries the failed TUs. It is stated by
        `boundary` instead, unconditionally, which is a stronger guarantee than a warning
        the reader has learned to ignore.
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


class RenameDetection(BaseModel):
    """Whether git's rename map -- an input to identity (ARCH-001 §5) -- is complete.

    Not a property of either model, so it sits beside the per-side records rather than in
    one of them. When `diff.renameLimit` is exceeded git skips inexact rename detection and
    says so only on stderr: exact renames are still found, but a file renamed *and* edited
    arrives as a delete plus an add, and every internal-linkage entity in it as a removal
    plus an addition. What was missing is stated at the top (§11, rule 2), and per event the
    match stage records every unpaired entity in an added or deleted file as having a
    candidate (`Unpaired.CANDIDATE`), so none of them reads as a confident addition or
    removal (`match.unpaired.unpaired_reasons`).
    """

    model_config = ConfigDict(extra="forbid")

    complete: bool = Field(
        default=True,
        description="False when git skipped inexact rename detection because "
        "`diff.renameLimit` was exceeded. The limit is not lifted (`-l0` is potentially "
        "quadratic on the large diffs that hit it); it is reported instead.",
    )
    limit_needed: int | None = Field(
        default=None,
        description="The `diff.renameLimit` git said would have sufficed, when it said.",
    )

    @property
    def warning(self) -> str | None:
        if self.complete:
            return None
        advice = (
            f"; setting diff.renameLimit to at least {self.limit_needed:,} would find them"
            if self.limit_needed is not None
            else ""
        )
        return (
            "git skipped inexact rename detection (diff.renameLimit exceeded): only exact "
            "renames were applied, so a file renamed and edited reads as deleted and added, "
            f"and its file-scoped entities as removed and re-added{advice}"
        )


class UncoveredFile(BaseModel):
    """One side of one file's changed tokens that no change event explains."""

    model_config = ConfigDict(extra="forbid")

    side: Side
    path: str
    lines: list[int] = Field(description="The lines the uncovered tokens are on, ascending.")
    tokens: int
    modelled: bool = Field(
        default=True,
        description="Whether either model declares anything in this file. Checked by path "
        "against both models together, not against this side's model alone. False is roughly "
        "'no translation unit includes it'; approximate, because an included file that "
        "declares nothing the model records also reads False.",
    )


class ExcludedFile(BaseModel):
    """A changed C++ file whose tokens were not compared, and why."""

    model_config = ConfigDict(extra="forbid")

    path: str
    reason: str


class UncoveredReport(BaseModel):
    """Where the changes no change event explains are (ADR-042).

    The counts are in `summary`; this is the detail a reader needs to go and look. Not a
    warning and not part of `degraded`: it is stated on every run, like the boundary, because
    it is a standing measure of what the model sees rather than a sign that something failed.
    """

    model_config = ConfigDict(extra="forbid")

    files: list[UncoveredFile] = Field(default_factory=list)
    moved_tokens: int = Field(
        default=0,
        description="Changed tokens in blocks deleted in one place and inserted token for "
        "token in another. Textually identical, which is not the same as semantically "
        "unchanged; counted apart from uncovered tokens, not as explained.",
    )
    excluded_files: list[ExcludedFile] = Field(
        default_factory=list,
        description="Changed C++ files whose tokens were not compared, with the reason: "
        "'binary', or 'third-party' for a file in the third-party set (ADR-025).",
    )
    lines_in_unmodelled_files: int = Field(
        default=0,
        description="Of `summary.uncovered_lines`, those in files neither model declares "
        "anything in -- roughly, files no translation unit includes. Approximate, as "
        "`UncoveredFile.modelled` says.",
    )
    renames_complete: bool = Field(
        default=True,
        description="False when git skipped inexact rename detection for the text diff, so a "
        "file renamed and edited counts every token as changed. Detected separately from "
        "`completeness.renames`, because this diff covers only C++ files and can differ.",
    )


class CompletenessBlock(BaseModel):
    """§10's `completeness: { per-side completeness records }`, plus §11's standing limits."""

    model_config = ConfigDict(extra="forbid")

    base: CompletenessReport
    head: CompletenessReport
    renames: RenameDetection = Field(
        default_factory=RenameDetection,
        description="Whether git's rename map was complete. Shared by both sides.",
    )
    limits: list[str] = Field(
        default_factory=lambda: list(SYSTEMATIC_LIMITS),
        description="ARCH-001 §11's systematic limits, then what the producer cannot see "
        "(`Completeness.not_visible`, ADR-029), carried on every document so that every "
        "consumer states them rather than each remembering to.",
    )

    uncovered: UncoveredReport | None = Field(
        default=None,
        description="Where the changes no change event explains are. None when they were "
        "not measured, which is not the same as none.",
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
    def boundaries(self) -> list[tuple[Side, str]]:
        """Each side's boundary sentence, where it has one. Stated on every run.

        Separate from `warnings` on purpose: this is how much of the graph lies outside
        the repository, which is a property of what was analyzed rather than of how well
        it went. A view prints both blocks; only `warnings` decides whether the run is
        announced as degraded.
        """
        return [
            (side, sentence)
            for side, report in ((Side.BASE, self.base), (Side.HEAD, self.head))
            if (sentence := report.boundary) is not None
        ]

    @property
    def degraded(self) -> bool:
        """Whether §11 rule 2's "any output built on an incomplete model must say so" applies.

        **Coverage, not boundary.** `tus_complete` on both sides plus anything either
        builder recorded in `notes`. The dropped-edge residue is excluded because it is
        present on every real repository -- see `CompletenessReport` -- and is reported
        unconditionally through `boundaries` instead of intermittently through here.
        Reading it as degradation made every fmt and spdlog run announce itself as
        incomplete, which is how a §11 signal stops being read at all.

        A truncated rename map degrades the run too: it is identity input that is missing,
        and the events it distorts look like ordinary additions and removals.
        """
        return bool(self.warnings) or not self.renames.complete


class ThirdPartyPath(BaseModel):
    """One directory of the third-party set, and where it came from (ADR-025).

    `sources` lists both when a submodule was also declared: each is a separate reason the
    path is third-party, and a consumer should not have to guess which one applied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sources: list[ThirdPartySource]


class ThirdPartyHint(BaseModel):
    """A tracked directory named like a dependency that is not in the third-party set.

    A note and nothing more: it excludes nothing (ADR-025). Structured, so a consumer can act
    on `path` without parsing `message`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    message: str


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
        description="Which sides hold this entity, through the correspondence: either id of "
        "a match counts as both, since a function paired by name has a different id on each "
        "side (ADR-036). A one-sided entity is exactly what a "
        "pipeline without rename detection produces for a rename (STATUS §6), so the view "
        "can say 'gone' or 'new' without re-deriving it from the event kind."
    )


class Finding(BaseModel):
    """ARCH-001 §8's uniform adapter output: evidence attached to a node.

    Empty on every v0 document -- §13 defers the evidence layer. It is in the schema
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
    derive: float = 0.0
    classify: float = 0.0
    coverage: float = Field(
        default=0.0,
        description="Reading the C++ text diff as tokens and setting it against "
        "the change events (ADR-042).",
    )
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
    """What tier 1 could not pair -- the measurement ARCH-001 §13 calls a v0 deliverable.

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
    files_renamed: int = Field(
        default=0,
        description="Renames git resolved and the matcher was given (ARCH-001 §5). Reported "
        "because it is the one input to identity that comes from outside the models: a "
        "reviewer comparing two runs cannot otherwise tell whether a rename was seen and "
        "applied or missed entirely, and those produce very different deltas.",
    )

    changed_cpp_lines: int | None = Field(
        default=None,
        description="Lines in C++ files, headers included, carrying a changed token; a "
        "deleted line and its replacement count as two. Tokens, not text: a whitespace or "
        "comment edit changes none (ADR-042). None when not measured.",
    )
    uncovered_lines: int | None = Field(
        default=None,
        description="Of `changed_cpp_lines`, those carrying a changed token on no change "
        "event's lines. **A lower bound** on what the model cannot explain: an event's hunk "
        "covers whole lines, so an unexplained edit inside a changed entity's span is counted "
        "as explained. Reported beside the compression, so the ratio cannot improve by the "
        "model seeing less. Files in the third-party set are not counted (ADR-025).",
    )
    uncovered_tokens: int | None = Field(
        default=None,
        description="The changed tokens behind `uncovered_lines`, moved tokens excluded.",
    )

    events_reported: int = Field(
        description="`len(events)`. The complete set. Not the visible set, not the "
        "top-level set, not a filtered set."
    )
    compression: str = Field(
        description="§10's sentence, ready to print: '2,431 lines changed -> 9 semantic "
        "events, 2 high-risk'. Rendered here so every view prints the same claim."
    )
    compression_ratio: float | None = Field(
        description="lines_changed / events_reported. None when there are no change events, "
        "because 'infinite compression' and 'no change the model can see' are different "
        "facts, and a sentinel would let a consumer average over them."
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


# -- naming: telling apart two entities that share a qualified name ----------------------
#
# Identity is the USR (ARCH-001 §5), so an ambiguity here is only ever in the *label*. Leaving
# one in place has cost a misdiagnosis (v0 tracker §5, reports/2026-08-01--v0-build/STATUS.md):
# a rendered name that collapsed a partial specialization and its primary template into one
# string was read as a self-edge, filed as a producer defect, and sat in the defect list for
# weeks against a producer that was right.
#
# A *discriminator* is a pure function of one entity that returns the annotation to bracket
# after its name, or None when it has nothing to say. `entity_names` tries them in the order
# below and takes the first that yields a value for **every** member of an ambiguous group
# *and* yields a different one for each -- so a group is annotated uniformly, or not by that
# rule at all. Two properties follow, and both are the point:
#
# - **Stable.** Every discriminator reads fields of the entity itself. None reads iteration
#   order, insertion order or a position in a list, so two runs of the same comparison render
#   identically and so do two machines. The group is a *set* of entities sharing a name, and
#   the choice of rule is a function of that set's contents, never of how it was built.
# - **Repository-relative, never absolute.** `_by_location` leans on `Location.file` being
#   repo-relative. The worst identity bug this project has had was the extractor leaking a
#   worktree path -- and worktrees are named by SHA -- which made nodes differ on every
#   comparison. That shape must not come back through the label.


def _by_signature(node_id: NodeId, entity: EntitySummary) -> str | None:
    """The overload's parameter types. What the original rule used, and right for the case
    it was written for: two `log_msg_buffer::log_msg_buffer` constructors are two entities
    and the signature is exactly what distinguishes them. `None` on a record, which is why
    it cannot be the only rule."""
    return entity.signature


#: How clang spells a tag's template role into its USR, which is the one place the document
#: still carries the distinction: `extract.py` maps `CLASS_TEMPLATE` and
#: `CLASS_TEMPLATE_PARTIAL_SPECIALIZATION` to the *tag* kind, so `fmt::v12::detail::
#: is_named_arg` is `struct` on both sides of the `inherits` edge and `kind` cannot separate
#: them. `S`/`C`/`U` is a plain tag and says nothing on its own -- it is read only for the
#: template arguments that may follow it, which make it a specialization.
_USR_TEMPLATE_FORMS = {
    "ST": "primary template",
    "UT": "primary template",
    "SP": "partial specialization",
    "UP": "partial specialization",
}

#: The entity's own declaration is the *last* record in its USR, so this anchors at the end:
#: `@<marker><parameters>@<name>[<arguments>]`. Anchoring is what makes it safe to read a
#: USR here at all. A template argument can itself be a USR -- fmt's `native_formatter` has
#: `…@ST>3#T#T#N$@N@fmt@N@v12@N@detail@E@type@native_formatter` -- and a rule that took the
#: last marker it could find anywhere would report the *argument's* kind as the entity's.
#: `[^@]*` cannot cross into a nested USR, so such a spelling matches nothing and the rule
#: declines. Declining is a missed annotation; guessing would be a wrong one, and a wrong
#: name is the defect being fixed.
_USR_TAIL = r"@(ST|UT|SP|UP|S|C|U)(?:>[^@]*)?@{name}(>.*)?$"

_RECORD_KINDS = frozenset({NodeKind.CLASS, NodeKind.STRUCT, NodeKind.UNION})


def _by_template_role(node_id: NodeId, entity: EntitySummary) -> str | None:
    """Primary template, partial specialization or specialization, read off the USR.

    Preferred over the location because it answers the question the reader actually has.
    `is_named_arg -> is_named_arg` reads as a bug; two line numbers defuse that but leave
    the reader to open the header to find out what the two are. *`partial specialization`
    inherits from `primary template`* is the fact, it is what the producer modelled, and it
    is legible without leaving the page. It is also unaffected by the code moving.

    Restricted to records because the markers are tag markers: applied to a method or a
    function, the same shapes appear in the *enclosing* class's segment of the USR and would
    be reported as the member's own role.
    """
    if entity.kind not in _RECORD_KINDS or not entity.name:
        return None
    match = re.search(_USR_TAIL.format(name=re.escape(entity.name)), node_id, re.DOTALL)
    if match is None:
        return None
    marker, arguments = match.group(1), match.group(2)
    # A plain tag carrying template arguments is a specialization of something; a plain tag
    # without them is an ordinary record, which cannot share a scope with a template of the
    # same name and so has nothing to disambiguate against.
    return _USR_TEMPLATE_FORMS.get(marker) or ("specialization" if arguments else None)


def _by_location(node_id: NodeId, entity: EntitySummary) -> str | None:
    """Where it is declared: repository-relative path and line.

    Precise and always available, and the only thing that separates the case neither rule
    above can -- two internal-linkage entities with the same qualified name and the same
    signature in two different files, which ARCH-001 §5 names as the tier-1 hazard. It ranks
    below the template role rather than above it because it is noisy, because it changes when
    code moves, and because `terminal.py` already prints the location after a node subject:
    promoting it would print the same string twice on the same line.
    """
    if entity.location is None:
        return None
    return f"{entity.location.file}:{entity.location.line}"


def _by_id(node_id: NodeId, entity: EntitySummary) -> str | None:
    """The USR. Exact, ugly, and guaranteed to work, because it is the key this entity is
    stored under and `entities` is a dict. Unreadable as a name -- but it is the string that
    takes the reader to the same entity in the JSON, which is the first move on anything
    surprising seen in the terminal: the view trades identities for names, on purpose."""
    return node_id


#: Tried in order, most legible first. `_by_id` terminates the search on every possible
#: input, so the ambiguous fallback in `entity_names` is unreachable rather than merely
#: unlikely -- see the comment there for why it is written anyway.
_DISCRIMINATORS: tuple[Callable[[NodeId, EntitySummary], str | None], ...] = (
    _by_signature,
    _by_template_role,
    _by_location,
    _by_id,
)


class DiffDocument(BaseModel):
    """ARCH-001 §10's document. The product.

    The invariant that outranks every other property of this type: **`events` is complete and
    uncollapsed**. `caused_by` is a link and never a deletion (§6.3), so a view may nest but
    the document always carries every event the diff produced. A view that hides a consequence
    is a choice a reader can undo; a document that never carried it is a change nobody can
    recover.
    """

    model_config = ConfigDict(extra="forbid")

    meta: Meta
    completeness: CompletenessBlock
    third_party: list[ThirdPartyPath] = Field(
        default_factory=list,
        description="The third-party set: submodules at either revision and `--third-party` "
        "paths. v0 analyzes only code the repository owns (ADR-025).",
    )
    third_party_hints: list[ThirdPartyHint] = Field(
        default_factory=list,
        description="Tracked directories whose name suggests a dependency and that are not in "
        "`third_party`. Hints only: folder names never exclude anything.",
    )
    entities: dict[NodeId, EntitySummary] = Field(default_factory=dict)
    events: list[ChangeEvent] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    ranking: list[ChangeEventId] = Field(
        default_factory=list,
        description="§10 verbatim: event ids, highest risk first. Reading order.",
    )
    scores: dict[ChangeEventId, RiskScore] = Field(
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

    def event(self, event_id: ChangeEventId) -> ChangeEvent:
        return self.by_id[event_id]

    @property
    def by_id(self) -> dict[ChangeEventId, ChangeEvent]:
        # Recomputed per access rather than cached: `DiffDocument` is not frozen, and a cached
        # index on a mutable model is the kind of staleness this project spends its whole
        # design budget avoiding. Documents are built once and read tens of times; the cost is
        # a dict comprehension over a list a reviewer could read.
        return {e.id: e for e in self.events}

    def ranked(self) -> list[ChangeEvent]:
        """Events in reading order (ARCH-001 §7). Anything unranked follows, in document
        order -- an event omitted from `ranking` is a defect, but dropping it here would
        turn a broken ranking into a missing change."""
        index = self.by_id
        seen = set(self.ranking)
        return [
            *(index[i] for i in self.ranking if i in index),
            *(e for e in self.events if e.id not in seen),
        ]

    def score(self, event_id: ChangeEventId) -> RiskScore | None:
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

        Sharing a qualified name is ordinary C++ -- two `log_msg_buffer::log_msg_buffer`
        constructors are two entities, and so are `is_named_arg` and the partial
        specialization of it -- and a view that printed the bare name would show the same
        line twice and read as a bug in the tool rather than as two real changes. Identity
        is the USR (§5), so the ambiguity is only ever in the *label*; the fix is a
        labelling one, and it appends something that *means* the difference rather than an
        index that means nothing.

        The rules, and the order they are tried in, are above this class. This method only
        applies them: for each group of entities sharing a name, the first rule that has
        something to say about every member and says something different about each wins,
        and annotates the whole group.

        Bracketed rather than concatenated, which the original rule got right and is worth
        keeping for every rule: `Node.signature` is a whole function type
        (`void (const log_msg &)`), so juxtaposing it against the name reads as a malformed
        declaration rather than as a disambiguator. Everything appended here reads as an
        annotation about the entity, never as C++.

        Cached because it is O(entities) and the view asks per event; safe to cache for the
        same reason `Correspondence.by_base` is -- `entities` is written once by `build.py`
        and read thereafter.
        """
        by_name: dict[str, list[NodeId]] = {}
        for node_id, entity in self.entities.items():
            by_name.setdefault(self._plain_name(entity), []).append(node_id)

        out: dict[NodeId, str] = {}
        for name, ids in by_name.items():
            if len(ids) == 1:
                out[ids[0]] = name
                continue
            for discriminator in _DISCRIMINATORS:
                marks = [discriminator(i, self.entities[i]) for i in ids]
                if None in marks or len(set(marks)) != len(marks):
                    continue
                out.update({i: f"{name} [{m}]" for i, m in zip(ids, marks, strict=True)})
                break
            else:
                # Unreachable: `_by_id` returns the key each entity is stored under, and a dict
                # cannot hold two. Written anyway because the alternative is a label that is
                # ambiguous while the code believes it disambiguated -- the exact failure this
                # method exists to prevent -- and a defect that announces itself in the output
                # costs one reader a confusing line, where a silent one once cost a bug report
                # filed against the wrong stage.
                out.update({i: f"{name} [ambiguous: {i}]" for i in ids})
        return out

    @staticmethod
    def _plain_name(entity: EntitySummary) -> str:
        # The global namespace is a node with no name and is the container of every
        # file-scope entity, so its `contains` edges would otherwise render with a leading
        # blank the reader has to guess at.
        return entity.qualified_name or entity.name or "(global scope)"
