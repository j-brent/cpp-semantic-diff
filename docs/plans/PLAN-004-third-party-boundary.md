<!-- metadata
id: PLAN-004
type: plan
status: not started
author: Brent Runyan
created: 2026-10-10
dependencies: [ARCH-001]
kind: living
-->

# Third-party code is outside the model

*Living document: kept current until it is done, then deleted.*

Closes [#3](https://github.com/j-brent/cpp-semantic-diff/issues/3), with [#4](https://github.com/j-brent/cpp-semantic-diff/issues/4) in the same schema change.

Delete this file when every step below is done, the measurements are in a frozen report, and
#3 and #4 are closed.

---

## 1 · The prerequisite

Third-party code must not change between the two points. The prerequisite is recorded in
[ARCH-001 §13](../ARCH-001-architecture.md#13--v0). This plan makes the tool enforce it.

The tool already draws this boundary around code outside the repository: the standard library,
system headers and libc. That code is parsed but never modeled, and relationships into it are
dropped and counted. This plan moves third-party code inside the repository to the same side of
the boundary.

---

## 2 · Decisions

Decided 2026-10-10.

| Question | Decision |
|---|---|
| What counts as third-party | **Git submodules**, automatically. **Paths the user declares**, for libraries copied in. Folder names are only a hint and never exclude anything. |
| Third-party code changed between the two points | **Refuse.** v0 analyzes changes the repository controls directly. Third-party changes are deferred, not ignored. |
| Third-party entities in the model | **None.** Relationships into third-party code are boundary crossings, like calls into `std::`. |
| Boundary crossings | **Recorded, with a label naming where they lead.** For visualization only: the comparison and the ranking never read them. |
| Output format | **One schema version change**, shared with [#4](https://github.com/j-brent/cpp-semantic-diff/issues/4) ("event" → "change event"). |

Boundary crossing labels:

| Callee declared in | Label |
|---|---|
| Declared third-party path or submodule | that path, e.g. `test/gtest` |
| Outside the repository | the library's name, e.g. `C++ standard library`, `boost` (§3.3) |
| Nowhere (compiler-generated) | `implicit` |

This replaces the [v0 tracker](../../reports/2026-08-01--v0-build/STATUS.md)'s §8 decision "Vendored code is partitioned out of the ranking, not
merely down-weighted."

---

## 3 · Design

### 3.1 · Recognizing third-party code

The third-party set for a comparison is the union of:

- **Submodule paths** recorded in `.gitmodules` at the base or the head revision, read with
  `git config --blob <rev>:.gitmodules`.
- **Declared paths**, from a new repeatable `cppsd diff --third-party PATH` option. They are
  repository-relative directories.

A declared path that exists in neither revision is refused. It is almost certainly a typo, and a
typo would silently leave a library in the model.

**Folder-name hints.** The `VENDOR_SEGMENTS` names stay in use for one purpose only: a tracked
directory that matches one and is not in the third-party set produces a note in the output:
"`test/gtest` looks like third-party code; pass `--third-party test/gtest` if it is." Add
`bundled` to the list. spdlog vendors fmt under `include/spdlog/fmt/bundled`, which the current
list misses, so today it is modeled as spdlog's public API.

**Untracked files inside the worktree.** The build directory lives inside the worktree
(`acquire/workspace.py` configures into `<worktree>/build`). It can hold generated headers and
FetchContent downloads (`build/_deps`). These are not contained in the repository. **Decided**
([ADR-037](../adr/ADR-037-only-tracked-files-are-modelled.md)): only files git tracks at that revision are modeled, and everything else gets
the label `untracked`. No fixture has an untracked node
([step 1 report §2](../../reports/2026-10-10--plan-004-baseline/measurements.md#2--nodes-per-label)).

### 3.2 · Refusing a changed third-party dependency

After the revisions are resolved and before any worktree is built, compare the third-party set
against `file_changes` (`acquire/git.py`). A submodule bump is a modified gitlink at the
submodule's path; a copied library shows its own changed files. Any hit raises a new
`PreconditionError` subclass. The error names each changed third-party path and says that v0
does not analyze changes to third-party code.

This runs before worktree creation so that the refusal is immediate and costs no parsing.

### 3.3 · Extraction

`produce/extract.py` currently prunes a cursor when `_relative` returns `None` (outside the
repository). Extend that prune to third-party paths and to untracked
files ([ADR-037](../adr/ADR-037-only-tracked-files-are-modelled.md)). Also:

- **Third-party translation units are not parsed.** Compile-database entries whose source is
  third-party are excluded in `build_model`, with a note stating the count. This mirrors how
  non-C++ entries are handled.
- **Crossings are recorded during parsing.** Calls (`visit_body`), base classes (`visit_record`)
  and overrides already look up the referenced cursor. When its file is outside the model,
  record a crossing (caller, callee qualified name, label) instead of an edge. The label is
  known at that moment, so there is no guessing afterward.
- **Naming a library outside the repository.** Take the callee's header path relative to the
  include directory that found it: the TU's `-I`/`-isystem` directories plus the toolchain's
  default search directories, discovered once with the toolchain.
  - under the compiler's builtin header directory → `compiler builtins`;
  - under `c++/<version>` or `c++/v1` → `C++ standard library`;
  - a C standard or POSIX header (a fixed list), directly in a search directory or its
    architecture subdirectory (`x86_64-linux-gnu`) → `C library`;
  - inside a subdirectory → that subdirectory's name (`boost`, `openssl`);
  - any other header directly in a search directory → the header's name without its suffix
    (`zlib`).

  A wrong label costs nothing beyond the picture it appears in, because no analysis reads it.
- **`implicit` is assigned at model build.** An edge still unresolved when the model is
  assembled (`produce/build.py`) points to an entity inside the model's files that no declaration
  produced. The working claim is that these are all compiler-generated members. Step 1 checks
  that claim. If anything else turns up, it gets its own label rather than being called
  `implicit`. **Measured:** the claim does not hold. Lambda members, members of local classes,
  template instantiations whose alias reaches no node, builtins and a few others are also
  unresolved ([step 1 report §3](../../reports/2026-10-10--plan-004-baseline/measurements.md#3--unresolved-in-repository-callees)).
- **The cache key includes the third-party set.** It is a runtime input that changes what
  extraction keeps, so `TuCache.identity` must fold it in. Otherwise a TU cached with one set is
  served under another. `_producer_code_digest` already covers the code change itself.

### 3.4 · The model

- `Model` gains a `crossings` collection. It is deduplicated the same way edges are: one entry
  per caller, callee and kind, with every site location.
- `Completeness.edges_dropped` becomes per-label counts. The class docstring's distinction
  between a coverage gap and the boundary stays. Crossings are the boundary, now itemized.
- `match`, `diff`, `classify` and `rank` do not read `crossings`. A test asserts it: adding or
  removing crossings leaves the change events and the ranking identical.

### 3.5 · Ranking and output

Removed:

- `Exposure.VENDORED`, its weight, and the vendored partition in `rank/ranker.py`;
- the vendored refusal in `rank/components.py`, and `score.py`'s notes about vendored edges;
- `Ranking.vendored_count`, `first_party_count` (it becomes the total), and the
  "… more in bundled dependencies" text in `render/build.py` and `render/terminal.py`;
- `vendored_events` in the JSON.

Added to the JSON:

- the third-party set and where each entry came from (submodule or declared);
- crossing counts per label, and the crossings themselves;
- folder-name hints as structured notes, not prose.

`SCHEMA_VERSION` becomes `2.0`. #4 lands under the same `2.0` before either change is
merged to `main`.

`rank/exposure.py` is left with TEST, INTERNAL and PUBLIC. Its module notes about vendored
detection move to the third-party code, since recognizing third-party code is no longer a
ranking question.

---

## 4 · Steps

Each step is one commit, and the suite passes after each one.

1. **Measure before changing.** On fmt `HEAD~12`, spdlog `HEAD~30` and leveldb `HEAD~9..~1`:
   - nodes per label the new rule would assign (third-party, untracked, first-party);
   - the unresolved in-repository callees, to check that they are all compiler-generated.

   Record the numbers here. Decide §3.1's untracked question from them.
   **Measured** in a [frozen report](../../reports/2026-10-10--plan-004-baseline/measurements.md), with the baseline outputs §5 compares against.
   The untracked question is decided in [ADR-037](../adr/ADR-037-only-tracked-files-are-modelled.md).
2. **The third-party set.** Submodule discovery, `--third-party`, the missing-path refusal and
   folder-name hints. Not wired into extraction yet. Tests use temporary git repositories, like
   `tests/test_git.py`.
   **Built** in `acquire/third_party.py`. Decided 2026-10-11:
   - `bundled` is added to a hint-only list, `HINT_SEGMENTS = VENDOR_SEGMENTS | {"bundled"}`.
     Ranking still reads `VENDOR_SEGMENTS` until step 5. Step 5 moves the list there.
   - A hint is the topmost tracked directory, at base or head, whose own name matches and that
     is neither in, under nor above a set entry. leveldb's `third_party` holds only its
     submodules, so it gets no hint.
   - A submodule counts at a revision only where that revision has a gitlink at its path. A
     stale `.gitmodules` entry is left out, without a note.
   - The set and hints are in the JSON now (`third_party`, `third_party_hints`), under
     `2.0.dev`, and in the terminal view. Step 6 adds only the crossing counts and the demo.
3. **The refusal.** The new precondition error, checked before checkout. Tests: a submodule bump
   is refused; a change inside a declared path is refused; a change next to a declared path is
   not.
4. **Extraction boundary and crossings.** Prune, skip third-party TUs, record crossings, assign
   `implicit`, add the set to the cache key. Tests: a fixture with a declared library yields no
   nodes from it and labeled crossings into it; changing the set invalidates the cache.
5. **Remove vendored from ranking and rendering.** Tests in `test_rank.py` and `test_render.py`
   that assert the partition are deleted or rewritten, not loosened.
6. **Schema 2.0 and the demo.** Add `--third-party test/gtest` for fmt and
   `--third-party include/spdlog/fmt/bundled` for spdlog to `.github/workflows/demo.yml`, and
   replace the `vendored_events` row in its summary table with crossing counts.
   The rest of [#4](https://github.com/j-brent/cpp-semantic-diff/issues/4) lands here too; the
   code and terminal output already say "change event":
   - JSON fields: `events` → `change_events`, `summary.events_reported` →
     `summary.change_events_reported`, `summary.top_level_events` →
     `summary.top_level_change_events`; `summary.vendored_events` is removed by step 5, not
     renamed.
   - The demo summary table: the jq paths `$s.events_reported` and `$s.top_level_events`, and
     the labels "semantic events (numerator)" → "change events (numerator)" and "lines per
     event" → "lines per change event".
7. **Documents.** [`ROADMAP.md`](../../ROADMAP.md) (the order, once #3 is done), the
   `exposure.py` docstring, `README.md`, and a frozen report with the measurements from §5.

---

## 5 · What "done" looks like

| Check | Expected |
|---|---|
| fmt `HEAD~12`, `--third-party test/gtest` | runs; no node under `test/gtest`; crossings labeled `test/gtest`; first-party change events identical to a run before this work, since gtest is static in this range |
| spdlog `HEAD~30`, `--third-party include/spdlog/fmt/bundled` | runs; bundled fmt is no longer modeled; first-party change events unchanged |
| spdlog `HEAD~30`, no option | runs, and notes that `include/spdlog/fmt/bundled` looks like third-party code |
| leveldb `HEAD~10` | **refused**, naming `third_party/googletest` and `third_party/benchmark` |
| leveldb `HEAD~9..~1` | runs; the 49 head-side unmatched entities, all in googletest (v0 tracker §3), are gone |
| `uv run pytest -q`, `ruff format --check`, `ruff check` | clean |

If a "change events unchanged" row is not unchanged, the difference is the finding. Explain it
before going further.

---

## 6 · Not covered

- **Analyzing third-party changes.** Deferred past v0 by decision. The refusal message is where
  a future version picks this up.
- **Exact library identity**, such as which package or version provided a header. The label is
  a name for display, not a dependency record.
- **A config file in the repository** listing third-party paths. The command-line option is
  enough for v0 and works on repositories we cannot edit.
