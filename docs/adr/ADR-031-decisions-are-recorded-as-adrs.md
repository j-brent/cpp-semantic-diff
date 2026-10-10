<!-- metadata
id: ADR-031
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
-->

# ADR-031 · Decisions are recorded as ADRs

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

Decisions were a table in the v0 tracker
([§8](../../reports/2026-08-01--v0-build/STATUS.md#8--decisions--with-the-reason-so-you-can-tell-if-it-still-holds)),
copied into the roadmap's list of decisions in force. Others existed only in tracker prose or in
plans. A table row cannot hold its context, its rejected alternatives and its supersession. Not yet
in a commit.

## Decision

Every decision that constrains the design or the process is an architecture decision record in
`docs/adr/`, one per file, numbered in the order the decisions were made. The rules are in
[`CONTRIBUTING.md`](../../CONTRIBUTING.md#decisions). This replaces the tracker's §8 table and the
roadmap's decision list.

## Consequences

- An ADR is frozen once accepted. A changed decision gets a new ADR, and the old one's status
  line says so.
- The decisions in force are the accepted ADRs; there is no separate list.
- Other documents link to an ADR rather than restating its reasons. Code comments may cite ADRs.

## Alternatives rejected

- **A decision table in a living document,** as before.
