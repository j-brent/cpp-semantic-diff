<!-- metadata
id: ADR-002
type: adr
kind: frozen
status: superseded in part
decided: 2026-08-01
author: Brent Runyan
superseded_by: [ADR-026]
-->

# ADR-002 · v0 ships tier-1 identity only

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01. Superseded in part by
[ADR-026](ADR-026-changed-parameters-same-function.md) on 2026-10-10.

## Context

The tool pairs each entity in the "before" model with one in the "after" model. Tier 1 pairs by
clang's identifier (the USR). Tiers 2–4 would detect renames and moves, at real cost.
RESEARCH-002 §7 step 4 says to ship tier 1 and measure how badly renames hurt before building the
rest. Recorded in `49d030b` ([ARCH-001 §13](../ARCH-001-architecture.md#13--v0)).

## Decision

v0 pairs entities by tier 1 only. Renames look bad on purpose. Measuring the damage is a v0
deliverable.

## Consequences

- A renamed or moved entity whose USR changes is reported as removed plus added.
- The measurement was taken; see [ADR-020](ADR-020-do-not-build-identity-tiers-2-4.md).

## Alternatives rejected

- **Build tiers 2–4 in v0,** before knowing whether they pay for themselves.
