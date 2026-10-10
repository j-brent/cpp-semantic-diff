<!-- metadata
id: ADR-012
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-012 · A system libclang is a hard requirement

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

The `libclang` PyPI wheel ships the native library without the compiler's builtin headers. Every
translation unit then fails on `stddef.h`, clang abandons the rest of the file, and libclang
returns a truncated AST with no exception. Two models built that way omit the same declarations
and agree with each other. Found 2026-08-01: the first 50-TU timing was wrong by 21 s
([v0 tracker §5](../../reports/2026-08-01--v0-build/STATUS.md#5--findings-worth-keeping)).
Recorded in `8c60838`.

## Decision

The library is discovered on the system, probe-parsed against `<stddef.h>` and `<stdarg.h>`
before any model is built, and refused if the probe is not clean. The wheel supplies only the
Python bindings. ([ARCH-001 §13](../ARCH-001-architecture.md#13--v0), "Technology".)

## Consequences

- Setup needs a system LLVM. Convenience is not worth a failure that produces two agreeing wrong
  models.
- The parser is host-dependent. Reproducibility comes from detecting the toolchain, not pinning it
  ([ARCH-001 §12.3](../ARCH-001-architecture.md#123-what-is-deliberately-not-pinned)).

## Alternatives rejected

- **The wheel's bundled library.** It truncates ASTs silently.
