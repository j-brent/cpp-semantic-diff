<!-- metadata
id: ADR-013
type: adr
kind: frozen
status: accepted
decided: 2026-08-02
author: Brent Runyan
-->

# ADR-013 · Declining to cache beats a weaker key

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-02.

## Context

The per-TU cache key includes the content of every header a translation unit reads
([ADR-006](ADR-006-cache-per-translation-unit.md)). That list comes from `clang-scan-deps`. A
scanner whose major version differs from the parser can report a smaller set. Recorded in
`b8ee31e`.

## Decision

When `clang-scan-deps` is missing or its version does not match the parser, the cache is disabled
whole, and `Completeness` says so.

## Consequences

- Such a host is slow, never wrong.

## Alternatives rejected

- **Cache with whatever key is available.** A smaller header set gives a hit that ignores a header.
  Declining to cache is slow; a stale hit is wrong.
