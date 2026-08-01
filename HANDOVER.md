# Handover

For the next agent picking this up. Written 2026-08-01, at the end of the first
implementation session. Development moves to **WSL/Linux from here — Windows is no longer a
supported target**, see §3.

---

## 1 · What this is, and what to read

`cpp-semantic-diff` presents code reviewers with the changes that actually matter, instead of
a line diff they have to parse in their head. The thesis: **every useful review signal is a
two-point analysis** — model the codebase at commit A, model it at commit B, diff the models.
The text diff is the degenerate case where the model is an ordered list of lines.

Read in this order. Do not start coding before the first two.

| Doc | Why |
|---|---|
| [`docs/research/semantic-diff-for-review.md`](docs/research/semantic-diff-for-review.md) | Founding document. Twelve questions a reviewer is actually answering, prior art for each, the five pieces to build, staged plan. |
| [`docs/architecture/overview.md`](docs/architecture/overview.md) | **ARCH-001.** Pipeline, IR, identity, delta events, ranking, caching. Code comments cite its section numbers; keep doing that. |
| [`docs/research/cpp-analysis-tool-landscape.md`](docs/research/cpp-analysis-tool-landscape.md) | Tool inventory. Reference material for the evidence layer; skim. |

---

## 2 · Where things stand

Two of eight pipeline stages exist. Both verified against real `fmt` history.

**Done**

- `model/ir.py` — the IR (ARCH §4). `Node`, `Edge`, `Model`, `Completeness`. Scalar-only
  attributes are enforced; `body_hash` is separate from `attrs`; `Location` is provenance and
  provably not identity (there is a fingerprint test for it).
- `acquire/git.py` — revision resolution. Base is `merge-base(main, HEAD)`, three-dot.
- `acquire/workspace.py` — git worktrees cached by SHA, CMake configure, compile-database
  parsing.
- `errors.py` — the precondition taxonomy. ARCH §11: these are hard failures, never degraded
  output.
- `cli.py` — `cppsd plan` and `cppsd acquire`.

**Measured, on `fmt`, `--base HEAD~12`**

```
plan:     11 files, 5 C++ files, 179 lines changed   ← the compression denominator
acquire:  50 C++ TUs of 51 entries, both sides
          12.2 s cold  →  1.2 s warm
```

38 tests, ruff clean. `uv run pytest -q` · `uv run ruff check .`

**Not started:** producer, identity matching, delta, classify, enrich, rank, render, cache.

---

## 3 · Platform: Linux only from here

The owner is continuing in WSL and does not want Windows supported during design and
implementation. Everything below was written on Windows. **None of it is broken on Linux** —
but several pieces carry Windows-motivated complexity that is now dead weight, and two places
are hard-coded to Windows paths and will fail outright.

### Must change — these break on Linux

| Location | Problem |
|---|---|
| `tests/conftest.py:8` | `FIXTURE_ROOT = Path("C:/dev/cpp-demo-repos")`. Make it `CPPSD_FIXTURE_ROOT` env-driven with a sane Linux default. Nothing uses it yet (`require_fixture` has no callers), so this is cheap to fix now and painful later. |
| `pyproject.toml:45` | The `fixtures` pytest marker description hard-codes the same Windows path. |

### Should simplify — Windows-motivated, now unnecessary

