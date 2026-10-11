"""Which changed tokens no change event explains (ADR-042).

The reverse direction of provenance (ARCH-001 §6.2), applied to the whole text diff: a changed
token is **explained** when a change event's provenance hunk, on the same side and in the same
file, contains its line. Every other changed token is **uncovered** -- a change the model did
not turn into any change event, which until now read as "nothing changed".

Line granularity, on purpose. A hunk is a line span, and an event about a function covers
every line of it, so an edit inside a changed function's span is explained even when the event
is not about that edit. The count is therefore a lower bound on what the model cannot explain,
never an overstatement of it -- provided every line that declares an entity is on one of its
events' hunks, which is what `Node.sites` is for.

Code under `#if 0`, or under any preprocessor branch the configuration does not take, is in no
model, so its changes are uncovered. That is the correct answer, not a false alarm: the model
genuinely cannot see them (ARCH-001 §11).

Runs after classify, over the final change events, and changes none of them.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from cpp_semantic_diff.acquire.tokens import ExcludedFile, TokenDelta, TokenRun
from cpp_semantic_diff.model import ChangeEvent, Model, Side

#: The least a run must be to count as moved: this many tokens and one whole line on each side.
#: Below that, identical text in two places is as likely to be coincidence -- `return 0;`, a
#: closing brace -- as one block moved, and calling coincidence a move would hide a change.
MIN_MOVED_TOKENS = 8


@dataclass(frozen=True)
class UncoveredFile:
    """One side of one file's uncovered tokens, with the lines they are on."""

    side: Side
    path: str
    lines: tuple[int, ...]
    tokens: int
    #: Whether either model holds a declaration in this file. False approximates "no
    #: translation unit includes it": it also covers an included file that declares nothing
    #: the model records, such as one holding only macros.
    modelled: bool = True


@dataclass(frozen=True)
class Coverage:
    """How much of the C++ text diff the change events explain."""

    #: Lines, on either side, carrying at least one changed token. A deleted line and the
    #: line that replaced it are two.
    changed_lines: int = 0
    #: Every changed token is exactly one of explained, uncovered or moved.
    changed_tokens: int = 0
    explained_tokens: int = 0
    uncovered_lines: int = 0
    uncovered_tokens: int = 0
    #: Changed tokens in a block that was deleted in one place and inserted, token for token,
    #: in another. Textually identical, which is not the same as semantically unchanged.
    moved_tokens: int = 0
    files: tuple[UncoveredFile, ...] = ()
    excluded_files: tuple[ExcludedFile, ...] = ()
    #: False when git cut rename detection short for the text diff (`TokenDelta.rename_limit`).
    renames_complete: bool = True

    @property
    def unmodelled_lines(self) -> int:
        """Uncovered lines in files neither model declares anything in. Approximate."""
        return sum(len(f.lines) for f in self.files if not f.modelled)


def _moved(runs: Sequence[TokenRun]) -> set[int]:
    """Indices of the runs that are one half of a move.

    One-to-one: each inserted run is the destination of at most one deleted run, so a block
    pasted twice is one move and one insertion, not two moves. Pairs are chosen in file and line
    order on both sides, which makes the choice a function of the diff and nothing else.
    """

    def eligible(run: TokenRun) -> bool:
        return len(run.tokens) >= MIN_MOVED_TOKENS and run.whole_lines >= 1

    def order(i: int) -> tuple[str, int]:
        return runs[i].file, runs[i].tokens[0].line

    inserted: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for i in sorted(range(len(runs)), key=order):
        if runs[i].side is Side.HEAD and eligible(runs[i]):
            inserted[runs[i].texts].append(i)
    moved: set[int] = set()
    for i in sorted(range(len(runs)), key=order):
        run = runs[i]
        if run.side is Side.BASE and eligible(run) and (targets := inserted.get(run.texts)):
            moved.update((i, targets.pop(0)))
    return moved


class _Spans:
    """Every provenance line span, merged per side and file, for a line-membership test."""

    def __init__(self, events: Iterable[ChangeEvent]) -> None:
        raw: dict[tuple[Side, str], list[tuple[int, int]]] = defaultdict(list)
        for event in events:
            for hunk in event.provenance:
                location = hunk.location
                end = location.end_line or location.line
                raw[(hunk.side, location.file)].append((location.first_line, end))
        self._starts: dict[tuple[Side, str], list[int]] = {}
        self._ends: dict[tuple[Side, str], list[int]] = {}
        for key, spans in raw.items():
            merged: list[tuple[int, int]] = []
            for first, last in sorted(spans):
                if merged and first <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], last))
                else:
                    merged.append((first, last))
            self._starts[key] = [first for first, _ in merged]
            self._ends[key] = [last for _, last in merged]

    def contains(self, side: Side, file: str, line: int) -> bool:
        starts = self._starts.get((side, file))
        if not starts:
            return False
        i = bisect.bisect_right(starts, line) - 1
        return i >= 0 and line <= self._ends[(side, file)][i]


def modelled_files(*models: Model) -> frozenset[str]:
    """Every file any node in these models is located or declared in."""
    return frozenset(
        location.file
        for model in models
        for node in model.nodes.values()
        for location in (*((node.location,) if node.location else ()), *node.sites)
    )


def measure_coverage(
    delta: TokenDelta,
    events: Iterable[ChangeEvent],
    modelled: Collection[str] | None = None,
) -> Coverage:
    """Set the changed tokens against the change events' provenance.

    `modelled` is the files the two models declare anything in (`modelled_files`), which
    sorts uncovered files into a first, cheap breakdown: a change in a file no model reads
    from is one the model could not have explained. None leaves every file marked modelled.
    """
    runs = delta.runs
    moved = _moved(runs)
    spans = _Spans(events)

    changed: set[tuple[Side, str, int]] = set()
    uncovered: dict[tuple[Side, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    moved_tokens = explained_tokens = 0
    for i, run in enumerate(runs):
        for token in run.tokens:
            changed.add((run.side, run.file, token.line))
        if i in moved:
            moved_tokens += len(run.tokens)
            continue
        for token in run.tokens:
            if spans.contains(run.side, run.file, token.line):
                explained_tokens += 1
            else:
                uncovered[(run.side, run.file)][token.line] += 1

    files = tuple(
        UncoveredFile(
            side=side,
            path=path,
            lines=tuple(sorted(lines)),
            tokens=sum(lines.values()),
            modelled=modelled is None or path in modelled,
        )
        for (side, path), lines in sorted(uncovered.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    )
    return Coverage(
        changed_lines=len(changed),
        changed_tokens=sum(len(run.tokens) for run in runs),
        explained_tokens=explained_tokens,
        uncovered_lines=sum(len(f.lines) for f in files),
        uncovered_tokens=sum(f.tokens for f in files),
        moved_tokens=moved_tokens,
        files=files,
        excluded_files=delta.excluded_files,
        renames_complete=delta.rename_limit is None,
    )
