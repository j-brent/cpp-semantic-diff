<!-- metadata
id: ADR-039
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-039 · A macro is identified by its file and name, not by clang's USR

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

PLAN-006 step 5 adds macros to the model, and issue
[#9](https://github.com/j-brent/cpp-semantic-diff/issues/9) names their identity as the problem to
solve first. Every other node is identified by its USR
([ADR-038](ADR-038-entities-that-carry-values.md)), and a macro's USR holds the byte offset of its
definition: `c:a.cpp@8@macro@LIMIT` becomes `@23@` when a line is added above it. Paired by USR,
every macro below an edit would read as removed and added. The change catalog's `macro-shifted`
case pinned this before macros were modelled.

## Decision

- **A macro's id is its USR with the offset removed:** `c:a.cpp@macro@LIMIT`. The file name
  stays, so a macro is identified by file and name, as a `static` is, and `usr_file` reads the id
  as file-scoped: the defining TU is part of its identity, and a file rename is resolved from git
  before matching (ARCH-001 §5).
- **A name defined again in the same file** after an `#undef` is a second macro, numbered in the
  order the file defines it: `c:a.cpp@macro@LIMIT#2`.
- **Only macros the repository writes.** Clang's built-in macros and `-D` macros have no file and
  are skipped, as is anything defined outside the repository. So is an include guard: an
  object-like macro with an empty replacement, in a file clang reports as guarded against a second
  inclusion, whose name the file tested with `#ifndef` or `defined` before defining it. A guard is
  how a header is written, not a value, and it would be renamed with its header.
- **Compared:** `replacement_hash`, a hash of the tokens after the name, parameter list included;
  `function_like`. Weights 0.80 and 0.70.
- **Parsing keeps the detailed preprocessing record,** which is what puts macro definitions in the
  AST. Macro uses and `#include` directives in it are skipped before their location is read.

## Consequences

- `macro-value` and `macro-shifted-and-changed` in the change catalog pass; `macro-shifted` still
  reports nothing.
- A changed macro is reported without the code that uses it; that is the uses relationship, out of
  v0 ([ADR-041](ADR-041-v0-scope-of-steps-6-and-7.md)).
- One model is one preprocessor configuration (ARCH-001 §11). A macro defined differently on two
  branches of an `#if` is one macro, holding whichever definition the parsed TUs saw.
- Two headers of one file name in two directories that each define a macro of one name give one
  node, as two such statics do ([ADR-022](ADR-022-same-basename-statics-merge.md)).

## Alternatives rejected

- **Pair macros by name alone.** Two source files may each define their own `LIMIT`; they are not
  one macro.
- **Keep clang's USR and pair moved macros in a later tier.** Every edit would move every macro
  below it, and v0 builds no such tier.
- **Model include guards.** Every header would gain a node whose only change is its header's
  rename.
