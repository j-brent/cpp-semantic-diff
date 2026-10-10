<!-- metadata
id: ADR-006
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-006 · The cache unit is the translation unit, not the commit

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

Every review builds two models, and parsing is the expensive part. Recorded in `49d030b`
([ARCH-001 §9](../ARCH-001-architecture.md#9--caching-and-the-performance-budget)); built in
`b8ee31e`.

## Decision

Partial models are cached per translation unit, keyed on its content, its headers' content, its
flags and the producer. A commit's model is assembled from cached partials plus a reparse of the
rest.

## Consequences

- Base and head share most entries, and so do reviews against the same base.
- The key must be complete: a stale hit is the worst failure the cache can have
  (see [ADR-013](ADR-013-declining-to-cache-beats-a-weaker-key.md)).

## Alternatives rejected

- **Cache per commit.** The first review against any new base pays full price, and the base is what
  every review shares.
