# Status

Living tracker: what is built, what is next, and what was already decided. Updated at the end of
each working session. Started 2026-08-01 as a handover note; kept as a tracker because the
*reasons* behind the settled decisions are the part worth carrying forward.

Setup, requirements and the commands that work today are in [`README.md`](README.md). Design is
[ARCH-001](docs/architecture/overview.md); this file is state, not design.

---

## 1 · Pipeline

Eight stages (ARCH §3). **Seven exist; `enrich` is deferred post-v0 by design.**

- [x] **acquire** — two revs → two worktrees + compile databases
- [x] **produce** — worktree + compile db → `Model`
  - [x] toolchain: discover a system libclang and prove it before use
  - [x] cursor traversal and node extraction
  - [x] USR dedup, within and across translation units
  - [x] edges: `contains`, `calls`, `inherits`, `overrides`
  - [x] per-TU cache keyed on content + headers + flags + producer version
  - [x] parallel parsing — what makes the §9 budget reachable
  - [x] `Completeness` filled in from real parse failures
- [x] **match** — two `Model`s → `Correspondence` (tier 1 only, deliberately)
- [x] **diff** — → `Event` list
- [x] **classify** — cause and cascade links
- [ ] **enrich** — evidence adapters *(post-v0; blast radius shipped inside `rank`)*
- [x] **rank** — risk scores and decomposition
- [x] **render** — JSON document, terminal as a consumer

`uv run cppsd diff <repo> --base <ref>` runs the whole thing.

---

## 2 · The measurement this project exists to take

```
fmt    HEAD~12    179 lines changed  →  16 semantic events, 4 high-risk
                  7 top-level, 9 nested under a cause · 5 components
fmt    HEAD~100   3,178 lines changed → 335 semantic events
                  56 top-level, 279 nested · 12 components
spdlog HEAD~30    1,708 lines changed → 32 semantic events
                  24 top-level, 8 nested · 15 components
spdlog HEAD~5     35 lines changed   →  0 semantic events
```

**The verdict, without spin.** Compression is real but roughly an order of magnitude weaker than
ARCH §10's illustrative *2,431 → 9* (270:1). Measured: **9.5:1 to 53:1** on `events_reported`.

The number that actually reads as dramatic is **top-level events after cascade collapse** — 3,178
lines down to 56 things to read. If the compression line is to be the headline, its honest form is
two numbers. It currently prints the weaker one first on purpose: the numerator must never quietly
become the collapsed count, because that is the one number in this product nobody can audit from
the outside.

The spdlog `HEAD~5` row is the one that validates the rest. Thirty-five lines of CMake, comments and
a Windows-only header produce zero events, *correctly* — and the run prints the §11 limit that says
why, rather than implying the change was empty.

---

## 3 · The v0 identity measurement — a negative result

v0 ships tier-1 identity only so the damage could be measured before tiers 2–4 were built
(§8, ARCH §5). That measurement is a v0 deliverable and it is now in:

| Comparison | unmatched base | unmatched head | matched |
|---|---|---|---|
| fmt `HEAD~12` | 3 | 3 | 10,571 |
| fmt `HEAD~100` | 5 | 97 | — |
| spdlog `HEAD~30` | 2 | 1 | — |

fmt `HEAD~100`'s 97 head-side residue are genuine additions, so the *pairable* bound is 5.

**The upper bound on events that tiers 2–4 could remove is 3, 5 and 1.** Tier-1-only costs almost
nothing on these repositories. This argues against the next planned investment, which is exactly
what ARCH §10 asks the design to surface in week one rather than month six.

Caveat worth keeping: fmt and spdlog are stable libraries. A repository mid-refactor would look
different, and the number to watch is `internal_linkage_residue` — statics and anonymous-namespace
members whose USR embeds the file name, where a file rename breaks tier 1 wholesale.

---

## 4 · Measured performance

fmt, 50 TUs, WSL, 8 cores. All 22 measured builds — 1 vs 4 vs 8 workers, cold/warm, cache on/off —
produced the same fingerprint, `5ba8fd29041c`.

