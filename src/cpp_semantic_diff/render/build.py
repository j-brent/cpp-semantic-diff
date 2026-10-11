"""Stage outputs in, one `DiffDocument` out. ARCH-001 §10.

The last stage of the pipeline (§3), and a pure function of what the earlier ones produced:
two models, a correspondence, an event list, a ranking, and the text-diff denominator that
`acquire` measured. It parses nothing, walks no graph and computes no score. Everything it
adds is *assembly* -- selecting the entities the events reference, flattening two
`Completeness` records, and rendering §10's one sentence.

Two things it deliberately does not do.

**It does not collapse.** `caused_by` arrives set and leaves set, and `events` is the whole
list in the order the diff stage emitted it. Collapse is a view concern (§6.3, `collapse.py`)
and this document is what the views agree on.

**It does not improve the numerator.** `summary.events_reported` is `len(events)`, and the
numerator is its first-party part (schema 1.2; `compression_sentence` says why). §10 makes
the compression ratio the experiment this project runs on itself, and there is no filter,
bucket or threshold here that could flatter it.
"""

from __future__ import annotations

from collections.abc import Sequence

from cpp_semantic_diff import __version__
from cpp_semantic_diff.acquire import RenameLimitHit, RevPair
from cpp_semantic_diff.match import Correspondence
from cpp_semantic_diff.model import ChangeEvent, Model, Node, NodeId, Side
from cpp_semantic_diff.rank import Ranking, subject_endpoints
from cpp_semantic_diff.render.document import (
    CompletenessBlock,
    CompletenessReport,
    DiffDocument,
    EntitySummary,
    Meta,
    RenameDetection,
    Residue,
    Summary,
    Timings,
)


def build_document(
    *,
    repo: str,
    revs: RevPair,
    base: Model,
    head: Model,
    correspondence: Correspondence,
    events: Sequence[ChangeEvent],
    ranking: Ranking,
    lines_changed: int,
    files_changed: int,
    cpp_files_changed: int,
    files_renamed: int = 0,
    rename_limit: RenameLimitHit | None = None,
    timings: Timings | None = None,
) -> DiffDocument:
    """Assemble ARCH-001 §10's document from the stage outputs.

    Everything is passed in. Nothing is recomputed here that a stage already decided --
    notably the ranking order and the high-risk count, which come off `Ranking` rather than
    being re-derived from the scores, because two implementations of one threshold is one
    too many.
    """
    entities = _entities(events, base, head)
    completeness = CompletenessBlock(
        base=CompletenessReport.of(Side.BASE, base.rev, base.completeness),
        head=CompletenessReport.of(Side.HEAD, head.rev, head.completeness),
        renames=RenameDetection(
            complete=rename_limit is None,
            limit_needed=rename_limit.needed if rename_limit is not None else None,
        ),
    )

    high_risk = ranking.high_risk_count
    count = len(events)
    first_party = ranking.first_party_count

    return DiffDocument(
        meta=Meta(
            tool_version=__version__,
            repo=repo,
            base_ref=revs.base_ref,
            head_ref=revs.head_ref,
            base_rev=revs.base_sha,
            head_rev=revs.head_sha,
            timings=timings or Timings(),
        ),
        completeness=completeness,
        entities=entities,
        # The complete, uncollapsed set (§6.3). Copied into a list rather than aliased:
        # a document that shared its list with the caller's would let a later mutation
        # rewrite an already-serialized product.
        events=list(events),
        findings=[],  # §8 is deferred (§13); the key exists so consumers do not change.
        ranking=ranking.order,
        scores={s.event_id: s for s in ranking.scores},
        components=list(ranking.components),
        summary=Summary(
            lines_changed=lines_changed,
            files_changed=files_changed,
            cpp_files_changed=cpp_files_changed,
            files_renamed=files_renamed,
            events_reported=count,
            vendored_events=ranking.vendored_count,
            compression=compression_sentence(
                lines_changed, first_party, high_risk, ranking.vendored_count
            ),
            compression_ratio=(lines_changed / first_party) if first_party else None,
            high_risk_count=high_risk,
            top_level_events=sum(1 for e in events if e.caused_by is None),
            components=len(ranking.components),
            residue=Residue(
                matched=len(correspondence.matches),
                unmatched_base=len(correspondence.unmatched_base),
                unmatched_head=len(correspondence.unmatched_head),
                internal_linkage_residue=correspondence.internal_linkage_residue,
                tiers_run=list(correspondence.tiers_run),
            ),
        ),
    )


