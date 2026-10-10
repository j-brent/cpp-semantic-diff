<!-- metadata
id: ADR-019
type: adr
kind: frozen
status: superseded
decided: 2026-08-04
author: Brent Runyan
superseded_by: [ADR-025]
-->

# ADR-019 · Vendored code is partitioned out of the ranking, not merely down-weighted

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Superseded by [ADR-025](ADR-025-third-party-code-must-not-change.md) on 2026-10-10.

## Context

On leveldb `HEAD~10`, which bumps bundled googletest, the top 12 change events and 44 of the top
50 were googletest. Exposure was one weighted term worth at most 0.25, while blast radius plus
novelty was worth 0.60. Measured 2026-08-04
([v0 tracker §2](../../reports/2026-08-01--v0-build/STATUS.md#2--the-measurement-this-project-exists-to-take)).
Recorded in `562921c`.

## Decision

Every vendored change event sorts after every first-party one, as a block. Only vendored code is
partitioned: the project's own tests are its code and keep their weighted demotion. The exposure
weight stays, to order vendored change events among themselves.

## Consequences

- leveldb `HEAD~10`: vendored change events in the top 12 went from 12 to 0, and in the top 50
  from 44 to 0.
- Vendored code is still modelled, compared and reported, last.

## Alternatives rejected

- **Raise the exposure weight.** It would fit a number to one repository and still lose on the
  next. The claim is "not what is being reviewed", which is an ordering, not a quantity.
