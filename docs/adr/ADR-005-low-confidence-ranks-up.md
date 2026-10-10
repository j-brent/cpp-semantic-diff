<!-- metadata
id: ADR-005
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-005 · Low confidence ranks a change event up

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

Every change event carries a confidence, derived from how its entity was paired and how complete
both models are. Recorded in `49d030b`
([ARCH-001 §7](../ARCH-001-architecture.md#7--ranking-and-attention)).

## Decision

A low-confidence change event ranks higher, not lower. Uncertainty is a reason for a human to
look, not a reason to hide.

## Consequences

- The ranker's uncertainty term raises scores. Mutation-tested cases guard the direction.

## Alternatives rejected

- **Rank low confidence down,** as most tools would. It hides exactly the changes the tool may
  have got wrong.
