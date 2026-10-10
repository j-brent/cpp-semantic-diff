<!-- metadata
id: ADR-023
type: adr
kind: frozen
status: accepted
decided: 2026-09-20
author: Brent Runyan
-->

# ADR-023 · ARCH-001 lists every cause rule

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-09-20.

## Context

`call-moved-to-another-overload` was found by running the tool on leveldb and was missing from
ARCH-001 §6.3. It collapses leveldb `HEAD~10` from 39 first-party top-level change events to 11,
measured 2026-08-04
([v0 tracker §6](../../reports/2026-08-01--v0-build/STATUS.md#6--known-defects-and-gaps)).
Whether a design document should list every rule the code has was an open question
([v0 tracker §7](../../reports/2026-08-01--v0-build/STATUS.md#7--still-open)). Recorded in
`08db50e`.

## Decision

[ARCH-001 §6.3](../ARCH-001-architecture.md#63-cause-and-cascade) is the whole table of cause
rules. Its rows are `CAUSE_RULES` in `classify/rules.py`, its status column is `RuleStatus`, and
its order is part of the table: the first rule to find a cause wins.

## Consequences

- A cause rule is added to ARCH-001 in the same change as the code.

## Alternatives rejected

- **List only the rules the design anticipated.** A cause link is what render hides a change event
  under, so a rule that lives only in code is a change disappearing for a reason the design cannot
  explain.
