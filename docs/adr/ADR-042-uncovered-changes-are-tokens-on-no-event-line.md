<!-- metadata
id: ADR-042
type: adr
kind: frozen
status: pending Brent Runyan's review
decided: 2026-10-11
author: agent review during v0 orchestration, pending Brent Runyan's review
-->

# ADR-042 · An uncovered change is a changed token on no change event's lines

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

A change the model cannot see produces no change event, and the output then reads as if
nothing changed. Of 22 ordinary changes in the change catalog, 16 did this
([PLAN-006 §1](../plans/PLAN-006-model-sufficiency.md#1--the-problem)). The compression ratio
made it worse: it improves when the model sees less. [#6](https://github.com/j-brent/cpp-semantic-diff/issues/6)
asks for every run to count the changes in the repository's C++ files that no change event
explains, compared as tokens so that whitespace and comment edits do not count.

Every change event already carries its source lines (ARCH-001 §6.2), so the count is a set
difference against the text diff. What had to be decided is what "explains" means, how text is
turned into tokens, and how a moved block is counted.

## Decision

- **A changed token is explained when a change event's provenance hunk, on the same side and in
  the same file, contains its line.** Every other changed token is uncovered. Per run the output
  gives the changed C++ lines, the uncovered lines and tokens, and the uncovered lines per file;
  the compression sentence carries the uncovered count.
- **Line granularity, so the count is a lower bound.** A hunk covers whole lines, and an event
  about a function covers all of it, so an unexplained edit inside a changed function's span
  counts as explained. The count can understate what the model misses and cannot overstate it,
  provided every line that declares an entity is on its events' hunks. So a node records every
  declaration site (`Node.sites`) and a span starts where the declaration's text starts
  (`Location.start_line`).
- **Tokens come from this project's own lexer,** a regular expression over the text: comments,
  whitespace and line splices are not tokens; raw string literals, digit separators and encoding
  prefixes are. Within each hunk of a zero-context `git diff`, the two sides' tokens are aligned
  and an equal stretch is formatting. `>>` is two tokens, so `> >` and `>>` compare equal.
- **A moved block is counted as moved, not as uncovered.** A run of changed tokens deleted in one
  place and inserted token for token in another, at least 8 tokens and one whole line on each
  side, is a move. Pairing is one to one: each inserted run is the destination of at most one
  move. "Moved" means textually identical, not semantically unchanged: the same text in a new
  scope can mean something else.
- **Code the configuration does not compile is uncovered.** An edit under `#if 0`, in a file for
  another platform, or in a header no translation unit includes is in no model. Counting it is
  correct: the model cannot see it.
- **Binary files are listed as not compared,** with the reason, rather than dropped.

## Consequences

- A blind spot becomes a number on every run, next to the claim it qualifies.
- Every change catalog case satisfies "a change the tokens show produces change events, or
  uncovered or moved tokens"; `tests/test_change_catalog.py` checks it.
- A textual change with no semantic effect, such as a namespace spelled through a macro, counts
  as uncovered. The model does not explain it, and saying so is correct.
- Until third-party code is excluded (#6), the count includes vendored code and is not to be
  quoted.

## Alternatives rejected

- **Lex with libclang.** It would need each file's compile flags, and the files that most need
  counting are the ones with none: headers no translation unit includes, sources for another
  platform. The tokens are only ever compared with each other, so a lexer that agrees with
  itself is enough.
- **Count lines, not tokens.** A reindented block or an edited comment would count as a change
  the model failed to explain.
- **Token-precise explanation,** which tokens an event is about. It would need each change event
  to say which tokens produced it, which none can. Line granularity gives a lower bound now.
- **Leave moves uncovered.** A function moved within a file produces no change event and is
  not a change to its behaviour; reporting every moved token as unexplained would bury the
  changes that are.
