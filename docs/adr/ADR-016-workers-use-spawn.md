<!-- metadata
id: ADR-016
type: adr
kind: frozen
status: accepted
decided: 2026-08-02
author: Brent Runyan
-->

# ADR-016 · Parser workers start with `spawn`, not `fork`

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-02.

## Context

Translation units are parsed in parallel processes. Threads were measured and do not help: 1.16×
on four threads, 2026-08-02
([v0 tracker §5](../../reports/2026-08-01--v0-build/STATUS.md#5--findings-worth-keeping)). Each
worker must use the libclang the parent proved complete
([ADR-012](ADR-012-system-libclang-required.md)). Recorded in `33681d8`.

## Decision

Workers use `spawn`. Each loads the parent's library, runs the probe itself, and raises if its
toolchain identity differs from the parent's.

## Consequences

- Each worker pays for proving its own toolchain at startup.
- A worker that loaded a different library fails instead of parsing with it.

## Alternatives rejected

- **`fork`.** A forked child inherits the verified library, so the guarantee would hold by accident
  of start method, and stop holding silently if that default changed.
