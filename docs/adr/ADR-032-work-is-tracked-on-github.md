<!-- metadata
id: ADR-032
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
-->

# ADR-032 · Work is tracked on GitHub

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

Open work sat in five places before 2026-10-10, and a tracker in a document drifts by omission
([ADR-028](ADR-028-documents-are-living-or-frozen.md)). `ROADMAP.md` then held the one list of open
work, with each item's full text and status. Not yet in a commit.

## Decision

Work is tracked on GitHub: issues, a v0 milestone and a project board. Status lives there.
`ROADMAP.md` keeps only oversight information that GitHub does not hold: scope, order and the
reasons for it, and links.

## Consequences

- The confirmed issues move from `ROADMAP.md` to GitHub issues.
- `ROADMAP.md` links to issues rather than holding their text.

## Alternatives rejected

- **Keep the work list in `ROADMAP.md`.** Status kept in a document repeats GitHub and drifts.