| Location | What it is | Recommendation |
|---|---|---|
| `acquire/workspace.py:56-64` | `is_rooted()` checks both path flavours because `Path("/src/a.cpp").is_absolute()` is `False` on Windows. | On Linux `Path.is_absolute()` is sufficient. **Keep anyway** — compile databases travel between hosts and the cost is two lines. Reword the docstring. |
| `acquire/workspace.py:66-71` | `cache_root()` prefers `%LOCALAPPDATA%`. | Drop the Windows branch; use `XDG_CACHE_HOME` then `~/.cache`. Keep the `CPPSD_CACHE_DIR` override. |
| `acquire/workspace.py:29-45` | Generator selection exists because CMake defaults to Visual Studio on Windows, which configures fine and emits **no** compile database. | On Linux the default is `Unix Makefiles`, which does emit one — so this stops being load-bearing. **Keep it anyway**: Ninja configures and builds much faster, and the "configure succeeded but produced nothing" failure mode is worth keeping guarded. Demote the rationale in the comment. |
| `model/ir.py:88` | Comment says "Windows is the primary development platform". | Reword. Separator normalization is still right — compile databases and clang output both emit backslashes on some inputs. |
| `acquire/workspace.py:105` | Comment justifying POSIX `shlex` splitting "even on Windows". | Simplify; POSIX splitting is simply correct now. |
| `tests/test_ir.py:173,205` | Two tests named/documented around Windows behaviour. | Keep the tests — they assert correct cross-platform behaviour. Rename so they read as portability tests, not Windows tests. |

### Docs to update

- `docs/architecture/overview.md:464` — §13 lists "Windows path normalization" as an open
  question. **Strike it.** It is resolved and no longer relevant.
- `docs/architecture/overview.md:120` — the generator rule in §3.1 is framed around Windows.
  Keep the rule, reframe the reason as "the tool must choose a generator it knows emits a
  compile database" rather than "because Windows".

### Line endings

The repo is **clean** — `core.autocrlf=true` on the Windows side meant LF was stored in git,
verified on the committed blobs. A fresh WSL clone gets LF and needs nothing. Consider adding
a `.gitattributes` anyway so this cannot regress:

```
* text=auto eol=lf
```

---

## 4 · WSL setup

```sh
# toolchain
sudo apt install -y cmake ninja-build git
curl -LsSf https://astral.sh/uv/install.sh | sh

# project
git clone <this repo> ~/dev/cpp-semantic-diff && cd ~/dev/cpp-semantic-diff
uv sync
uv run pytest -q
```

**libclang**: the `libclang` PyPI wheel bundles the native library, so `apt install
libclang-dev` is probably unnecessary. If the producer needs a specific LLVM version, point
`clang.cindex.Config.set_library_file()` at it rather than relying on search order — a silent
version mismatch here produces plausible-but-wrong ASTs, which is the worst failure mode this
project has.

**Fixtures.** `fmt` and `spdlog` were unshallowed on the Windows side (7,939 and 4,497
commits). Re-clone them inside the WSL filesystem — **not** `/mnt/c`, where git worktree
operations and per-file I/O are slow enough to distort every timing measurement you take.

```sh
mkdir -p ~/fixtures && cd ~/fixtures
git clone https://github.com/fmtlib/fmt.git
git clone https://github.com/gabime/spdlog.git
```

Full history is required. A shallow clone has no second point and the tool rejects it by
design (`ShallowRepositoryError`). The other five fixtures from the CodeAtlas research (CLI11,
FTXUI, bitcoin, rocksdb, opencv) are only needed for scale testing.

---

## 5 · Next task: the producer

Turn a `Checkout` into a `Model`. This is ARCH §4 and §9 together, and the cache ships **with**
it, not after — see below for why.

**Design constraints already settled. Do not relitigate:**

- **libclang, not clangd.** ARCH §12. Full control over extraction, USRs come free, no
  per-symbol LSP round-trips, no private index format to reverse.
- **Dedup by USR is correctness, not optimization.** Every header is reparsed once per TU. The
  CodeAtlas measurements found 15,656 method cursors collapsing to 161 classes in FTXUI. Get
  this wrong and node counts are inflated by two orders of magnitude.