| Scenario | invalidated TUs | 1 worker | 8 workers |
|---|---|---|---|
| cold, cache off | 50 | 105.7 s | **31.0 s** |
| cold, cache on | 35 + 15 in-run hits | 86.1 s | **25.6 s** |
| fully warm | 0 | 6.3 s | 6.4 s *(no pool started)* |
| warm, 10 sources touched | 13 | 30.7 s | **14.2 s** |
| warm, 20 sources touched | 24 | 55.0 s | **19.4 s** |

**ARCH §9's budget — ≤ 20 TUs under 30 s warm — is met**, and at 24 invalidated TUs rather than 20,
so the stated case has more headroom than the number shows. At one worker the same review is 55 s.

The residual ~6.5 s warm floor is not parallelized: ~1.3 s `clang-scan-deps` + toolchain discovery,
~4.2 s key hashing and cache deserialization, ~0.6 s assembly.

---

## 5 · Findings worth keeping

Every one of these came from running the code against real repositories. None was visible in
synthetic fixtures.

- **Silent AST truncation.** The `libclang` PyPI wheel ships the native library without builtin
  headers. Every TU came back with exactly one error — `'stddef.h' file not found` — and no
  exception. The first 50-TU measurement was wrong by 21 s. This is the worst possible failure for
  a two-point tool: both models omit the same declarations and agree.
- **The extractor leaked the host path and commit SHA into identity.** clang spells an anonymous
  record as `(anonymous union at <abs path>:<line>:<col>)`, and that reached `name`,
  `qualified_name`, and — less obviously — canonical *type* spellings, so it also reached
  `attrs["type"]`, `attrs["return_type"]` and `signature`. Worktrees are named by SHA, so 32 fmt
  nodes and 22 spdlog nodes differed on **every comparison of any two revisions**. They were the
  only spurious differences in an otherwise clean two-point run.
- **Lazily-evaluated exception specifications are a tier-1 hazard.** clang leaves an implicit
  special member's spec `unevaluated` in a TU that never odr-uses it, so `~ostream` is
  `void () noexcept` from one TU and `void ()` from another. `signature` is tier-1 identity input:
  add one test file and an unchanged destructor reports delete + add.
- **Never parse a version out of a whole path.** `x86_64-linux-gnu` contains `86`, which outranked
  every real LLVM version and ranked LLVM 16 above LLVM 20.
- **Ask the system before guessing.** Ubuntu ships `llvm-config-18` and no bare `llvm-config`, so
  the good discovery path silently never fired. `ldconfig -p` reports a directory and suffix no glob
  had. The same trap recurred with `clang-scan-deps`, which is also not on PATH.
- **The producer is the parser *and* the extraction code.** `Toolchain.id` alone leaves a cache-key
  hole exactly the size of this project's own source: the extractor fix above changes every partial
  while leaving all cache inputs byte-identical. `ir.py` and `extract.py` are hashed into the key.
- **fmt's compile database has 50 entries but only 35 distinct compilations** — three test sources
  compiled once per target, differing only in `-o`. Because the cache keys on flags as
  `parse_arguments` produces them, 15 of 50 parses in a *cold* run are served from entries written
  earlier in the same run.
- **fmt's model is 80.6 % test code**, 31.7 % of it vendored gtest. Extraction is right to include
  it; ranking had to be taught it exists, or every review opens with vendored churn.
- **Threads do not help.** ctypes drops the GIL around `index.parse`, but the extraction half is
  Python walking cursors: four TUs go 10.38 s → 8.93 s on four threads. 1.16×.
- **Aliasing merges as well as resolves.** fmt's 26,071 distinct edges drop 5,113 unresolvable
  endpoints but yield 20,958 — not what subtraction gives. 2,399 are two calls to two instantiations
  of one template becoming one call to the template, which is what ARCH §5 asks for.

---

## 6 · Known defects and gaps

Found by running the pipeline on real code. Ranked by how badly each corrupts the product's claim.

