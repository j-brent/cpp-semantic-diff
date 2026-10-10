<!-- metadata
id: ADR-014
type: adr
kind: frozen
status: accepted
decided: 2026-08-02
author: Brent Runyan
-->

# ADR-014 · The cache key covers the extraction code, not just libclang

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-02.

## Context

`extract.py` decides what becomes a node. Change it, and the same inputs yield a different partial
model while every other cache input stays byte-identical
([v0 tracker §5](../../reports/2026-08-01--v0-build/STATUS.md#5--findings-worth-keeping)).
Recorded in `b8ee31e`.

## Decision

The producer part of the key hashes the source of `ir.py` and `extract.py`, as well as identifying
libclang.

## Consequences

- Any change to those files, even to a comment, invalidates every cache entry.
- Superseded generations pile up and need collecting
  ([ADR-021](ADR-021-cache-gc-collects-by-age.md)).

## Alternatives rejected

- **A hand-bumped version constant.** Forgetting to bump it fails toward serving stale entries.
