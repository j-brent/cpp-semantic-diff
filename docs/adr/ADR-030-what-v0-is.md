<!-- metadata
id: ADR-030
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
-->

# ADR-030 · v0 is a tool whose output can be trusted on changes the repository controls

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

All pipeline stages but `enrich` run end to end. The 2026-10-10 review found that the tool
misreports ordinary changes, in seven confirmed issues, and that 16 of 22 small, ordinary changes
produce no change event at all, measured 2026-10-10
([`the-model.md`](../../reports/2026-10-10--up-to-speed/the-model.md)). Decided in the roadmap
review with Brent; not yet in a commit.

## Decision

v0 is a tool whose output can be trusted on changes the repository controls
([ADR-025](ADR-025-third-party-code-must-not-change.md)).

**In:**
- all seven confirmed issues in the roadmap, as of 2026-10-10;
- [PLAN-006](../plans/PLAN-006-model-sufficiency.md) step 1 (the change catalog) and step 2
  (report what the model cannot see);
- the parts of PLAN-006 the issues require: steps 4 and 5, and as much of steps 6 and 7 as issues
  4 and 5 need;
- detecting and reporting a rename map that git truncated (`diff.renameLimit`), because a silent
  truncation is a wrong answer.

**Out:**
- PLAN-006 steps 3, 8 and 9;
- the Bitcoin Core probe (PLAN-002), deferred until the model is fixed;
- where the reviewer reads the output: out of scope for now, ad hoc views suffice;
- `enrich`, post-v0 by design.

## Consequences

- ARCH-001 §13's definition of v0 widens beyond tier-1 identity
  ([ADR-026](ADR-026-changed-parameters-same-function.md)).
- The change catalog defines when each in-scope step is done.

## Alternatives rejected

- **v0 as built,** the original ARCH-001 §13 slice. Its output misdescribes ordinary changes.
- **Probe scale first** (PLAN-002). Probing scale before the model is right would measure the
  wrong thing.
