<!-- metadata
id: ADR-021
type: adr
kind: frozen
status: accepted
decided: 2026-08-04
author: Brent Runyan
-->

# ADR-021 · `cppsd cache gc` collects by age, not by identity

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-04.

## Context

Every change to the extraction code gives every translation unit a new cache identity
([ADR-014](ADR-014-cache-key-covers-extraction-code.md)), and nothing writes to the old one again.
Measured 2026-08-04: 576 of 1,156 entries, 147 of 229 MB, were unreachable
([v0 tracker §6](../../reports/2026-08-01--v0-build/STATUS.md#6--known-defects-and-gaps)).
Recorded in `a645cee`.

## Decision

`cppsd cache gc` deletes entries nothing has touched for `--older-than` days. A cache hit touches
its entry, so age tracks use.

## Consequences

- A superseded generation survives until it is that old: the same directories, collected one
  generation later.
- Cleaning the cache needs no toolchain and no repository.
- Worktrees are reported, not collected.
- Reaffirmed in the 2026-10-10 roadmap review as an accepted design, not a defect.

## Alternatives rejected

- **Delete every identity that is not the current producer's.** Knowing the current identity needs
  a toolchain and a checkout, so cleaning the cache would need a repository to clean it against.