def compression_sentence(lines_changed: int, events: int, high_risk: int, vendored: int = 0) -> str:
    """§10's line, verbatim in shape: *2,431 lines changed -> 9 change events, 2 high-risk*.

    Built here rather than in a view so that every consumer prints the same claim, and so
    that the claim is in the JSON -- §10 says it "prints on every run", and a sentence a
    view composes is one a view can quietly change.

    The degenerate cases are spelled out rather than divided through. Zero lines changed
    with events is a real and interesting state (a submodule bump, a whitespace-only commit
    that moved a `#define`), and zero events on a large diff is the single most important
    result this tool can report -- it means the change had no semantic content the model can
    see, which is either excellent news or a hole in the model.

    **`events` here is the first-party count, and the two sides must match.** The denominator
    is lines of text diff -- "the number a reviewer would otherwise have read" -- and git
    renders a submodule bump as two lines, not the hundred thousand inside it. Dividing that
    by every modelled event compares a first-party denominator against a whole-world
    numerator, which on leveldb read 0.0 lines per event. So vendored events are named beside
    the sentence and excluded from the arithmetic, and the fix is a real cost rather than a
    flattering one: aligning the universes made leveldb's ratio finite but still inverted,
    which is the honest result.

    `summary.events_reported` is untouched and remains the complete set. It is the one figure
    nobody can audit from outside, and it may not shrink because a dependency moved.
    """
    head = f"{lines_changed:,} lines changed -> {events:,} change event{'' if events == 1 else 's'}"
    if not events:
        if vendored:
            # Distinct from "nothing changed": a dependency moved and none of it reached
            # the reviewer's own code, which is a result rather than an absence of one.
            return f"{head} (all {vendored:,} of it in bundled dependencies)"
        return f"{head} (no semantic change the model can see)"
    tail = f", {high_risk:,} high-risk"
    if vendored:
        tail += f" · {vendored:,} more in bundled dependencies, ranked last"
    return f"{head}{tail}"


def _entities(
    events: Sequence[ChangeEvent], base: Model, head: Model
) -> dict[NodeId, EntitySummary]:
    """The node summaries the events reference, and only those (ARCH-001 §10).

    An event's subject is a node id or an edge id (§6.2); `subject_endpoints` resolves both,
    so an `EdgeRemoved` contributes summaries for the caller *and* the callee -- without
    which a view could name the edge but neither end of it.

    **Head wins where an id is on both sides.** The subject of every matched-node event is
    the head id (`diff/nodes.py`), so head is what the reader is looking at; the base copy
    is carried only through `sides`, which is what lets a view say "removed" without
    inspecting the event kind. Removals are base-only ids and are found in `base` alone,
    which is the reason both models are consulted rather than head only.
    """
    wanted: set[NodeId] = set()
    for event in events:
        wanted.update(subject_endpoints(event.subject))
        # `NodeMoved` names its parents in the payload, and a parent may have no event of
        # its own. A view that cannot name the new container is reporting a move to
        # nowhere. Cheap to include, and the alternative surfaces as a raw USR.
        for key in ("old_parent", "new_parent"):
            value = event.payload.get(key)
            if isinstance(value, str):
                wanted.add(NodeId(value))

    out: dict[NodeId, EntitySummary] = {}
    for node_id in sorted(wanted):
        in_base = base.nodes.get(node_id)
        in_head = head.nodes.get(node_id)
        node = in_head or in_base
        if node is None:
            # An id neither model carries. Real, and not a defect to hide: the builder drops
            # edges whose endpoint no TU declared but their ids can still reach an event
            # through a subject. Omitting the summary is honest -- the view falls back to the
            # raw id, which is all that is actually known about it.
            continue
        out[node_id] = _summary(node, in_base is not None, in_head is not None)
    return out


def _summary(node: Node, in_base: bool, in_head: bool) -> EntitySummary:
    return EntitySummary(
        id=node.id,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        signature=node.signature,
        symbol=node.symbol,
        location=node.location,
        sides=[s for s, present in ((Side.BASE, in_base), (Side.HEAD, in_head)) if present],
    )
