<!-- metadata
id: ADR-004
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-004 · Cascade collapse happens at render, never at diff

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

One change causes others: deleting a class removes every member and every edge into it. A reviewer
should read the cause, with its consequences filed under it. Recorded in `49d030b`
([ARCH-001 §6.3](../ARCH-001-architecture.md#63-cause-and-cascade)).

## Decision

The JSON always carries the full set of change events. Only the view hides consequences under
their causes. When cause attribution is uncertain, the view shows rather than hides.

## Consequences

- Nothing is lost in the product; a wrong cause link only affects what is folded in a view.
- A change event with no cause link is always shown.

## Alternatives rejected

- **Collapse at diff,** dropping consequences from the output. A wrong cause link would then
  delete a real change.
