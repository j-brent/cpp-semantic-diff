"""The terminal view: a consumer of the document, and nothing more. ARCH-001 §10.

**Every number on this page comes out of a `DiffDocument`.** No `Model`, no `Correspondence`,
no `Ranking`, no `ChangeEvent` that is not already in `document.change_events`. The function
signatures enforce it -- `render()` takes a document and a handful of display flags -- and the
reason is §10's: the moment a view can reach a model, a number that "was easier to get from the
model" lands in the presentation layer, and the analysis core has a delivery surface inside it.
That is the mistake §10 names. **If this view needs a number the document does not carry, the fix
is to put it in the document.**

**No document string passes through the markup parser.** Every value read off the document is
`escape()`d before it is interpolated into a markup template. Rich reads `[...]` as a style
tag and *drops* one it cannot resolve rather than raising, so a bracket that is not styling
disappears along with what it contained: `f [void (int)]` printed as `f`, a diagnostic about
`[[nodiscard]]` printed as one about `[]`.

Worth knowing precisely, because the obvious example is the wrong one. Rich's tag pattern
requires `[a-z#/@]` after the bracket, so a warning flag (`[-Wunused-variable]`) passes
through untouched and the hazard looks theoretical. What does not pass through is every C++
attribute (`[[nodiscard]]`, `[[maybe_unused]]`), clang's own `[note]`/`[deprecated]` phrasing,
a standard-section citation, and every annotation `DiffDocument.label` appends. All of which
is to say the rule cannot be applied by inspecting strings for danger -- it is positional.
The *view* supplies the markup; everything it styles is data.

This matters most exactly where §11's rule 2 requires the output to say what was missing: half a
reason a model is incomplete reads as a whole one.

The page is ordered by what §11 and §10 say must be seen first:

1. **Completeness, at the top, never in a log** (§11, rule 2). What was missing precedes what was
   found, because a reader who scrolls past the events has already formed a belief.
2. **The compression line** (§10). The product's own success metric, printed on every run,
   with the changes no change event explains beneath it (ADR-042).
3. **The events**, in ranking order (§7), grouped by component and with consequences nested
   under their causes (§6.3, `collapse.py`).
4. **The tier-1 residue** (§13). v0's deliberate rename damage, stated as a measurement rather
   than left to be inferred from an inflated event count.
5. **§11's systematic limits**, which are true of every run and are printed on every one.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from cpp_semantic_diff.model import AnyId, ChangeEvent, ChangeEventId, ChangeEventKind, Side
from cpp_semantic_diff.rank import subject_endpoints
from cpp_semantic_diff.render.collapse import Collapsed, CollapseNode, collapse
from cpp_semantic_diff.render.document import CompletenessBlock, DiffDocument

#: Top-level events shown before the view stops and says how many it stopped at. A page a
#: reviewer will not read is the problem the ranking exists to solve, so truncating is the
#: right default -- but truncation that does not announce itself is §11's failure in
#: miniature, so the count of what was withheld is always printed.
DEFAULT_LIMIT = 25

#: Callers of one changed signature shown before the view says how many more there are. The
#: document carries every one (ADR-040); `limit=0` shows them all here too.
CALLERS_SHOWN = 10


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
    _third_party(document, console)
    _compression(document, tree, console)
    _uncovered(document, console)
    _events(document, tree, console, limit=limit)
    _residue(document, console)
    _limits(document, console)


# -- 1 · what was analyzed ---------------------------------------------------------------


def _header(document: DiffDocument, console: Console) -> None:
    meta = document.meta
    console.print(
        f"[bold]{escape(meta.repo)}[/]  "
        f"[dim]{escape(meta.base_ref)}...{escape(meta.head_ref)}[/]  "
        f"[dim]({meta.base_rev[:12]} -> {meta.head_rev[:12]})[/]"
    )


def _completeness(document: DiffDocument, console: Console) -> None:
    """ARCH-001 §11, rule 2: "any output built on an incomplete model states what was missing, at
    the top, not in a log."

    Two blocks, because there are two kinds of missing and only one of them is news.

    **Boundary** -- how many distinct edges lead outside the repository -- prints on every run,
    degraded or not, from `CompletenessReport.edges_dropped` and `edges_total`. It is a
    sizeable share of a real graph when everything works, so it is a standing fact about what
    the model can see. This view no longer reads it out of `Completeness.notes`: the number
    lives in the document (§10) and every consumer gets it as a number.

    **Coverage** -- `CompletenessBlock.degraded` -- is the yellow banner, and it fires only
    when something actually went wrong: a TU that would not parse, a note the builder
    chose to record, or a rename map git cut short at `diff.renameLimit`. Warnings print
    verbatim rather than summarized; a summary of a caveat is a smaller caveat.
    """
    block = document.completeness
    parsed = (
        f"base {block.base.tus_parsed}/{block.base.tus_total} TUs · "
        f"head {block.head.tus_parsed}/{block.head.tus_total} TUs · "
        f"{escape(block.head.producer_version) or 'unknown producer'}"
    )

    if not block.degraded:
        console.print(f"[dim]{parsed} · both sides parsed clean[/]")
        _boundary(block, console)
        # Both sides can parse perfectly and still have been asked different questions.
        _language_mode(block, console)
        return

    console.print()
    console.print("[bold yellow]this model is incomplete[/] [dim]— read what follows knowing it[/]")
    if (renames := block.renames.warning) is not None:
        # Labelled "both": the rename map is input shared by the two sides, not either's.
        console.print(f"  [yellow]both[/] {escape(renames)}")
    for side, warning in block.warnings:
        console.print(f"  [yellow]{side.value:4}[/] {escape(warning)}")
    for report in (block.base, block.head):
        # Named, not counted. A reviewer who knows which TU failed knows which part of the
        # tree to distrust; a reviewer told "3 failed" knows only to distrust all of it.
        #
        # Escaped because a parse failure is a clang diagnostic quoting C++, and C++ is
        # bracketed: `[[nodiscard]]`, a template argument list, clang's own `[note]`. The
        # markup parser drops what it cannot style, so the sentence naming the cause used to
        # arrive with the cause removed -- a §11 rule 2 failure wearing the costume of a rule 2
        # disclosure, which is the worst place in this view for it to happen.
        for failure in report.failures[:5]:
            # `str()` rather than `or ""`: a detail-less failure has always printed "None"
            # here, and this pass is an escaping pass -- changing what the block says is a
            # separate decision from making it say all of it.
            detail = escape(str(failure.detail))
            console.print(f"  [dim]{report.side.value:4} {escape(failure.path)}: {detail}[/]")
        if len(report.failures) > 5:
            console.print(f"  [dim]{report.side.value:4} … {len(report.failures) - 5} more[/]")
    console.print(f"  [dim]{parsed}[/]")
    _boundary(block, console)
    _language_mode(block, console)


def _language_mode(block: CompletenessBlock, console: Console) -> None:
    """The `-std=` each side was parsed at, printed only when the two differ.

    Silent when they agree, because a standard that did not move explains nothing and §11's
    limits are already the standing ones. Yellow when it moved, because that is a determinant
    of the AST which no other line accounts for: leveldb `HEAD~9..~1` raises
    `CMAKE_CXX_STANDARD` from 11 to 17 in one line of CMake, and 117 events appear in vendored
    googletest that no source change explains (measured 2026-08-04, v0 tracker §5,
    reports/2026-08-01--v0-build/STATUS.md). In the delta they read as plain additions, so
    without this a reviewer has no way to reach the right conclusion.
    """
    base, head = block.base.language_modes, block.head.language_modes
    if base == head:
        return
    # `-std=` values come out of the build, not out of this project's source.
    was, now = escape(", ".join(base)), escape(", ".join(head))
    console.print(
        f"  [yellow]language mode differs: base {was or 'unset'} → "
        f"head {now or 'unset'} — additions and removals may be the standard "
        f"moving rather than the code[/]"
    )


def _boundary(block: CompletenessBlock, console: Console) -> None:
    """Where the modelled world ends, in numbers, on every run (ARCH-001 §11).

    Dim rather than yellow: this is the standing scope of a static model of one repository, not
    a fault. The percentage is what makes it readable: one dropped count is routine against a
    large graph and a catastrophe against a small one, and only the ratio distinguishes them.
    """
    for side, sentence in block.boundaries:
        console.print(f"  [dim]{side.value:4} {escape(sentence)}[/]")


def _third_party(document: DiffDocument, console: Console) -> None:
    """The third-party set and the folder-name hints (ADR-025), when there are any.

    The set is dim: it is what the run was told, not news. A hint is yellow because it asks
    the reader to act -- a library the set does not name is analyzed as the repository's own.
    """
    if document.third_party:
        listed = " · ".join(
            f"{escape(p.path)} ({', '.join(s.value for s in p.sources)})"
            for p in document.third_party
        )
        console.print(f"  [dim]third-party {listed}[/]")
    for hint in document.third_party_hints:
        console.print(f"  [yellow]note[/] {escape(hint.message)}")


# -- 2 · the metric the project is pointed at --------------------------------------------


def _compression(document: DiffDocument, tree: Collapsed, console: Console) -> None:
    """ARCH-001 §10's sentence, and the honest arithmetic around it.

    The sentence is taken from `summary.compression` rather than rebuilt, so that the JSON and the
    terminal cannot disagree about the one claim the project is testing. The line below it separates
    the two counts that are easy to conflate: `change_events_reported` is the complete delta and the
    numerator; the collapsed count is what this view chose to nest, and is never the numerator.
    """
    s = document.summary
    console.print()
    console.print(f"[bold]{escape(s.compression)}[/]")

    parts = []
    if s.compression_ratio is not None:
        parts.append(f"{s.compression_ratio:.1f} lines per change event")
    roots = len(tree.roots)
    nested = len(tree.flatten()) - roots
    parts.append(f"{roots} top-level, {nested} nested under a cause")
    parts.append(f"{s.components} component{'' if s.components == 1 else 's'}")
    parts.append(f"{s.files_changed} files ({s.cpp_files_changed} C++)")
    if document.meta.timings.total:
        parts.append(f"{document.meta.timings.total:.1f}s")
    console.print(f"  [dim]{' · '.join(parts)}[/]")

    if tree.promoted:
        # §6.3: when cause attribution is uncertain, show. Saying so is the other half --
        # a refusal nobody is told about looks exactly like a rule that never fired.
        console.print(
            f"  [yellow]{len(tree.promoted)} change event(s) had an unusable cause link and are "
            f"shown at top level[/]"
        )


def _uncovered(document: DiffDocument, console: Console) -> None:
    """Where the changes no change event explains are, beside the claim they qualify (ADR-042).

    Printed on every run where it was measured, nothing to report included, because a missing
    line would read as "everything was explained". The files are named, most first, so a reader
    knows where to look; the JSON has every line.
    """
    s = document.summary
    report = document.completeness.uncovered
    if report is None or s.uncovered_lines is None or s.uncovered_tokens is None:
        return
    line = (
        f"  [dim]{s.uncovered_lines:,} changed C++ line(s), {s.uncovered_tokens:,} token(s), "
        f"are on no change event's lines — a lower bound on what the model cannot explain"
    )
    if report.moved_tokens:
        line += f"; {report.moved_tokens:,} token(s) moved unchanged"
    console.print(line + "[/]")
    if s.uncovered_lines and report.lines_in_unmodelled_files:
        console.print(
            f"  [dim]  about {report.lines_in_unmodelled_files:,} of those line(s) are in files "
            f"no model declares anything in (roughly: no translation unit includes them)[/]"
        )
    by_size = sorted(report.files, key=lambda f: (-f.tokens, f.path, f.side.value))
    for f in by_size[:5]:
        unread = "" if f.modelled else ", no model declares anything in it"
        console.print(
            f"  [dim]  {f.side.value:4} {escape(f.path)}: {f.tokens:,} token(s) on "
            f"{len(f.lines):,} line(s){unread}[/]"
        )
    if len(by_size) > 5:
        console.print(f"  [dim]  … {len(by_size) - 5} more file(s) in the JSON[/]")
    if not report.renames_complete:
        console.print(
            "  [yellow]  git skipped inexact rename detection for this count: a file renamed "
            "and edited counts as wholly changed[/]"
        )
    for excluded in report.excluded_files:
        console.print(
            f"  [dim]  not compared: {escape(excluded.path)} ({escape(excluded.reason)})[/]"
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
                f"\n[dim]… {len(tree.roots) - shown} more top-level change events "
                f"(--limit 0 for all, or --json for every one)[/]"
            )
            break
        score = scores.get(root.event_id)
        component = score.component if score else None
        if component != current:
            current = component
            _component_header(document, component, console)
        _event(document, root, console, depth=0, limit=limit)


def _component_header(document: DiffDocument, index: int | None, console: Console) -> None:
    if index is None:
        console.print("\n[bold dim]unranked[/]")
        return
    part = next((c for c in document.components if c.index == index), None)
    size = f"{part.size} change event{'' if part and part.size == 1 else 's'}" if part else "?"
    console.print(f"\n[bold]component {index}[/] [dim]· {size}[/]")


def _event(
    document: DiffDocument, node: CollapseNode, console: Console, *, depth: int, limit: int
) -> None:
    event = document.by_id[node.event_id]
    score = document.score(node.event_id)
    indent = "  " + "   " * depth

    total = f"{score.total:.2f}" if score else "  — "
    # `is_high_risk`, never `total >= HIGH_RISK`. They are different questions: `total`
    # includes uncertainty so that a low-confidence event sorts *up* (ARCH-001 §7), while
    # the high-risk verdict deliberately excludes it, because our doubt about a match is not
    # a consequence of the change. (Before ADR-034, when every add and remove scored maximum
    # uncertainty, the marker fired on novelty alone.) Recomputing the threshold here is how
    # the dots came to disagree with the count in `summary`, which is the number the reader
    # is being asked to trust.
    marker = "[red]●[/]" if score and score.is_high_risk else "[dim]·[/]"
    glyph = "" if depth == 0 else "[dim]└ [/]"

    console.print(
        f"{indent}[dim]{total}[/] {marker} {glyph}[bold]{event.kind.value}[/]  "
        f"{_subject(document, event.subject)}{_caller_note(event)}"
    )

    if depth == 0:
        _why(document, node, console, indent=indent)

    shown = withheld = 0
    for child in node.children:
        if limit and _is_caller(document, child):
            if shown >= CALLERS_SHOWN:
                withheld += 1 + child.hidden
                continue
            shown += 1
        _event(document, child, console, depth=depth + 1, limit=limit)
    if withheld:
        # Announced, like the top-level truncation: a short list that does not say it is
        # short reads as every caller.
        console.print(
            f"{indent}   [dim]└ +{withheld} more caller(s) (--limit 0 for all, or --json)[/]"
        )


def _is_caller(document: DiffDocument, node: CollapseNode) -> bool:
    return document.by_id[node.event_id].kind is ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE


def _caller_note(event: ChangeEvent) -> str:
    """Whether a caller of a changed signature was edited in this diff (ADR-040).

    An unedited caller is the one the text diff does not show, so the line says which it is.
    """
    if event.kind is not ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE:
        return ""
    edited = event.payload.get("caller_edited") is True
    return f"  [dim]{'caller edited' if edited else 'caller not edited'}[/]"


def _subject(document: DiffDocument, subject: AnyId) -> str:
    """The entity's name, marked with which side carries it.

    `sides` comes off the entity summary rather than off the event kind, which is what makes
    a rename legible for what it is in v0: the removal and the addition are two one-sided
    entities, and the marks say so without the view guessing that they are related.

    The label is escaped, per the module rule. It is the site that rule was found at:
    `DiffDocument.label` brackets its disambiguator -- `f [void (int)]`,
    `is_named_arg [partial specialization]` -- so an unescaped label had the whole annotation
    eaten and printed the ambiguous name the document had taken care to disambiguate. The
    return value is markup this function *composed* and is interpolated by `_event` as such;
    only the parts read off the document are escaped.
    """
    label = escape(document.label(subject))
    # Only a node subject has an entity summary to mark; an edge names two entities and is
    # labelled by `document.label` without a side mark, since neither end alone is "it".
    endpoints = subject_endpoints(subject)
    entity = document.entities.get(endpoints[0]) if len(endpoints) == 1 else None
    if entity is None:
        return f"[cyan]{label}[/]"
    mark = {(True, False): "[red]-[/]", (False, True): "[green]+[/]"}.get(
        (Side.BASE in entity.sides, Side.HEAD in entity.sides), " "
    )
    where = ""
    if entity.location is not None:
        where = f"  [dim]{escape(entity.location.file)}:{entity.location.line}[/]"
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
    # `Text(...)` with an explicit style, not a markup string -- `Text` does not parse
    # markup, so `f.detail` needs no escaping and must not be rewritten into a
    # `console.print(f"[dim]…")` without one. The module rule, satisfied the other way.
    line = Text(f"{indent}       {why}", style="dim", no_wrap=False)
    console.print(line)
    if node.children:
        console.print(f"{indent}       [dim]{node.hidden} consequence(s) nested below[/]")


# -- 4 · what v0 deliberately cannot do --------------------------------------------------


def _residue(document: DiffDocument, console: Console) -> None:
    """v0 ships tier-1 identity only, and *measuring the damage* is the deliverable (§13).

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
    plural = "s" if len(r.tiers_run) > 1 else ""
    console.print()
    console.print(f"[dim]identity: tier{plural} {tiers} only (ARCH-001 §13)[/]")
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
        console.print(f"  [dim]{escape(limit)}[/]")


def hidden_ids(tree: Collapsed) -> set[ChangeEventId]:
    """Every event the view nested under a cause. Exposed for tests of the §6.3 invariant:
    the document must still carry all of these."""
    return {node.event_id for root in tree.roots for node, depth in root.walk() if depth}