- [ ] **Consequences outrank their causes.** `_blast_radius` takes the larger of an edge's two
      endpoints, so a new call into a popular utility inherits the utility's fan-in. On fmt
      `HEAD~100`, `NodeAdded vprintln` scores 0.70 while ten `EdgeAdded` nested *under* it score up
      to 0.84. Ranking order is the product's core claim, so this matters more than it looks.
- [ ] **`high_risk_count` does not discriminate** — 40 % of events on fmt `HEAD~100`. Structural,
      not a tuning accident: in v0 every add/remove is unmatched residue, so uncertainty is pinned
      at 1.0 and a public `NodeAdded` clears the 0.6 threshold with *zero* blast radius. It
      currently measures "public and new".
- [ ] **Decomposition collapses on large diffs** — 78 % of events in one component on fmt
      `HEAD~100`. `calls` edges connect most of a header-only library.
- [ ] **`Completeness.is_complete` counts TU parses only.** fmt drops ~5,113 of 26,071 distinct
      edges per side and `is_complete` still returns `True`, so `Confidence.models_complete` is
      `True` on every event in a model that discarded a fifth of its graph. The only record is prose
      in `notes`, which no consumer can state numerically.
- [ ] **No `EdgeAttrChanged` event kind.** `inherits` going public → private between two otherwise
      unchanged classes is invisible in the delta — a real C++ breaking change, silently dropped.
- [ ] **`match_models(renames=...)` is never fed.** ARCH §5 requires file renames resolved from git
      *before* matching; `acquire` exposes only `changed_files` (`--name-only`), which cannot carry
      them. Needs an `acquire` API.
- [ ] **`Node.tu` is declared and never populated.** `ir.py` documents it as identity-relevant for
      internal linkage; the producer never sets it. Partly masked because clang's USRs for
      internal-linkage entities already embed the file name.
- [ ] **`EdgeAdded`/`EdgeRemoved` between two surviving nodes is not collapsible** by any current
      cause rule, and is the bulk of the remaining top-level noise.
- [ ] Self-edges appear in real models (`is_named_arg -> is_named_arg`). Producer-side.
- [ ] Worker startup is ~0.6 s each, of which the probe is 24 ms — the rest is
      `candidate_libraries()` re-running `llvm-config`/`ldconfig` that the parent already ran.
      Wants a `Toolchain.adopt(library, expected_id)`.
- [ ] `cli.py` has no `--workers` flag, though `build_model(workers=...)` and `$CPPSD_WORKERS` are
      live.
- [ ] `cache.notes()` quotes a stale per-TU cost (~2.6 s; now ~1.5–2.0 s serial, ~0.5 s parallel).
- [ ] Member function templates are classified `function`, not `method`, losing `explicit` with no
      other representation — 414 on fmt. Both models omit it identically, so it does not corrupt a
      diff; it corrupts rendering and any kind-keyed logic.
- [ ] `signature` is `-std`-dependent. spdlog is configured `-std=c++11`, where `noexcept` is not
      part of the function type; 157 of its 2,478 nodes carry `exception_spec` with no `noexcept` in
      the signature. A `CMAKE_CXX_STANDARD` bump — one line of diff — rewrites 6 % of the model.
      Belongs in `Completeness` as a language-mode note. Relatedly, **fmt's compile database carries
      no `-std` at all**, so fmt is parsed at libclang's default standard rather than the project's.

---

## 7 · Still open

- **Generated sources.** ARCH §3.1 states the problem, not the policy. Configure-only is the v0
  design; `clang-scan-deps` failed on 119 of opencv's 1,292 TUs (9.2 %) against an unbuilt tree,
  every one on a header CMake emits during the build. `fmt` and `spdlog` will not reveal where the
  threshold is.
- **Whether tiers 2–4 are worth building at all**, given §3. The answer is probably "not on these
  repositories" and "unknown on a repository mid-refactor". Measure a third repository before
  deciding.
- **Where the reviewer actually reads this.** CLI is right for v0 and probably wrong as the
  product. The JSON-first contract defers the decision without prejudging it.
