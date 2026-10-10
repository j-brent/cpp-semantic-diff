<!-- metadata
id: ADR-017
type: adr
kind: frozen
status: accepted
decided: 2026-08-02
author: Brent Runyan
-->

# ADR-017 · Toolchain identity carries the target triple, never a host path

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-02.

## Context

Toolchain identity is part of every cache key, and anything that can change an AST without
changing it is a cache-correctness bug
([ARCH-001 §12.1](../ARCH-001-architecture.md#121-toolchain-identity)). The same headers under a
different target give different declarations and `sizeof`, with identical content hashes. Recorded
in `9cdd2e3`.

## Decision

Identity includes the target triple, read from the probe translation unit of the verified library.
It contains no host path; a test asserts it has no `/`.

## Consequences

- Identity is a value two hosts compare
  ([ARCH-001 §12.2](../ARCH-001-architecture.md#122-the-invariant-is-checked-not-assumed)), so
  equivalent machines agree.
- An undeterminable target is recorded as a weaker identity, not raised as an error.

## Alternatives rejected

- **A host path, such as the resource directory.** Equivalent machines would hard-fail.
- **`llvm-config --host-target`.** It describes the host of the LLVM build, not the library's
  default target, and can point at a different LLVM.
- **A type-layout proxy.** It needs a hand-maintained list of what to measure.
