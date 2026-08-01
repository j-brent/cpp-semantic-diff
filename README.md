# cpp-semantic-diff

Meaningful diffs for C++ code review.

A reviewer reading a unified diff is doing compiler work by hand — parsing changed lines,
reconstructing context from memory, and comparing two mental models at human error rates. A line
is not a unit of meaning. This tool aims to present the changes that actually determine whether a
change is correct, complete, non-breaking, and structurally sound, and to say plainly which parts
of the diff are noise.

The core claim: **every useful review signal is a two-point analysis** — build a semantic model at
commit A, build it at commit B, diff the models. The text diff is the degenerate case where the
model is an ordered list of lines.

## Status

Pre-implementation. Research only; nothing is built yet.

## Documents

| Doc | What it is |
|---|---|
| [`docs/research/semantic-diff-for-review.md`](docs/research/semantic-diff-for-review.md) | Founding document. The twelve questions a reviewer is actually trying to answer, the artifact and prior art for each, what this tool must build, the hard problems, and a staged plan. |
| [`docs/research/cpp-analysis-tool-landscape.md`](docs/research/cpp-analysis-tool-landscape.md) | Inventory of C++ analysis tooling across source, IR, object and binary levels, classified by use case. The supply side. |

## Relationship to CodeAtlas C++

Both research notes were written during work on
[CodeAtlas C++](https://github.com/j-brent/codeatlas-cpp) and moved here when it became clear this
is a separate product rather than a feature of that one. CodeAtlas answers *what is this codebase*
— a one-point question. This tool answers *what did this change do* — a two-point question with
different identity, caching, and presentation requirements.

CodeAtlas components worth extracting rather than rewriting: the graph IR and its JSON Schema, the
clangd-backed producer, and the CMake file-API provider. See §5 of the founding document.
