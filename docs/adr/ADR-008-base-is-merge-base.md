<!-- metadata
id: ADR-008
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-008 · The base is `merge-base(main, HEAD)`

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

The tool compares two points. The main use is reviewing a branch before it merges into main.
Recorded in `e6b121c` ([ARCH-001 §3.1](../ARCH-001-architecture.md#31-what-two-commits-means)).

## Decision

The base is `merge-base(main, HEAD)`: a three-dot comparison.

## Consequences

- Only the branch's own changes are reported.
- Getting this wrong is invisible in the output, so a test guards it.

## Alternatives rejected

- **Main's tip.** Every change other people merged while the branch was open would be attributed
  to this branch.
