"""Uncovered changes: changed tokens no change event explains (ADR-042).

A change the model did not turn into a change event used to read as "nothing changed". These
pin what counts as explained, what counts as moved, and that nothing changed is ever silent.
"""

from __future__ import annotations

from cpp_semantic_diff.acquire.tokens import (
    ExcludedFile,
    LineHunk,
    Token,
    TokenDelta,
    TokenRun,
    changed_runs,
)
from cpp_semantic_diff.diff.coverage import MIN_MOVED_TOKENS, measure_coverage
from cpp_semantic_diff.model import (
    ChangeEvent,
    ChangeEventKind,
    Confidence,
    Hunk,
    Location,
    MatchTier,
    NodeId,
    Side,
)

SURE = Confidence(tier=MatchTier.DECLARED, models_complete=True)


def run(
    side: Side, file: str, *lines: int, texts: tuple[str, ...] = (), whole: int = 1
) -> TokenRun:
    """A run with one token per line given, or `texts` all on the first line."""
    if texts:
        tokens = tuple(Token(t, lines[0], lines[0]) for t in texts)
    else:
        tokens = tuple(Token(f"t{line}", line, line) for line in lines)
    return TokenRun(side, file, tokens, whole)


def at(side: Side, file: str, first: int, last: int, start: int | None = None) -> ChangeEvent:
    location = Location(file=file, line=first, end_line=last, start_line=start)
    return ChangeEvent(
        kind=ChangeEventKind.NODE_BODY_CHANGED,
        subject=NodeId(f"c:@F@{file}{first}#"),
        provenance=[Hunk(side=side, location=location)],
        confidence=SURE,
    )


class TestExplained:
    def test_a_token_on_an_events_lines_is_explained(self) -> None:
        delta = TokenDelta(runs=(run(Side.HEAD, "a.cpp", 5, 40),))
        coverage = measure_coverage(delta, [at(Side.HEAD, "a.cpp", 3, 10)])
        assert (coverage.changed_lines, coverage.uncovered_lines) == (2, 1)
        assert [(f.side, f.path, f.lines) for f in coverage.files] == [(Side.HEAD, "a.cpp", (40,))]

    def test_the_side_and_the_file_must_both_match(self) -> None:
        """Base line 5 and head line 5 are different lines, and so are two files' line 5."""
        delta = TokenDelta(runs=(run(Side.BASE, "a.cpp", 5), run(Side.HEAD, "b.cpp", 5)))
        coverage = measure_coverage(delta, [at(Side.HEAD, "a.cpp", 1, 10)])
        assert coverage.uncovered_lines == 2

    def test_a_declarations_first_line_counts(self) -> None:
        """A template header above the name is part of the span (`Location.start_line`)."""
        delta = TokenDelta(runs=(run(Side.HEAD, "a.h", 4),))
        assert measure_coverage(delta, [at(Side.HEAD, "a.h", 5, 9, start=4)]).uncovered_lines == 0
        assert measure_coverage(delta, [at(Side.HEAD, "a.h", 5, 9)]).uncovered_lines == 1

    def test_line_granularity_makes_the_count_a_lower_bound(self) -> None:
        """An unrelated edit inside a changed function's span is counted as explained, never
        the other way round: the number can understate what the model misses, not overstate."""
        delta = TokenDelta(runs=(run(Side.HEAD, "a.cpp", 7),))
        assert measure_coverage(delta, [at(Side.HEAD, "a.cpp", 1, 20)]).uncovered_tokens == 0

    def test_no_events_leaves_every_change_uncovered(self) -> None:
        delta = TokenDelta(runs=(run(Side.BASE, "a.cpp", 1), run(Side.HEAD, "a.cpp", 1)))
        coverage = measure_coverage(delta, [])
        assert (coverage.uncovered_lines, coverage.uncovered_tokens) == (2, 2)

    def test_excluded_files_are_carried_through(self) -> None:
        excluded = (ExcludedFile("blob.h", "binary"),)
        assert measure_coverage(TokenDelta(excluded_files=excluded), []).excluded_files == excluded


class TestMoved:
    BLOCK = tuple(f"x{i}" for i in range(MIN_MOVED_TOKENS))

    def test_a_block_deleted_here_and_inserted_there_is_moved_not_uncovered(self) -> None:
        delta = TokenDelta(
            runs=(
                run(Side.BASE, "a.cpp", 3, texts=self.BLOCK),
                run(Side.HEAD, "b.cpp", 90, texts=self.BLOCK),
            )
        )
        coverage = measure_coverage(delta, [])
        assert (coverage.moved_tokens, coverage.uncovered_tokens) == (2 * MIN_MOVED_TOKENS, 0)

    def test_each_insertion_is_the_destination_of_one_move(self) -> None:
        """A block deleted twice and inserted once is one move and one deletion."""
        delta = TokenDelta(
            runs=(
                run(Side.BASE, "a.cpp", 3, texts=self.BLOCK),
                run(Side.BASE, "a.cpp", 30, texts=self.BLOCK),
                run(Side.HEAD, "a.cpp", 60, texts=self.BLOCK),
            )
        )
        coverage = measure_coverage(delta, [])
        assert coverage.moved_tokens == 2 * MIN_MOVED_TOKENS
        assert [(f.side, f.lines) for f in coverage.files] == [(Side.BASE, (30,))]

    def test_a_short_or_partial_line_match_is_not_a_move(self) -> None:
        """`return 0;` in two places is coincidence, and calling it a move would hide it."""
        short = self.BLOCK[:-1]
        for texts, whole in ((short, 1), (self.BLOCK, 0)):
            delta = TokenDelta(
                runs=(
                    run(Side.BASE, "a.cpp", 3, texts=texts, whole=whole),
                    run(Side.HEAD, "a.cpp", 9, texts=texts, whole=whole),
                )
            )
            assert measure_coverage(delta, []).moved_tokens == 0

    def test_a_swapped_pair_of_functions_is_found_from_real_text(self) -> None:
        old = "int a() { return 1 + 2 + 3; }\nint b() { return 4 + 5 + 6; }\n"
        new = "int b() { return 4 + 5 + 6; }\nint a() { return 1 + 2 + 3; }\n"
        runs = changed_runs("a.cpp", old, "a.cpp", new, [LineHunk(1, 2, 1, 2)])
        coverage = measure_coverage(TokenDelta(runs=tuple(runs)), [])
        assert coverage.uncovered_tokens == 0
        assert coverage.moved_tokens == sum(len(r.tokens) for r in runs)
