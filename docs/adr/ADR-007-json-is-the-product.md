<!-- metadata
id: ADR-007
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-007 · JSON is the product; the terminal is a consumer

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

CodeAtlas C++ is TypeScript because its delivery surface, a VS Code extension, shaped its core.
Where a reviewer will finally read this tool's output is not known. Recorded in `49d030b`
([ARCH-001 §10](../ARCH-001-architecture.md#10--output-contract)).

## Decision

The product is a versioned JSON document. The terminal view, and any later view, consumes it.
`render`'s signature admits no `Model`.

## Consequences

- Delivery surfaces stay out of the analysis core, and the choice of surface stays open.
- The first check on anything seen in the terminal is the same change event in the JSON: the view
  is lossy on purpose ([v0 tracker §5](../../reports/2026-08-01--v0-build/STATUS.md#5--findings-worth-keeping)).
- Renaming a JSON field changes the format and needs a new schema version.

## Alternatives rejected

- **A view as the core,** as CodeAtlas did with its extension.
