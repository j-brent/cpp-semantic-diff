"""The terminal view: a consumer of the document, and nothing more. ARCH-001 §10.

**Every number on this page comes out of a `DiffDocument`.** No `Model`, no `Correspondence`,
no `Ranking`, no `Event` that is not already in `document.events`. The function signatures
enforce it -- `render()` takes a document and a handful of display flags -- and the reason is
§10's: the moment a view can reach a model, a number that "was easier to get from the model"
lands in the presentation layer, and the analysis core has a delivery surface inside it. That
is the mistake §10 names. **If this view needs a number the document does not carry, the fix
is to put it in the document.**

The page is ordered by what §11 and §10 say must be seen first:

1. **Completeness, at the top, never in a log** (§11.2). What was missing precedes what was
   found, because a reader who scrolls past the events has already formed a belief.
2. **The compression line** (§10). The product's own success metric, printed on every run.
3. **The events**, in ranking order (§7), grouped by component and with consequences nested
   under their causes (§6.3, `collapse.py`).
4. **The tier-1 residue** (STATUS §6). v0's deliberate rename damage, stated as a
   measurement rather than left to be inferred from an inflated event count.
5. **§11's two systematic limits**, which are true of every run and are printed on every one.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from cpp_semantic_diff.model import EventId, Side
from cpp_semantic_diff.render.collapse import Collapsed, CollapseNode, collapse
from cpp_semantic_diff.render.document import CompletenessBlock, DiffDocument

#: Top-level events shown before the view stops and says how many it stopped at. A page a
#: reviewer will not read is the problem the ranking exists to solve, so truncating is the
#: right default -- but truncation that does not announce itself is §11's failure in
#: miniature, so the count of what was withheld is always printed.
DEFAULT_LIMIT = 25


def render(
    document: DiffDocument,
    console: Console,
    *,
    collapse_cascades: bool = True,
    limit: int = DEFAULT_LIMIT,
) -> None:
    """Print the whole view. `limit=0` means no truncation."""
    tree = collapse(document, enabled=collapse_cascades)

    _header(document, console)
    _completeness(document, console)
    _compression(document, tree, console)
    _events(document, tree, console, limit=limit)
    _residue(document, console)
    _limits(document, console)


# -- 1 · what was analyzed ---------------------------------------------------------------


def _header(document: DiffDocument, console: Console) -> None:
    meta = document.meta
    console.print(
        f"[bold]{meta.repo}[/]  [dim]{meta.base_ref}...{meta.head_ref}[/]  "
        f"[dim]({meta.base_rev[:12]} -> {meta.head_rev[:12]})[/]"
    )


def _completeness(document: DiffDocument, console: Console) -> None:
    """ARCH-001 §11.2: "any output built on an incomplete model states what was missing, at
    the top, not in a log."

    Two blocks, because there are two kinds of missing and only one of them is news.

    **Boundary** -- how many distinct edges lead outside the repository -- prints on every
    run, degraded or not, from `CompletenessReport.edges_dropped` and `edges_total`. It is
    ~20 % of fmt's graph when everything works, so it is a standing fact about what the
    model can see. This view no longer reads it out of `Completeness.notes`: the number
    lives in the document (§10) and every consumer gets it as a number.

    **Coverage** -- `CompletenessBlock.degraded` -- is the yellow banner, and it fires only
    when something actually went wrong: a TU that would not parse, or a note the builder
    chose to record. Warnings print verbatim rather than summarized; a summary of a caveat
    is a smaller caveat.
    """
    block = document.completeness
    parsed = (
        f"base {block.base.tus_parsed}/{block.base.tus_total} TUs · "
        f"head {block.head.tus_parsed}/{block.head.tus_total} TUs · "
        f"{block.head.producer_version or 'unknown producer'}"
    )

    if not block.degraded:
        console.print(f"[dim]{parsed} · both sides parsed clean[/]")
        _boundary(block, console)
        return

    console.print()
    console.print("[bold yellow]this model is incomplete[/] [dim]— read what follows knowing it[/]")
    for side, warning in block.warnings:
        console.print(f"  [yellow]{side.value:4}[/] {warning}")
    for report in (block.base, block.head):
        # Named, not counted. A reviewer who knows which TU failed knows which part of the
        # tree to distrust; a reviewer told "3 failed" knows only to distrust all of it.
        for failure in report.failures[:5]:
            console.print(f"  [dim]{report.side.value:4} {failure.path}: {failure.detail}[/]")
        if len(report.failures) > 5:
            console.print(f"  [dim]{report.side.value:4} … {len(report.failures) - 5} more[/]")
    console.print(f"  [dim]{parsed}[/]")
    _boundary(block, console)


def _boundary(block: CompletenessBlock, console: Console) -> None:
    """Where the modelled world ends, in numbers, on every run (ARCH-001 §11).

    Dim rather than yellow: this is the standing scope of a static model of one repository,
    not a fault. The percentage is what makes it readable -- 5,113 dropped edges is routine
    against 26,071 and a catastrophe against 6,000, and only the ratio distinguishes them.
    """
    for side, sentence in block.boundaries:
        console.print(f"  [dim]{side.value:4} {sentence}[/]")


# -- 2 · the metric the project is pointed at --------------------------------------------


def _compression(document: DiffDocument, tree: Collapsed, console: Console) -> None:
    """ARCH-001 §10's sentence, and the honest arithmetic around it.

    The sentence is taken from `summary.compression` rather than rebuilt, so that the JSON
    and the terminal cannot disagree about the one claim the project is testing. The line
    below it separates the two counts that are easy to conflate: `events_reported` is the
    complete delta and is the numerator; the collapsed count is what this view chose to
    nest, and is never the numerator.
    """
    s = document.summary
    console.print()
    console.print(f"[bold]{s.compression}[/]")

    parts = []
    if s.compression_ratio is not None:
        parts.append(f"{s.compression_ratio:.1f} lines per event")
    parts.append(f"{len(tree.roots)} shown, {tree.hidden} nested under a cause")
    parts.append(f"{s.components} component{'' if s.components == 1 else 's'}")
    parts.append(f"{s.files_changed} files ({s.cpp_files_changed} C++)")
    if document.meta.timings.total:
        parts.append(f"{document.meta.timings.total:.1f}s")
    console.print(f"  [dim]{' · '.join(parts)}[/]")

    if s.vendored_events:
        # The events are ranked last, so on a dependency bump a top-N view simply stops
        # showing them -- and silence there is indistinguishable from "nothing changed".
        # A reviewer is reviewing their own code, but they are entitled to know how much
        # was set aside on their behalf and that it is in the JSON.
        share = s.vendored_events / s.events_reported if s.events_reported else 0.0
        console.print(
            f"  [dim]{s.vendored_events:,} event(s) in bundled dependencies "
            f"({share:.0%} of the delta) ranked last — in the JSON, not shown here[/]"
        )

    if tree.promoted:
        # §6.3: when cause attribution is uncertain, show. Saying so is the other half --
        # a refusal nobody is told about looks exactly like a rule that never fired.
        console.print(
            f"  [yellow]{len(tree.promoted)} event(s) had an unusable cause link and are "
            f"shown at top level[/]"
        )


# -- 3 · the events ----------------------------------------------------------------------


def _events(document: DiffDocument, tree: Collapsed, console: Console, *, limit: int) -> None:
    """Ranking order, grouped by component (ARCH-001 §7), consequences nested (§6.3).

    Components are headed rather than merely numbered because RESEARCH-002 §4 says the
    partition is the point: "these 2,000 lines are one mechanical rename; these 40 lines are
    the actual change" is the single most useful sentence a review tool can produce, and it
    only reads that way if the two groups are visibly separate.
    """
    if not tree.roots:
        return

    scores = document.scores

    # Grouped by component, ranking order preserved inside each. A stable sort, so this is
    # a *regrouping* of the ranking rather than a second ordering: components are numbered
    # by descending top score (`rank/components.py`), so component 0 still leads with the
    # highest-risk event in the delta. Without this, two components whose events interleave
    # by score would print their headers repeatedly and the partition -- the thing
    # RESEARCH-002 §4 calls the most useful sentence a review tool can produce -- would read
    # as noise.
    roots = sorted(
        tree.roots,
        key=lambda r: (
            scores[r.event_id].component if r.event_id in scores else len(document.components)
        ),
    )

    current: int | None = None

    for shown, root in enumerate(roots):
        if limit and shown >= limit:
            console.print(
                f"\n[dim]… {len(tree.roots) - shown} more top-level events "
                f"(--limit 0 for all, or --json for every one)[/]"
            )
            break
        score = scores.get(root.event_id)
        component = score.component if score else None
        if component != current:
            current = component
            _component_header(document, component, console)
        _event(document, root, console, depth=0)


def _component_header(document: DiffDocument, index: int | None, console: Console) -> None:
    if index is None:
        console.print("\n[bold dim]unranked[/]")
        return
    part = next((c for c in document.components if c.index == index), None)
    size = f"{part.size} event{'' if part and part.size == 1 else 's'}" if part else "?"
    console.print(f"\n[bold]component {index}[/] [dim]· {size}[/]")


def _event(document: DiffDocument, node: CollapseNode, console: Console, *, depth: int) -> None:
    event = document.by_id[node.event_id]
    score = document.score(node.event_id)
    indent = "  " + "   " * depth

    total = f"{score.total:.2f}" if score else "  — "
    # `is_high_risk`, never `total >= HIGH_RISK`. They are different questions: `total`
    # includes uncertainty so that a low-confidence event sorts *up* (ARCH-001 §7), while
    # the high-risk verdict deliberately excludes it -- otherwise, under tier-1-only
    # identity where every add and remove is unmatched residue, the marker would fire on
    # novelty alone. Recomputing the threshold here is how the dots came to disagree with
    # the count in `summary`, which is the number the reader is being asked to trust.
    marker = "[red]●[/]" if score and score.is_high_risk else "[dim]·[/]"
    glyph = "" if depth == 0 else "[dim]└ [/]"

    console.print(
        f"{indent}[dim]{total}[/] {marker} {glyph}[bold]{event.kind.value}[/]  "
        f"{_subject(document, event.subject)}"
    )

    if depth == 0:
        _why(document, node, console, indent=indent)

    for child in node.children:
        _event(document, child, console, depth=depth + 1)


def _subject(document: DiffDocument, subject: str) -> str:
    """The entity's name, marked with which side carries it.

    `sides` comes off the entity summary rather than off the event kind, which is what makes
    a rename legible for what it is in v0: the removal and the addition are two one-sided
    entities, and the marks say so without the view guessing that they are related.
    """
    label = document.label(subject)
    entity = document.entities.get(subject)  # type: ignore[arg-type]
    if entity is None:
        return f"[cyan]{label}[/]"
    mark = {(True, False): "[red]-[/]", (False, True): "[green]+[/]"}.get(
        (Side.BASE in entity.sides, Side.HEAD in entity.sides), " "
    )
    where = ""
    if entity.location is not None:
        where = f"  [dim]{entity.location.file}:{entity.location.line}[/]"
    return f"{mark} [cyan]{label}[/]{where}"


def _why(document: DiffDocument, node: CollapseNode, console: Console, *, indent: str) -> None:
    """The ranking, explained rather than recomputed (ARCH-001 §7).

    `RiskScore.factors` carries `detail` precisely so that this line exists: "37 dependent(s),
    4 direct, head model", "public", "unmatched". Ordered by contribution, so the first thing
    printed is the reason this event is where it is -- which is what a reviewer surprised by
    the order is actually asking. Nothing is recomputed; if the ordering looks wrong, the
    term responsible is named on the page and lives in `rank/weights.py`.
    """
    score = document.score(node.event_id)
    if score is None:
        return
    ordered = sorted(score.factors, key=lambda f: -f.contribution)
    why = " · ".join(f"{f.name.value.replace('_', ' ')} {f.detail}" for f in ordered)
    line = Text(f"{indent}       {why}", style="dim", no_wrap=False)
    console.print(line)
    if node.children:
        console.print(f"{indent}       [dim]{node.hidden} consequence(s) nested below[/]")


# -- 4 · what v0 deliberately cannot do --------------------------------------------------


def _residue(document: DiffDocument, console: Console) -> None:
    """STATUS §6: v0 ships tier-1 identity only, and *measuring the damage* is the deliverable.

    So the residue is printed on every run, not only when it is large, and it is printed as
    a limitation of the tool rather than as a property of the change. A reader who is not
    told what identity could not resolve will read the residue as something the branch did.

    The rename line is stated in whichever direction is true of *this* run. Renames are now
    resolved from git and applied (ARCH-001 §5), so the blanket warning that "a rename reads
    as a removal plus an addition" would be false wherever git found one -- and a caveat
    that is false where it matters most teaches a reader to discount it everywhere.
    """
    r = document.summary.residue
    renamed = document.summary.files_renamed
    tiers = ", ".join(t.value for t in r.tiers_run) or "none"
    console.print()
    console.print(f"[dim]identity: tier {tiers} only (ARCH-001 §13, STATUS §6)[/]")
    console.print(
        f"  [dim]{r.matched:,} matched · {r.unmatched_base:,} base / {r.unmatched_head:,} "
        f"head unmatched[/]"
    )
    if renamed:
        console.print(
            f"  [dim]{renamed:,} file rename{'' if renamed == 1 else 's'} resolved from git "
            f"and applied to identity[/]"
        )
    else:
        console.print(
            "  [dim]git found no renames; one it could not detect would read here as a "
            "removal plus an addition[/]"
        )
    if r.pairable_residue:
        console.print(
            f"  [dim]at most {r.pairable_residue:,} of those are pairs tiers 2-4 could "
            f"match; that is the upper bound on what rename detection would remove[/]"
        )
    if r.internal_linkage_residue:
        # ARCH-001 §5's named hazard, and now a sharper signal than it was. Renames are fed
        # to the matcher, so this being non-zero is no longer explained by "a file moved" --
        # it means a rename git did not report (a split, or below the similarity threshold),
        # or a tier-1 key that is wrong. Both are worth a look; neither is a fact about the
        # branch.
        console.print(
            f"  [yellow]{r.internal_linkage_residue:,} unmatched entities have file-scoped "
            f"USRs (internal linkage); a rename git did not report breaks tier 1 for these[/]"
        )


def _limits(document: DiffDocument, console: Console) -> None:
    """ARCH-001 §11: stated in the output itself, every time. Read off the document, so the
    JSON and the terminal state the same limits and neither can drift from the other."""
    console.print()
    console.print("[dim]true of every run:[/]")
    for limit in document.completeness.limits:
        console.print(f"  [dim]{limit}[/]")


def hidden_ids(tree: Collapsed) -> set[EventId]:
    """Every event the view nested under a cause. Exposed for tests of the §6.3 invariant:
    the document must still carry all of these."""
    return {node.event_id for root in tree.roots for node, depth in root.walk() if depth}