- **Unnamed (not anonymous) records have an unstable USR** — file basename plus a byte offset that
  moves on any edit above the record. A tier-1 hazard. Neither fixture has one.

---

## 8 · Decisions — with the reason, so you can tell if it still holds

| Decision | Why |
|---|---|
| Python, standalone repo | CodeAtlas C++ is TypeScript only because it began as a VS Code extension. Ideas carry over from it, not modules. |
| Linux only during design and implementation | Windows support is dead weight while the shape is still moving. Portability rules stay (§9) — cross-platform *correctness* is cheap; cross-platform *support* is not. |
| A system libclang is a hard requirement | The wheel's bundled library truncates ASTs silently. Convenience is not worth a failure mode that produces two agreeing wrong models. |
| Base is `merge-base(main, HEAD)` | Against main's tip, every change other people merged while the branch was open gets attributed to this branch. There is a test for this; keep it. |
| Compile database required at **both** revs | Hard precondition, not a degradation path. Configure is seconds, so it is affordable. |
| Base that does not build ⇒ hard failure | The base is main. A main that does not build is a different problem, not this tool's. |
| JSON is the product, terminal is a consumer | Keeps delivery surfaces out of the analysis core — the mistake that made CodeAtlas TypeScript. `render`'s signature admits no `Model`. |
| v0 ships **tier-1 identity only** | Renames look bad on purpose. Measure the damage before building tiers 2–4. Done — see §3. |
| Identity tiers are one-to-one | Splits and merges surface as unmatched residue rather than being forced into a match. Enforced in the type, not just the matcher: a double-pairing does not crash, it produces a delta that reads perfectly and describes a change nobody made. |
| Cascade collapse at render, never at diff | JSON always carries the full event set; the view hides consequences under causes. When cause attribution is uncertain, show rather than hide. |
| `EdgeRetargeted` is refused, not deferred | An edge's id *is* `src\|kind\|dst`, so "retargeted from B to C" claims B and C are the same entity — a node-identity judgement the one-to-one matcher already declined. |
| The cache unit is the TU, not the commit | Per-commit caching means the first review against any new base pays full price, and the base is what every review shares. |
| Declining to cache beats a weaker key | A missing or version-mismatched `clang-scan-deps` disables the cache whole. Declining to cache is slow; a stale hit is wrong. |
| The cache key covers the extraction code, not just libclang | `extract.py` decides what becomes a node; change it and the same inputs yield a different partial. Hashed rather than a hand-bumped constant, because forgetting a constant fails toward serving stale entries. |
| Workers use `spawn`, not `fork` | A forked child inherits the parent's verified libclang, so the toolchain guarantee would hold *by accident of start method* and stop holding silently if that default changed. |
| Low confidence ranks **up** | Uncertainty is a reason for a human to look, not a reason to hide (ARCH §7). Guarded by mutation-tested cases. |

---

## 9 · Conventions

- **No hardcoded paths.** A tool that analyzes arbitrary repositories on arbitrary machines cannot
  carry assumptions about either. Ask the system, then fall back; document fallbacks as fallbacks.
- **Cite ARCH sections in code comments** where a non-obvious choice traces to a design decision
  (`# ARCH-001 §4.3`). It is how the reasoning survives.
- **Docs carry a metadata block** — `id`, `type`, `status`, `author`, `created`, `dependencies`.
  IDs are `RESEARCH-00x` and `ARCH-00x`, unique, never recycled.
- **Commit messages** say what changed and *why it matters*. Record findings discovered by running
  the code; every finding in §5 was a real bug in waiting.
- **Tests assert the design, not the implementation.** The merge-base test exists because getting it
  wrong is invisible in output; the fingerprint test exists because "a moved entity is the same
  entity" is a claim ARCH makes; the cache-invalidation tests exist because a stale hit is the worst
  failure this system can have. Prefer tests of that kind. Do not pin float scores — it fossilizes
  weights that are meant to be tuned.
- Run `uv run ruff format . && uv run ruff check . && uv run pytest -q` before committing.
