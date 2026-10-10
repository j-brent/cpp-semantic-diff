<!-- metadata
id: ADR-003
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-003 · Identity pairing is one-to-one

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

Code is split (one function becomes two) and merged. A matcher could pair one old entity with
several new ones, or the reverse. Recorded in `49d030b`
([ARCH-001 §5](../ARCH-001-architecture.md#5--identity)).

## Decision

Pairing is bipartite and one-to-one at every tier. Splits and merges surface as unmatched entities
rather than being forced into a match. This is enforced in the type, not just in the matcher.

## Consequences

- A split or merge reads as removals and additions.
- A double pairing cannot happen silently. If it could, it would not crash: it would produce a
  delta that reads well and describes a change nobody made.

## Alternatives rejected

- **Many-to-one or one-to-many pairing.** Confident and wrong is the failure to avoid.
