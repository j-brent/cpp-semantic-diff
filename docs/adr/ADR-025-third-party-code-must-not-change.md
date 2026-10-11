<!-- metadata
id: ADR-025
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
supersedes: [ADR-019]
-->

# ADR-025 · Third-party code must not change between the two points

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

The tool modelled, compared and scored vendored code like the project's own, and only ranked it
last ([ADR-019](ADR-019-vendored-code-partitioned-in-ranking.md)). On leveldb `HEAD~10`, a
googletest and benchmark bump produced 3,660 change events in that code, 96 % of the total, plus
134 in leveldb's own code, mostly tests that changed because googletest moved under them
([v0 tracker §2](../../reports/2026-08-01--v0-build/STATUS.md#2--the-measurement-this-project-exists-to-take),
re-measured 2026-10-10). Recognition by folder name misses a library copied under another name.
Recorded in `b934fc1` (known issue 7) and `4f083c1`
([ARCH-001 §13](../ARCH-001-architecture.md#13--v0),
[PLAN-004 §2](https://github.com/j-brent/cpp-semantic-diff/blob/62dc064/docs/plans/PLAN-004-third-party-boundary.md#2--decisions)).

## Decision

v0 analyzes only code the repository owns and contains. Third-party code must be identical at both
points; a comparison where it changed is refused. Its headers are still parsed where the
repository's code includes them, because that code depends on them, but its own translation units
are not, and none of it is in the model, like the standard library. Calls into
it are recorded as boundary crossings labelled with where they lead, for visualization only: the
comparison and the ranking never read them.

Third-party code is recognized from git submodules, automatically, and from paths the user
declares. Folder names are only a hint: they produce a note and never exclude anything.

## Consequences

- A dependency bump, such as leveldb `HEAD~10`, is refused.
- No vendored class remains in ranking or output. The output-format change is shared with
  [ADR-027](ADR-027-term-is-change-event.md).
- Until [PLAN-004](https://github.com/j-brent/cpp-semantic-diff/blob/62dc064/docs/plans/PLAN-004-third-party-boundary.md) is built, the code still does what
  ADR-019 decided.
- Analyzing third-party changes is deferred, not rejected.

## Alternatives rejected

- **Warn, or ignore the change.** v0 analyzes changes the repository controls directly. A
  dependency that moves also changes what the project's own code means, as leveldb's tests show.
- **Keep modelling it and rank it last** (ADR-019).
- **Recognize it by folder name.** A guess, and it misses copies under other names.
