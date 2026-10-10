<!-- metadata
id: ADR-033
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
-->

# ADR-033 · `cppsd` exits with one code per kind of failure

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

Every refused precondition exited 2: a shallow clone, an unknown ref, a missing compile database and
an untrustworthy libclang. 2 is also what Click exits with for a malformed command line. Any other
failure in the package, such as git failing mid-run or the worker pool giving up, escaped as a
traceback with exit 1. A caller such as `demo.yml` or a CI job could not tell these apart without
parsing stderr.

## Decision

Each `SemanticDiffError` carries an `exit_code`, and the CLI exits with it:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | uncaught exception: a defect in `cppsd`, reported by its traceback |
| 2 | a malformed command line, option value or environment setting |
| 3 | the repository or the revisions cannot be compared |
| 4 | no usable compile database at a revision |
| 5 | no parser that can be trusted |
| 6 | an operation failed while running: git, the worker pool, writing output |

The codes are defined once, as `errors.ExitCode`.

## Consequences

- A caller can branch on the kind of failure: retry on 6, fix its input on 2, install LLVM on 5.
- Exit 1 now means only a defect in `cppsd`, so any exit 1 is worth a bug report.
- Codes 3, 4 and 5 replace 2 for refused preconditions. A caller that tested for exactly 2 must change.

## Alternatives rejected

- **Keep one code for every refusal.** It cannot tell "fix your input" from "install LLVM".
- **One code per error class.** Callers act on the kind of failure, not on the class that raised it,
  and the classes change more often than the kinds.
