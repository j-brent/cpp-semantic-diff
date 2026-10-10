<!-- metadata
id: ADR-022
type: adr
kind: frozen
status: accepted
decided: 2026-08-04
author: Brent Runyan
-->

# ADR-022 · Two same-named `static`s in same-named files stay merged, and named

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-04.

## Context

A `static`'s USR embeds its file's name, not its path. So two same-named `static`s in `a/x.cc` and
`b/x.cc` share a USR, and the model, keyed on the USR, merges them into one node. Half of them is
invisible, and a body change in either produces no change event. Separating them needs the model
keyed on `(usr, tu)`, which reaches every edge, change event, cache entry and the JSON contract.
Recorded in `07ed88b`; restated as a standing decision, with its trigger, in `85fd107`
(2026-09-07). See the
[v0 tracker §6](../../reports/2026-08-01--v0-build/STATUS.md#6--known-defects-and-gaps).

## Decision

Do not fix it now. Name it, and leave it.

## Consequences

- The cost of fixing is certain and large; the occurrence rate is zero on every fixture.
  `tests/test_tu_identity.py` is the only place it exists.
- The trigger to revisit is an observed instance. `merge_node` sees both observations, so the
  collision could be counted cheaply.

## Alternatives rejected

- **Key the model on `(usr, tu)`.** Days of work through every shared contract.
- **A half fix.** `Node.tu` fixed only the matching half; the model-level merge remains.
