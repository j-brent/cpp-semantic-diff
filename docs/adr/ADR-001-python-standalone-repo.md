<!-- metadata
id: ADR-001
type: adr
kind: frozen
status: accepted
decided: 2026-08-01
author: Brent Runyan
-->

# ADR-001 · The tool is written in Python, in its own repository

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-01.

## Context

CodeAtlas C++, an earlier project, already models C++ code. It is TypeScript only because it began
as a VS Code extension: an accident of delivery, not a modelling requirement. Recorded in `e4cafac`.

## Decision

The tool is Python, in a standalone repository. What carries over from CodeAtlas is designs, not
modules: the shape of the graph IR, the relation ontology, the CMake file-API approach to build
metadata, and scope projection.

## Consequences

- No CodeAtlas code is reused; its ideas are reimplemented.
- The tool does not live inside the `dev-team` repository.

## Alternatives rejected

- **Extend or extract CodeAtlas's TypeScript code.** Its language was chosen for a delivery
  surface this tool does not have.
- **A `dev-team` submodule.**
