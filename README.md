<!-- metadata
type: readme
kind: living
author: Brent Runyan
created: 2026-08-01
-->

# cpp-semantic-diff

*Living document: kept true as the code changes.*

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

The pipeline runs end to end. Seven of eight stages exist; `enrich` (the evidence layer) is deferred
past v0 by design. Where things stand, the known problems and the work ahead are in
[`ROADMAP.md`](ROADMAP.md).

| Stage | State |
|---|---|
| `acquire` — worktrees + compile databases at both revs | done |
| `produce` — worktree → `Model`, cached per TU, parsed in parallel | done |
| `match` — two models → `Correspondence` | done: tier 1, inline namespace, name |
| `diff` · `classify` · `rank` · `render` | done |
| `enrich` — evidence adapters | post-v0 |

What the tool reports on real repositories, and how much it compresses a review, is measured in the
[v0 tracker, §2](reports/2026-08-01--v0-build/STATUS.md#2--the-measurement-this-project-exists-to-take).
Compression is real on some commits and inverted on others.

v0 requires third-party code to be identical at both points
([ARCH-001 §13](docs/ARCH-001-architecture.md#13--v0)). The tool does not enforce this yet
([PLAN-004](docs/plans/PLAN-004-third-party-boundary.md)).

## Requirements

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/)
- **A system LLVM, 16 or newer, with its builtin headers** — the `-dev` package, not just the
  runtime library
- **CMake and Ninja**, to configure the repository under analysis
- **Git with full history** at both revisions; a shallow clone has no second point and is rejected

The libclang bundled in the PyPI wheel is deliberately *not* used. It ships the native library
without the compiler's builtin headers, so `#include <stddef.h>` fails, and libclang responds by
returning a **truncated AST and raising nothing**. Both models would then omit the same
declarations and agree with each other — confidently wrong output, which is the one failure mode
this project exists to argue against. The wheel is kept for its Python bindings only; the library
is discovered on the system and probe-verified before any model is built.

```sh
sudo apt install -y llvm-18 libclang1-18 libclang-common-18-dev cmake ninja-build git
# Fedora/RHEL: clang-devel llvm-devel · Arch: clang · macOS: brew install llvm
```

## Quickstart

```sh
git clone https://github.com/j-brent/cpp-semantic-diff && cd cpp-semantic-diff
uv sync
uv run cppsd doctor
```

`doctor` is the first thing to run on any new host. It proves the parser can reach its builtin
headers before you trust a single result:

```
libclang         /usr/lib/llvm-18/lib/libclang.so.1
version          Ubuntu clang version 18.1.3 (1ubuntu1)
builtin headers  parsed clean
target           x86_64-pc-linux-gnu
producer id      libclang 18.1.3 x86_64-pc-linux-gnu
```

Then, against a repository with full history:

```sh
uv run cppsd plan    ~/fixtures/fmt --base HEAD~12   # what a reviewer reads today
uv run cppsd acquire ~/fixtures/fmt --base HEAD~12   # both revisions, configured
uv run cppsd diff    ~/fixtures/fmt --base HEAD~12   # the whole pipeline
uv run cppsd diff    ~/fixtures/fmt --base HEAD~12 --json --out delta.json
```

`diff` prints ranked events with consequences folded under their causes; `--expand` shows the flat
set. The JSON is the product and always carries every event uncollapsed — the terminal is one
consumer of it, and any incompleteness in either model is stated before the events, not in a log.

Models are cached per translation unit, so the first run against a new base pays for parsing and
every later review against that base does not. Timings are in the
[v0 tracker, §4](reports/2026-08-01--v0-build/STATUS.md#4--measured-performance). `diff --workers N` (`-j N`) sets how many parser processes
that is for one run; `$CPPSD_WORKERS` sets it for every run, and the flag wins where both are
given.

| Environment variable | Effect |
|---|---|
| `CPPSD_LIBCLANG` | Use this library instead of discovering one. A path that is not a usable library is refused, never replaced |
| `CPPSD_RESOURCE_DIR` | Override the clang resource directory. An escape hatch: it turns the per-TU cache off unless the include closure already covers the directory |
| `CPPSD_SCAN_DEPS` | Use this `clang-scan-deps` (a path) instead of discovering one. If it is unusable the per-TU cache is off, and the run says why |
| `CPPSD_WORKERS` | Parser processes when `--workers` is absent (default: cores, capped at 8). `1` takes the serial path |
| `CPPSD_CACHE_DIR` | Where worktrees and cached models live (default: `$XDG_CACHE_HOME/cpp-semantic-diff`, else `~/.cache/cpp-semantic-diff`). A relative `XDG_CACHE_HOME` is ignored |
| `CPPSD_RENAME_SIMILARITY` | Git's rename-detection threshold, a percentage (default: 50, git's own `-M` default) |
| `CPPSD_FIXTURE_ROOT` | Where the test fixture clones live (default: `~/fixtures`) |

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Internal error: a bug in `cppsd`, reported with its traceback |
| 2 | Bad command line, option value or environment setting |
| 3 | The repository or revisions cannot be compared |
| 4 | No usable compile database at a revision |
| 5 | No trustworthy parser |
| 6 | An operation failed while running: git, a worker pool, writing output |

Development conventions are in [`CONTRIBUTING.md`](CONTRIBUTING.md). Tests marked
`fixtures` need real clones under `CPPSD_FIXTURE_ROOT`; they skip without them.

```sh
cd ~/fixtures
git clone https://github.com/fmtlib/fmt
git clone https://github.com/gabime/spdlog
git clone --recurse-submodules https://github.com/google/leveldb
```

The third is not more of the same. fmt and spdlog are both effectively header-only and spdlog
vendors fmt, so between them they cover one shape; leveldb is `.cc`-based with real internal
linkage, keeps its tests beside the code they test, and pins submodules. Each of those exercises
something the first two cannot — see the
[v0 tracker, §3](reports/2026-08-01--v0-build/STATUS.md#3--the-v0-identity-measurement--a-negative-result).

## Documents

Every document says whether it is **living** (kept true as the code changes) or **frozen**
(describes one moment, never edited). [`CONTRIBUTING.md`](CONTRIBUTING.md) has the rules.

| Doc | Kind | What it is |
|---|---|---|
| [`ROADMAP.md`](ROADMAP.md) | living | Scope, the order of work and the reasons for it, with links to the work on GitHub. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | living | Conventions for code, comments, documents and commits. |
| [ARCH-001](docs/ARCH-001-architecture.md) | living | The design: pipeline, IR, identity, delta events, ranking, caching, performance budget. |
| [`docs/plans/`](docs/plans/) | living while active | One plan per roadmap item being worked on, deleted when done. |
| [RESEARCH-002](docs/research/RESEARCH-002-semantic-diff-for-review.md) | frozen | The founding document: the twelve questions a reviewer is trying to answer, the artifact and prior art for each, what this tool must build, the hard problems, and a staged plan. Read it before writing code. |
| [RESEARCH-001](docs/research/RESEARCH-001-tool-landscape.md) | frozen | Inventory of C++ analysis tooling across source, IR, object and binary levels. Reference for the evidence layer. |
| [`reports/`](reports/) | frozen | Reviews and measurements, one folder per date and topic. [`2026-08-01--v0-build/`](reports/2026-08-01--v0-build/STATUS.md) is the tracker of the original implementation; [`2026-10-10--up-to-speed/`](reports/2026-10-10--up-to-speed/project-review.md) is the review that produced the roadmap's confirmed work. |

## Relationship to CodeAtlas C++

Both research notes were written during work on
[CodeAtlas C++](https://github.com/j-brent/codeatlas-cpp) and moved here when it became clear this
is a separate product rather than a feature of that one. CodeAtlas answers *what is this codebase*
— a one-point question. This tool answers *what did this change do* — a two-point question with
different identity, caching, and presentation requirements.

CodeAtlas is TypeScript because it began as a VS Code extension — an accident of delivery, not a
modelling requirement. This tool is Python, so what carries over is designs, not modules: the shape
of the graph IR, the relation ontology, the CMake file-API approach to build metadata, and scope
projection. See §5 of the founding document.
