<!-- metadata
id: ADR-018
type: adr
kind: frozen
status: accepted
decided: 2026-08-02
author: Brent Runyan
-->

# ADR-018 · Check what the whole key covers before widening an identity

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-02.

## Context

ARCH-001 §12.1 lists six inputs that can change an AST. Measured 2026-08-02: the include closure
already content-hashes the standard library and clang's builtin headers, so five of six were
covered before anything was added. Recorded in `9cdd2e3`; stated as a rule in `0b227d9`.

## Decision

Before adding a component to an identity or cache key, establish what the whole key already
covers.

## Consequences

- Fewer mass invalidations.
- Applied again to `CPPSD_RESOURCE_DIR`: no key was widened, and the cache declines to store where
  the closure does not cover the directory
  ([v0 tracker §6](../../reports/2026-08-01--v0-build/STATUS.md#6--known-defects-and-gaps)).

## Alternatives rejected

- **Add a key component per listed input.** Each redundant one costs a mass invalidation to defend
  against a hazard already defended.
