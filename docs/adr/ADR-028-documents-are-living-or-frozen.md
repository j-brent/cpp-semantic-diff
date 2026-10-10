<!-- metadata
id: ADR-028
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
-->

# ADR-028 · Documents are living or frozen

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

The 2026-10-10 documentation pass had to correct 32 files, because current state, history and
plans were mixed in the same documents and repeated across them; open work sat in five places. The
living tracker had drifted by omission: a fix landed and its line stayed
([v0 tracker §6](../../reports/2026-08-01--v0-build/STATUS.md#6--known-defects-and-gaps)).
Recorded in `5d06840` and `bd8e87f`.

## Decision

Every document is living (kept true as the code changes) or frozen (one moment, never edited), and
says which. The rules are in [`CONTRIBUTING.md`](../../CONTRIBUTING.md#documents).

- The v0 tracker `STATUS.md` is frozen as
  [`reports/2026-08-01--v0-build/STATUS.md`](../../reports/2026-08-01--v0-build/STATUS.md) and
  replaced by [`ROADMAP.md`](../../ROADMAP.md).
- Code comments cite only frozen documents or ARCH-001, whose section numbers are permanent.

## Consequences

- No two living documents hold the same fact, and measured numbers live in frozen reports.
- A removed ARCH-001 section leaves a stub. `tests/test_doc_citations.py` fails on a citation of a
  missing section or of another living document.

## Alternatives rejected

- **One living tracker** holding state, history and reasons together, as `STATUS.md` did.
