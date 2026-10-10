<!-- metadata
id: ADR-027
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
-->

# ADR-027 · The term is "change event", not "event"

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

"Event" alone does not say what happened. The tool uses it in the JSON fields (`events`,
`events_reported`, `top_level_events`, `vendored_events`), the terminal output, the code (`Event`,
`EventKind`) and the design documents. Recorded in `b934fc1` as known issue 3.

## Decision

The term is "change event". Renaming the JSON fields changes the output format, so the rename waits
for the next output-format change, and shares one schema version with
[PLAN-004](../plans/PLAN-004-third-party-boundary.md#2--decisions)
([ADR-025](ADR-025-third-party-code-must-not-change.md)).

## Consequences

- Prose uses "change event" now; the tool's output and code keep "event" until then.

## Alternatives rejected

- **Rename now,** in a schema version of its own: two format changes where one will do.
