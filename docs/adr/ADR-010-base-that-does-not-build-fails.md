<!-- metadata
id: ADR-010
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-010 · A base that does not build is a hard failure

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

The base is the merge base with main ([ADR-008](ADR-008-base-is-merge-base.md)). Recorded in
`e6b121c` ([ARCH-001 §3.1](../ARCH-001-architecture.md#31-what-two-commits-means)).

## Decision

If the base does not build, the tool fails. There is no partial mode.

## Consequences

- A main that does not build is a different problem, not this tool's.
- An incomplete worktree can look like this failure. A missing submodule did, until `acquire`
  populated submodules ([v0 tracker §5](../../reports/2026-08-01--v0-build/STATUS.md#5--findings-worth-keeping)).

## Alternatives rejected

- **A partial mode** that analyzes whatever parses.
