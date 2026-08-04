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
fmt    HEAD~12    179 lines changed  →  16 semantic events, 2 high-risk
                  7 top-level, 9 nested under a cause · 5 components
fmt    HEAD~100   3,178 lines changed → 335 semantic events, 17 high-risk
                  56 top-level, 279 nested · 30 components
spdlog HEAD~30    1,708 lines changed → 32 semantic events, 5 high-risk
                  24 top-level, 8 nested · 18 components
spdlog HEAD~5     35 lines changed   →  0 semantic events

leveldb HEAD~9..~1 45 lines changed  →   6 semantic events, 3 high-risk   (7.5 lines/event)
                  + 117 in bundled dependencies from a CMAKE_CXX_STANDARD bump

leveldb HEAD~10   52 lines changed   → 134 semantic events, 5 high-risk
                  3,660 more in bundled dependencies (96 %), ranked last
                  11 top-level · 142 components · first vendored event at rank 112
```

**The leveldb row is what a third fixture was for.** The commit bumps `third_party/googletest` and
`third_party/benchmark`, and on the first run the top 12 events and 44 of the top 50 were bundled
googletest — the exact failure `rank/exposure.py` was written to prevent. Vendored code now sorts
after everything first-party as a block (§6, and `ranker.py` carries the arithmetic), so the top
event is `DBImpl::TEST_CompactMemTable` — which is what the commit says it does.

| | before | after |
|---|---|---|
| vendored in top 12 | 12 | **0** |
| vendored in top 50 | 44 | **0** |
| rank of first vendored event | 1 | **112** |

Neither original fixture could show it: they vendor googletest as a static copy that never differs
between two revisions of the host, so vendored event counts were always near zero. It takes a
dependency that moves as a unit.

**The ratio no longer straddles.** It was 0.0 lines per event, dividing a first-party denominator by
a whole-world numerator. The denominator was never wrong — git renders a submodule bump as the two
lines a reviewer actually reads, and §10 defines it as "the number a reviewer would otherwise have
read", so inflating it to the hundred thousand lines inside googletest would have been fiction that
flattered the ratio. The numerator came down instead: `compression_ratio` and `high_risk_count` are
over first-party events, `vendored_events` names what was set aside, and `events_reported` stays the
complete set. Schema 1.1 → 1.2, because two fields kept their names and changed their meaning.

**leveldb still reads 0.4 lines per event, and that is the honest answer.** 52 lines of text diff
produced 134 first-party semantic events — mostly test-code entities that moved because googletest
moved under them. The tool is not compressing here; it is reporting that a two-line dependency bump
had 134 consequences in this repository's own code, which is a real finding and the opposite of the
thesis on this commit. Worth keeping visible rather than averaging away.

Nothing else moved: fmt `HEAD~12` (11.2 lines/event) and spdlog `HEAD~30` (53.4) are unchanged to
the digit, which is the check that the numerator was aligned rather than shrunk.

**The verdict, without spin.** Compression is real but roughly an order of magnitude weaker than
ARCH §10's illustrative *2,431 → 9* (270:1). Measured: **0.4:1 to 53:1** on first-party events.

The bottom of that range is new and it is not a rounding error — leveldb `HEAD~10` *decompresses*,
turning 52 lines into 134 things to read. Both fixtures that were available when the "9.5:1 to 53:1"
claim was written are header-only libraries that vendor by copying; the first repository with real
implementation files and a moving dependency produced a ratio below 1. One commit is not a verdict
on the thesis, but the range no longer supports a claim of reliable compression, and quoting the old
one now would mean quoting the two repositories that agreed.

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

| Comparison | unmatched base | unmatched head | matched | pairable bound |
|---|---|---|---|---|
| fmt `HEAD~12` | 3 | 3 | 10,571 | 3 |
| fmt `HEAD~100` | 5 | 97 | — | 5 |
| spdlog `HEAD~30` | 2 | 1 | — | 1 |
| leveldb `HEAD~9..~1` | **0** | 49 | 7,695 | **0** |

Head-side residue is not pairable where it is genuinely new: fmt `HEAD~100`'s 97 are real additions,
and leveldb's 49 are an artifact of the `CMAKE_CXX_STANDARD 11 → 17` bump in that range exposing
declarations C++11 did not (all 49 in vendored googletest — see §5).

**The upper bound on events that tiers 2–4 could remove is 3, 5, 1 and 0** — and the zero is on the
only repository with real implementation files rather than a header-only library. Tier-1-only costs
almost nothing anywhere it has been measured. This argues against the next planned investment, which
is exactly what ARCH §10 asks the design to surface in week one rather than month six.

**Verdict: do not build tiers 2–4.** §7 held this open pending a third repository; it now has one,
of a deliberately different shape, and the answer got *stronger* rather than weaker. What would
reopen it is a repository mid-refactor — none of these four comparisons renames anything
substantial, and `internal_linkage_residue` remains the number to watch.

Caveat worth keeping: fmt and spdlog are stable libraries. A repository mid-refactor would look
different, and the number to watch is `internal_linkage_residue` — statics and anonymous-namespace
members whose USR embeds the file name, where a file rename breaks tier 1 wholesale.

### The rename hazard, measured

fmt `da256982` (`src/posix.cc` → `src/os.cc`), before and after `acquire.file_changes` was wired
into `match_models`:

| | before | after |
|---|---|---|
| `internal_linkage_residue` | 2 | **0** |
| unmatched base / head | 1 / 1 | 0 / 0 |
| events reported | 8 | **0** |
| high-risk | 6 | **0** |

All eight were **one unchanged function** — `convert_rwcount` in the renamed file's anonymous
namespace — reported as removed and added, with its two call edges and its `contains` edge each
counted twice. Zero is the correct answer: the same commit moved declarations from `posix.h` to
`os.h`, and external-linkage USRs do not encode the file, so that part is correctly no event.

**How rare, and how bad.** fmt has **154 file-scoped USRs out of 9,752 nodes** (1.6 %), 144 of them
in vendored `gmock-gtest-all.cc`. Most renames touch none and cost nothing — `include/fmt/core.h` →
`base.h` measured 0 before *and* after. So the defect is rare but severe where it lands, which is
the argument for fixing it rather than counting how often it fires.

### Both original fixtures were the wrong shape — leveldb added

fmt and spdlog are both effectively header-only, and **spdlog vendors fmt**
(`include/spdlog/fmt/bundled/`, `src/bundled_fmtlib_format.cpp`), so they were closer to one and a
half samples than two. That matters specifically here: a header-only library cannot use
namespace-scope statics, because that would mean one copy per translation unit. The fixtures
structurally excluded the phenomenon `Node.tu` exists for.

| | nodes | TUs | file-scoped USRs | of those, **library code** |
|---|---|---|---|---|
| fmt `HEAD~12` | 10,574 | 50 | 153 (1.5 %) | **2** |
| leveldb `HEAD~10` | 7,977 | 93 | 825 (10.3 %) | **428** |

Model composition by exposure class, which is the number that explains it:

| | vendored | test | internal | public |
|---|---|---|---|---|
| fmt | 31.7 % | 49.0 % | **0.0 %** (3 nodes) | 19.4 % |
| leveldb | 52.8 % | 28.5 % | **6.2 %** (491) | 12.5 % |

fmt has *three* internal nodes in the whole model — everything lives in `include/`, so everything
is public. Every `Node.tu` conclusion drawn from fmt alone rested on a sample of two.

---

## 4 · Measured performance

fmt, 50 TUs, WSL, 8 cores. All 22 measured builds — 1 vs 4 vs 8 workers, cold/warm, cache on/off —
produced the same fingerprint, `5ba8fd29041c`. That value is superseded: `Model.fingerprint()`
now hashes `Node.tu` where the USR is file-scoped, so fmt `HEAD~12` reads `5ca3843b6c1a` —
verified identical at 1 and 8 workers, which is the property the check is for. The number changing
when the model gains content is expected; the number changing between two runs of the same build
would be the defect.

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
- **`EdgeAttrChanged` is correct and almost never fires** — zero occurrences across 300 commits of
  each fixture. Worth knowing before anything is built on top of it: a rule that depends on it will
  look correct in review and do nothing in practice.
- **The `-std` hazard is real, and it was found by a one-line CMake diff.** §6 predicted that a
  `CMAKE_CXX_STANDARD` bump "rewrites 6 % of the model"; leveldb `HEAD~9..~1` does exactly that —
  `11 → 17`, 45 lines of diff, and **117 events in vendored googletest** (49 `NodeAdded`, 67
  `EdgeAdded`) from declarations C++17 exposes and C++11 did not. Two things worth keeping. The
  first-party events on that range are **6, all real** and all traceable to the commits in it, so
  the artifact did not contaminate what the reviewer reads — the vendored partition contained it,
  which is that decision earning its place a second time. And the artifact is invisible in the
  residue as anything but "additions": a language-mode change looks exactly like new code, which is
  why `Completeness` should carry the language mode (§6) rather than leaving it to be inferred.
- **`git worktree add` does not populate submodules**, and nothing noticed until the fixture set
  widened past two repositories that vendor by copying. A submodule is a gitlink — mode 160000, a
  commit id — and the worktree gets an empty directory. leveldb failed CMake configure, which
  surfaced as ARCH §3.1's "the base is expected to build": alarming and wrong, since the base built
  and the worktree was incomplete. The quieter case is worse — where a missing submodule does *not*
  break configure, both models lose the same entities and agree with each other, which is the §5
  silent-truncation failure by another road. Fixed in `acquire`, and it **requires network**: a
  worktree clones submodules into its own module store (`.git/worktrees/<sha>/modules/…`), so the
  parent having them checked out does not help and `submodule.alternateLocation=superproject` does
  not rescue it. Both measured with the network blocked.
- **The exposure heuristic generalized to a layout it was not fitted to.** leveldb puts
  `db/db_test.cc` beside `db/db_impl.cc` with no `test/` directory anywhere, so only the
  filename-stem half of the rule applies — and it classifies correctly. One over-promotion worth
  calibrating: `port/port_stdcxx.h` reads PUBLIC because it is a header outside a private segment,
  though leveldb treats `port/` as internal.
- **`git diff --name-only` was discarding an answer git had already computed.** Rename detection
  defaults on since git 2.9, so the base-side path of every rename was available all along and the
  flag choice threw it away. Before building machinery to recover something, check whether the tool
  already knows it and is being asked the wrong question.
- **`errors="replace"` is not a safe default when the string is an identity.** Two different
  undecodable paths decode to the same string, so a rename map built through `run_git` would collapse
  two renames into one entry and assert that two different files are the same file. Paths that are
  identity are decoded with `surrogateescape`, the way `os` decodes filenames.

---

## 6 · Known defects and gaps

Found by running the pipeline on real code. Ranked by how badly each corrupts the product's claim.

Closed this session, kept because the *reason* each was invisible is the reusable part: three
ranking defects that were one attribution bug (an edge event was treated as belonging to both its
endpoints, so a new call inherited its callee's fan-in — inversions 174→0, high-risk 40 %→5 %,
largest component 78 %→18 %); `Completeness` unable to state incompleteness numerically; the
missing `EdgeAttrChanged`; and a toolchain identity that named the parser but not the parse. Every
one was invisible on synthetic fixtures and obvious on the first real run.

Also closed: **`match_models(renames=...)` was never fed** — `acquire.file_changes` now resolves
renames from git and `cli.py` passes them (§3 has the before/after). The lesson worth keeping is
about the *measurement*, not the fix: the commit chosen to demonstrate the defect showed 0 before
and 0 after, because the prediction that fmt's anonymous records carry file-scoped USRs was wrong —
clang spells them `…@Ua`. `internal_linkage_residue` is 0 on most real renames, so it cannot serve
as a smoke test; a fixture commit has to be chosen by first checking the renamed file for a
namespace-scope `static` or an anonymous namespace.

The first two are what the leveldb row in §2 is made of, and they are the most serious open items.

- [ ] **`internal_linkage_residue` is still computed over the whole model**, vendored included —
      the last metric that does not distinguish what `rank/exposure.py` classifies. leveldb is
      52.8 % vendored by node count. `compression_ratio` and `high_risk_count` were fixed; this one
      lives on `Correspondence`, which is upstream of `rank` and has no exposure map, so it needs a
      decision about where the classification belongs rather than a one-line change.

Designed in [PLAN-001](docs/plans/rename-identity.md), which also carries the measurements.

- [x] ~~**`Node.tu` is declared and never populated.**~~ Set where the USR is file-scoped **and**
      the declaration is in the TU's own primary source. The second condition is what makes it
      safe: a `static` in a *header* is one entity per includer collapsed to one node, so its TU
      would be whichever observation ranked highest and would flip when an unrelated source joined
      the build. Under the narrow rule exactly one TU can see each entity that gets a TU, so there
      is nothing to flip. `Model.fingerprint()` hashes it under the same condition
      `DeclaredTier.key` uses — fmt `HEAD~12` is now `5ca3843b6c1a` (§4). Re-measured on all three
      fixtures: **every event count, high-risk count and residue identical to before**, which is
      what "near-zero risk by construction" predicted.
- [ ] **Two same-basename statics still merge into one node** — and `Node.tu` does *not* fix this,
      which PLAN-001 claimed it would. `merge_node` keys the model on `Node.id`, which **is** the
      USR, so the collision happens before any TU is recorded; half of them stay invisible and a
      body change in either produces no event. What `tu` fixed is the *matching* half: a base node
      from `a/` no longer pairs with a head node from `b/`. Separating them in the model needs the
      node dictionary keyed on `(usr, tu)`, which reaches every edge endpoint, every event, every
      cache entry and the JSON contract. Named rather than half-done. No instance in any fixture;
      `tests/test_tu_identity.py` is the only place it exists.
- [x] ~~**`EdgeAdded`/`EdgeRemoved` between two surviving nodes is not collapsible**~~ — the shape
      that actually caused it is: a dependency adds an overload and every call site moves to it,
      leaving the old overload declared but uncalled. `call-moved-to-another-overload` links those
      to the `EdgeAdded`, which chains to the new overload's `NodeAdded`. leveldb `HEAD~10`:
      **39 top-level → 11**, and fmt and spdlog unchanged to the digit, which is the check that it
      did not over-link. A call genuinely dropped between two survivors still has no cause and is
      still shown — that is the design (`test_classify.py`), not a gap.
- [ ] **Exposure takes the max over both edge endpoints** while blast radius and decomposition now
      take the source. Contained by the cause bound today, but an `EdgeAdded` from test code into
      public API with *no* cause link is still scored as public. Decide it deliberately.
- [ ] **No cause rule links `NodeAttrChanged(virtual)` to the `EdgeAttrChanged` on every call edge
      into it.** A method gaining `virtual` with N callers is N+1 top-level events.
- [ ] **`merge_edge` merges edge attrs with `setdefault`** — the first partial to arrive wins, so an
      edge attribute that differs between TUs is silently taken from whichever TU parsed first.
- [ ] Self-edges appear in real models (`is_named_arg -> is_named_arg`). Producer-side.
- [ ] **Superseded cache identities are never reclaimed.** `_evict` trims within one identity
      directory only, so every producer change orphans the previous generation — ~109 MB after
      today's. Needs a `cppsd cache gc` or an age sweep.
- [ ] `CPPSD_RESOURCE_DIR` is keyed by path, not content: `clang-scan-deps` runs the compile-database
      command without our `-resource-dir`, so the closure reports the *default* resource dir's
      headers. Changing the directory invalidates; changing its contents does not. Escape-hatch only.
- [ ] Worker startup is ~0.6 s each, of which the probe is 24 ms — the rest is
      `candidate_libraries()` re-running `llvm-config`/`ldconfig` that the parent already ran.
      Wants a `Toolchain.adopt(library, expected_id)`. The same cost makes `tests/test_toolchain.py`
      take 140 s of the suite's ~100 s wall — one fix covers both.
- [ ] `cli.py` has no `--workers` flag, though `build_model(workers=...)` and `$CPPSD_WORKERS` are
      live.
- [ ] `cache.notes()` quotes a stale per-TU cost (~2.6 s; now ~1.5–2.0 s serial, ~0.5 s parallel).
- [ ] Member function templates are classified `function`, not `method`, losing `explicit` with no
      other representation — 414 on fmt. Both models omit it identically, so it does not corrupt a
      diff; it corrupts rendering and any kind-keyed logic.
- [x] ~~`signature` is `-std`-dependent and nothing says so.~~ `Completeness.language_modes` now
      carries the `-std=` values each side was parsed at, and the view says so in yellow when the
      two differ. The dependence is unchanged and unfixable — `noexcept` is part of the function
      type from C++17 and not before — so the answer is to state it, per ARCH §11. leveldb
      `HEAD~9..~1` reads `base c++11 → head c++11, c++17`, and the mixed head is real: 20 vendored
      `benchmark` TUs stay at c++11 while leveldb's own 73 move to c++17. fmt still states no `-std`
      at all, which now shows as an empty list rather than as silence.

---

## 7 · Still open

- **Generated sources.** ARCH §3.1 states the problem, not the policy. Configure-only is the v0
  design; `clang-scan-deps` failed on 119 of opencv's 1,292 TUs (9.2 %) against an unbuilt tree,
  every one on a header CMake emits during the build. `fmt` and `spdlog` will not reveal where the
  threshold is.
- ~~**Whether tiers 2–4 are worth building at all.**~~ **Answered: no.** The third repository is in
  and the pairable bound on leveldb is **0** (§3). Still unknown on a repository mid-refactor, which
  is the one condition that would reopen it.
- **Where the reviewer actually reads this.** CLI is right for v0 and probably wrong as the
  product. The JSON-first contract defers the decision without prejudging it.
- **Unnamed (not anonymous) records have an unstable USR** — file basename plus a byte offset that
  moves on any edit above the record. A tier-1 hazard. Neither fixture has one.
- **ARCH §6.3's cause table is missing a row.** `call-moved-to-another-overload` was found by
  running the tool, not by reading the design, and it is the rule that carries the largest cascade
  measured so far. §6.3 should name it, or state why the four uncited rules stay uncited.
- **`diff.renameLimit` can silently truncate the rename map.** Git skips *inexact* rename detection
  on large diffs and says so on stderr, which `run_git` discards on success — so the map would be
  incomplete with no signal, exactly the ARCH §11 case that wants the limit stated. `-l0` removes the
  cap but is potentially quadratic on the large refactor where it matters. Neither answer is
  obviously right, and the fixtures are too small to force the choice.

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
| Toolchain identity carries the target triple, never a host path | It is the one AST determinant the closure's content hashing cannot see. A path would break §12.2, which makes the identity a value two hosts *compare* — equivalent machines would hard-fail. |
| Check what the whole key already covers before widening an identity | The stdlib and builtin headers turned out to be content-keyed via the include closure. Every redundant key component costs a mass invalidation to defend against a defended hazard. |
| Workers use `spawn`, not `fork` | A forked child inherits the parent's verified libclang, so the toolchain guarantee would hold *by accident of start method* and stop holding silently if that default changed. |
| Low confidence ranks **up** | Uncertainty is a reason for a human to look, not a reason to hide (ARCH §7). Guarded by mutation-tested cases. |
| Vendored code is **partitioned** out of the ranking, not merely down-weighted | The reviewer is reviewing their own code; a bumped dependency is context, not the subject. As a weighted term it lost — measured on leveldb, where blast radius plus novelty (0.60) beat all of exposure (0.25) and put googletest in the top 12. Raising the weight would fossilize a number against one repository and still lose on the next. The claim is "not what is being reviewed", which is an ordering, not a quantity. Only VENDORED — the project's own tests are its code and keep the weighted demotion. |

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
- **Reserve the shared contracts when work is split across concurrent agents.** `model/ir.py` and
  `model/events.py` are what every stage agrees on; letting several workstreams edit them
  concurrently produces four partial versions of one decision. Held as read-only with a
  stop-and-report rule, it caught four separate requests to change `Completeness` and `EventKind`,
  which were then done once as a coherent pass. **Commit coupled work together** — a stage emitting
  a new event kind and the ranker learning its weight are one commit, because either alone is a
  `KeyError`.
