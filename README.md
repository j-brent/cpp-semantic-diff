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

The pipeline runs end to end. Seven of eight stages exist; `enrich` (the evidence layer) is deferred
past v0 by design — see [`STATUS.md`](STATUS.md) for the tracker and the known defects.

| Stage | State |
|---|---|
| `acquire` — worktrees + compile databases at both revs | done |
| `produce` — worktree → `Model`, cached per TU, parsed in parallel | done |
| `match` — two models → `Correspondence` | done, tier 1 only |
| `diff` · `classify` · `rank` · `render` | done |
| `enrich` — evidence adapters | post-v0 |

What it prints on real repositories:

```
fmt     HEAD~12    179 lines changed  →  16 semantic events, 2 high-risk
spdlog  HEAD~30    1,708 lines changed →  32 semantic events, 5 high-risk
spdlog  HEAD~5     35 lines changed   →   0 semantic events
leveldb HEAD~10    52 lines changed   → 134 semantic events, 5 high-risk
                                         + 3,660 in bundled dependencies, ranked last
```

The spdlog `HEAD~5` row validates the others: thirty-five lines of CMake, comments and a
Windows-only header, correctly reported as no semantic change — with the reason printed rather than
implied.

The leveldb row is the one that argues against the thesis, and it is here for that reason. Measured
compression is **0.4:1 to 53:1** — that commit *decompresses*, turning 52 lines into 134 things to
read, because bumping googletest moved a great deal of test code under it. Compression is real on
some commits and inverted on others, and which is which appears to depend on repository shape more
than on the tool: the two libraries that agree are both header-only and vendor by copying. See
[`STATUS.md`](STATUS.md) §2.

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
producer id      libclang 18.1.3
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

A review touching ≤ 20 translation units completes in under 30 s on a warm cache; a cold `fmt`
model is ~25 s across 8 workers. The first run against a new base pays for it and every later
review against that base does not.

| Environment variable | Effect |
|---|---|
| `CPPSD_LIBCLANG` | Use this library instead of discovering one |
| `CPPSD_RESOURCE_DIR` | Override the clang resource directory |
| `CPPSD_SCAN_DEPS` | Use this `clang-scan-deps` instead of discovering one |
| `CPPSD_WORKERS` | Parser processes (default: cores, capped at 8). `1` takes the serial path |
| `CPPSD_CACHE_DIR` | Where worktrees and cached models live (default: `$XDG_CACHE_HOME`) |
| `CPPSD_FIXTURE_ROOT` | Where the test fixture clones live (default: `~/fixtures`) |

Development: `uv run ruff format . && uv run ruff check . && uv run pytest -q`. Tests marked
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
something the first two cannot — see [`STATUS.md`](STATUS.md) §3.

## Documents

Read the first two before writing code.

| Doc | What it is |
|---|---|
| [`docs/research/semantic-diff-for-review.md`](docs/research/semantic-diff-for-review.md) | **RESEARCH-002**, the founding document. The twelve questions a reviewer is actually trying to answer, the artifact and prior art for each, what this tool must build, the hard problems, and a staged plan. |
| [`docs/architecture/overview.md`](docs/architecture/overview.md) | **ARCH-001.** Pipeline, IR, identity, delta events, ranking, caching, performance budget. Code comments cite its section numbers; keep doing that. |
| [`docs/research/cpp-analysis-tool-landscape.md`](docs/research/cpp-analysis-tool-landscape.md) | **RESEARCH-001.** Inventory of C++ analysis tooling across source, IR, object and binary levels. The supply side; reference material for the evidence layer. |
| [`STATUS.md`](STATUS.md) | What is built, what is next, and the decisions already settled — with the reason for each, so you can tell whether it still holds. |
| [`docs/plans/rename-identity.md`](docs/plans/rename-identity.md) | **PLAN-001.** The work in flight: renames and the defining TU. Design and the measurement that decides whether it worked. Deleted when it lands. |

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
