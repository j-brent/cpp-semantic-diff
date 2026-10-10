<!-- metadata
id: ADR-029
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
-->

# ADR-029 · What libclang cannot see is reported, not worked around

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

libclang does not expose implicit special members: the copy and move operations the compiler
writes. Adding a destructor to a class silently removes its implicit move operations, so every
move of that class becomes a copy, and nothing built on libclang can see it. Recorded in
[PLAN-006 §3](../plans/PLAN-006-model-sufficiency.md#3--what-libclang-cannot-see) and the roadmap's
decision list; not yet in a commit.

## Decision

Accept the limit and report it. Every run states it, the way it states `edges_dropped`. Change
catalog cases that depend on it expect "reported as not visible".

## Consequences

- The tool says when it cannot see a change, instead of reporting nothing.

## Alternatives rejected

- **Also read the full Clang AST,** for example `clang -Xclang -ast-dump=json`, which includes
  these traits. It is a second reader of the code: it needs its cost measured, its own correctness
  checks, and a rule for when the two readers disagree. Deferred, not ruled out.
