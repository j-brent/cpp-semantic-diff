<!-- metadata
id: ADR-011
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-011 · Linux only during design and implementation

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

The first code was written on Windows. Development moved to WSL/Linux. Recorded in `217c626`.

## Decision

Linux is the only supported platform while the design is still moving.

## Consequences

- Windows-specific code paths are not maintained as features.
- Portability rules stay ([`CONTRIBUTING.md`](../../CONTRIBUTING.md#code)): no hardcoded paths.
  Cross-platform *correctness* is cheap; cross-platform *support* is not.

## Alternatives rejected

- **Keep supporting Windows.** It is dead weight while the shape is still moving.
