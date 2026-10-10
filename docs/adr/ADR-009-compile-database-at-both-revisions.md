<!-- metadata
id: ADR-009
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-009 · A compile database is required at both revisions

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

Parsing C++ correctly needs each file's flags. Recorded in `e6b121c`
([ARCH-001 §3.1](../ARCH-001-architecture.md#31-what-two-commits-means)).

## Decision

A compile database at both revisions is a hard precondition. CMake configure produces it; a full
build is not needed.

## Consequences

- Configure takes seconds, so the cost is affordable.
- A repository with generated sources is the exception: it must report what it could not parse as
  incompleteness. The policy for it is still open.

## Alternatives rejected

- **A degraded mode,** such as a single-file parse without flags, explicitly labelled. It was the
  open question in `49d030b`.