- **The cache unit is the translation unit, not the commit.** ARCH §9. Key on
  `hash(TU content, transitive header content, flags, producer version)`. Assemble a commit's
  model from cached partials plus a reparse of only the invalid ones. Compute the invalidation
  set with `clang-scan-deps` (measured cheap: opencv's 1,292 TUs in 14.1 s).
- **Populate `Node.symbol` from the start.** It is the join key to `abidiff`/`bloaty`/`nm`.
  The evidence layer is deferred, but retrofitting this invalidates every cached model.
- **Fill in `Completeness` honestly.** Every TU that fails to parse is recorded with a reason.
  ARCH §11 — a tool that silently degrades is worse than no tool.

**The number to beat:** the CodeAtlas research measured FTXUI's 76 TUs at **137 s** through
Python libclang bindings, uncached. `fmt` has 50 TUs. Doubling that per review is not a
product, which is the entire reason the cache is not a later optimization. ARCH §9 sets the
budget: **a review touching ≤ 20 TUs completes in under 30 s warm.**

**v0 extraction scope** (ARCH §12): namespace / class / function / method / field nodes;
`contains`, `calls`, `inherits`, `overrides` edges. Nothing else yet.

**After the producer**, in order: identity matching (tier 1 only), the delta, then the
compression metric end to end. The first moment this project proves or disproves itself is
when `2,431 lines changed → 9 events` prints on a real `fmt` branch. Build toward that print
statement, not toward feature completeness.

---

## 6 · Decisions made — with the reason, so you can tell if it still holds

| Decision | Why |
|---|---|
| Python, standalone repo | CodeAtlas C++ is TypeScript only because it began as a VS Code extension. Ideas carry over from it, not modules. |
| Base is `merge-base(main, HEAD)` | Against main's tip, every change other people merged while the branch was open gets attributed to this branch. There is a test for this; keep it. |
| Compile database required at **both** revs | Hard precondition, not a degradation path. Configure is seconds, so it is affordable. |
| Base that does not build ⇒ hard failure | The base is main. A main that does not build is a different problem, not this tool's. |
| JSON is the product, terminal is a consumer | Keeps delivery surfaces out of the analysis core — the mistake that made CodeAtlas TypeScript. |
| v0 ships **tier-1 identity only** | Renames will look bad on purpose. Measure the damage before building tiers 2–4. That measurement is a v0 deliverable. |
| Identity tiers are one-to-one | Splits and merges surface as unmatched residue rather than being forced into a match. Confident-and-wrong is the failure mode to avoid. |
| Cascade collapse at render, never at diff | JSON always carries the full event set; the view hides consequences under causes. When cause attribution is uncertain, show rather than hide. |

---

## 7 · Still open

- **Generated sources.** ARCH §3.1 states the problem, not the policy. Configure-only is the
  v0 design; `clang-scan-deps` failed on 119 of opencv's 1,292 TUs (9.2 %) against an unbuilt
  tree, all on headers CMake emits during the build. `fmt` and `spdlog` will not reveal where
  the threshold is.
- **Tier-4 similarity threshold.** Git's `-M` has one knob and decades of tuning. Assume this
  needs per-repository calibration; design the knob in early.
- **Where the reviewer actually reads this.** CLI is right for v0 and probably wrong as the
  product. The JSON-first contract defers the decision without prejudging it.

---

## 8 · Conventions

- **Cite ARCH sections in code comments** where a non-obvious choice traces to a design
  decision (`# ARCH-001 §4.3`). The existing code does this consistently; it is how the
  reasoning survives.
- **Docs carry a metadata block** — `id`, `type`, `status`, `author`, `created`,
  `dependencies`. IDs are `RESEARCH-00x` and `ARCH-00x`, unique, never recycled.
- **Commit messages** say what changed and *why it matters*, not just what. Record findings
  discovered by running the code — two are already recorded that way and both were real bugs
  in waiting.
- **Tests assert the design, not the implementation.** The merge-base test exists because
  getting it wrong is invisible in output; the fingerprint test exists because "a moved entity
  is the same entity" is a claim ARCH makes. Prefer tests of that kind.
- Run `uv run ruff format . && uv run ruff check . && uv run pytest -q` before committing.
