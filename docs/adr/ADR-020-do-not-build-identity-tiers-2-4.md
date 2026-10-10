<!-- metadata
id: ADR-020
type: adr
kind: frozen
status: superseded in part
decided: 2026-08-04
author: Brent Runyan
superseded_by: [ADR-026]
-->

# ADR-020 · Identity tiers 2–4 are not built

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-04. Superseded in part by
[ADR-026](ADR-026-changed-parameters-same-function.md) on 2026-10-10.

## Context

v0 shipped tier-1 identity so that the damage could be measured first
([ADR-002](ADR-002-tier-1-identity-only-in-v0.md)). The upper bound on change events tiers 2–4
could remove was 3, 5, 1 and 0 on fmt `HEAD~12`, fmt `HEAD~100`, spdlog `HEAD~30` and leveldb
`HEAD~9..~1`. The zero is on the only repository with real implementation files. Measured
2026-08-04
([v0 tracker §3](../../reports/2026-08-01--v0-build/STATUS.md#3--the-v0-identity-measurement--a-negative-result)).
Recorded in `9941bee`.

## Decision

Do not build tiers 2–4.

## Consequences

- Renames whose USR changes keep reading as removed plus added.
- What would reopen it: a repository mid-refactor. The number to watch is
  `internal_linkage_residue`, entities whose USR embeds the file name.

## Alternatives rejected

- **Build them as planned.** The measurement argued against the next planned investment.
