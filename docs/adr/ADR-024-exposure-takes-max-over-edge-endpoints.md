<!-- metadata
id: ADR-024
type: adr
kind: frozen
status: accepted
decided: 2026-09-20
author: Brent Runyan
-->

# ADR-024 · Exposure of an edge change takes the max over its endpoints

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-09-20.

## Context

Blast radius and decomposition attribute an edge change event to its source (`subject_home`).
Exposure took the more exposed of the two endpoints, and the asymmetry needed a decision. Recorded
in `b4db4d8`.

## Decision

The max is kept. `subject_home` answers attribution: whose text changed, what is coupled.
ARCH-001 §7 asks exposure whether the subject is reachable from a public header, which is a
property of the pair an edge names. Two tests in `tests/test_rank.py` fail if exposure follows
`subject_home`.

## Consequences

- Measured 2026-09-20: spdlog `HEAD~30` and leveldb `HEAD~10` give field-for-field identical
  documents under both rules. fmt `HEAD~12` has the only two divergences, and both favour the max
  ([v0 tracker §6](../../reports/2026-08-01--v0-build/STATUS.md#6--known-defects-and-gaps)).
- Not guarded: an edge from vendored code into first-party code would score as first-party and
  escape the vendored partition. No instance exists; `rank/score.py` names the trigger.

## Alternatives rejected

- **Source only.** On fmt it relabels two new public partial specializations as test code, because
  the source of a `contains` edge is a namespace node whose location is arbitrary.
