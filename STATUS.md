# Status

Living tracker: what is built, what is next, and what was already decided. Updated at the end of
each working session. Started 2026-08-01 as a handover note; kept as a tracker because the
*reasons* behind the settled decisions are the part worth carrying forward.

Setup, requirements and the commands that work today are in [`README.md`](README.md). Design is
[ARCH-001](docs/architecture/overview.md); this file is state, not design.

---

## 1 · Pipeline

Eight stages (ARCH §3). Two and a half exist.

- [x] **acquire** — two revs → two worktrees + compile databases
- [ ] **produce** — worktree + compile db → `Model` ← *in progress, see §3*
  - [x] toolchain: discover a system libclang and prove it before use
  - [x] `extract.py`: one TU → a partial model. Traversal, the five node kinds, all four
        edge kinds, USR dedup, `symbol`, `body_hash`, per-TU failure recorded not raised
  - [ ] builder: merge partials into one `Model`, resolve edges and template aliases,
        fill `Completeness` (including `producer_version`)
  - [ ] per-TU cache keyed on content + headers + flags + producer version
- [ ] **match** — two `Model`s → `Correspondence` (tier 1 only in v0)
- [ ] **diff** — → `Event` list
- [ ] **classify** — cause and cascade links
- [ ] **enrich** — blast radius, evidence adapters *(post-v0)*
- [ ] **rank** — risk scores
- [ ] **render** — JSON first, terminal as a consumer

---

## 2 · Done

### Code

- [x] `model/ir.py` — the IR (ARCH §4). `Node`, `Edge`, `Model`, `Completeness`. Scalar-only
      attributes enforced; `body_hash` separate from `attrs`; `Location` is provenance and provably
      not identity, with a fingerprint test saying so.
- [x] `acquire/git.py` — revision resolution. Base is `merge-base(main, HEAD)`, three-dot.
- [x] `acquire/workspace.py` — git worktrees cached by SHA, CMake configure, compile-database
      parsing.
- [x] `produce/toolchain.py` — libclang discovery and verification. Discovers via
      `$CPPSD_LIBCLANG` → `llvm-config` (including versioned names) → `ldconfig -p` → directory
      patterns as a last resort; parses a probe TU including `<stddef.h>` and `<stdarg.h>` and
      refuses to run if it does not come back clean.
- [x] `produce/extract.py` — one translation unit → a partial model, which is also the cache unit
      (ARCH §9). Nodes keyed by USR; `contains` follows the semantic parent, so an out-of-line
      definition belongs to its class rather than to the namespace it was written in; `overrides`
      comes from clang rather than from matching names and signatures by hand.
- [x] `errors.py` — the precondition taxonomy. ARCH §11: hard failures, never degraded output.
- [x] `cli.py` — `cppsd version` · `doctor` · `plan` · `acquire`.

### Infrastructure

- [x] Repository published, private: `j-brent/cpp-semantic-diff`. Default branch `main`, work on
      `dev`.
- [x] `ci` workflow — format, lint, tests, plus two steps that exist because of specific bugs:
      `cppsd doctor` on a runner (the only place discovery runs on a host that is not a
      developer's), and a guard that points `CPPSD_LIBCLANG` at the truncating bundled library and
      **requires** `doctor` to reject it.
- [x] `demo` workflow — runs against `fmt` and `spdlog` on every push and publishes to the run
      summary. `workflow_dispatch` takes any repo URL and base ref. Uses a fresh cache root per run
      so a cold measurement is honestly cold, and states in place that the numerator is not built
      yet, rather than looking like semantic-diff output while showing none.
- [x] Linux-only cleanup: no hardcoded paths anywhere; `.gitattributes` pins LF.

### Measured

On `fmt`, `--base HEAD~12`, in WSL:

```
plan:     11 files, 5 C++ files, 179 lines changed   ← the compression denominator
acquire:  50 C++ TUs of 51 entries, both sides
          12.2 s cold  →  1.2 s warm
```

Producer spike, before any producer exists:

| Question | Answer | Consequence |
|---|---|---|
| Where does libclang time go? | parse 68 %, extract 32 % | The cost is the parser, not the Python FFI. Pruning the traversal saves only 18 %. |
| What does one TU cost, pruned? | 1.02 s | 20 TUs ≈ 20 s — inside the 30 s budget even cold. A full cold `fmt` model is 51 s per side, which is what the cache is for. |
| What do bodies cost? | 61 % of parse time, 2 % of USRs (10,588 → 10,413) | A real two-tier lever later. Unusable in v0: `calls` edges and `body_hash` both need bodies. |

This disproved the hypothesis that Python binding overhead dominates, and vindicated ARCH §9's
cache-with-the-producer plan.

**The real extractor, all 50 of `fmt`'s TUs at HEAD:**

```
125 s total,  2.5 s/TU      ← 2.4x the spike's 1.02 s/TU estimate
134,366 nodes extracted  →  10,574 distinct     (12.7x, dedup working)
224,571 edges extracted  →  28,470 distinct
kinds:  method 6,467 · function 1,615 · class 1,362 · field 821 · struct 268 · namespace 35 · union 6
edges:  calls 16,537 · contains 9,795 · overrides 1,227 · inherits 911
```

The estimate was low because the spike priced a *pruned traversal*, not the v0 scope: walking every
function body for `calls` edges is work the spike never did. Split measured directly — parse ≈ 50 %,
walk ≈ 45 %, body-hash tokenization only 3–19 %. So the 30 s warm budget still holds, but a cold
full model is ~125 s per side rather than 51 s, and the cache carries more weight than it looked.
`body_hash` is not the thing to cut if that ever needs cutting.

### Findings worth keeping

- **Silent AST truncation.** The `libclang` PyPI wheel ships the native library without builtin
  headers. Every TU came back with exactly one error — `'stddef.h' file not found` — and no
  exception. The first 50-TU measurement (13.8 s) was wrong; the truth was 34.9 s. This is the
  worst possible failure for a two-point tool: both models omit the same declarations and agree.
- **Never parse a version out of a whole path.** `x86_64-linux-gnu` contains `86`, which outranked
  every real LLVM version and ranked LLVM 16 above LLVM 20. `path_version()` matches only
  version-bearing components; there is a test naming the failure.
- **Ask the system before guessing.** Ubuntu ships `llvm-config-18` and no bare `llvm-config`, so
  the good discovery path silently never fired and hardcoded globs did all the work. `ldconfig -p`
  reports `/lib/x86_64-linux-gnu/libclang-18.so.18` — a directory and suffix no glob had.
  Distribution-specific advice belongs in error text, labelled; never in logic.
- **Nothing calls a template.** A call resolves to an *instantiation*, whose USR belongs to no
  declaration anyone wrote — 10,158 of fmt's call edges, roughly a third of the graph, pointed at
  entities that could never be nodes. `clang_getSpecializedCursorTemplate` maps them back, but it
  cannot be applied where it is discovered: an *explicit* specialization is hand-written code with
  its own identity (173 of them in fmt) and collapsing it into the primary template would hide real
  changes. The extractor records the mapping; the builder, which knows the merged node set, applies
  it only where the instantiation is not itself a node.
- **libclang echoes back relative paths.** A header reached through a relative `-I` comes back
  relative, and resolving that against the *process* working directory puts it outside the
  repository — dropping every declaration in it, with no diagnostic. Caught by a test that used a
  relative include on purpose; `-working-directory` alone was not enough.
- **The producer id must survive repackaging.** `libclang Ubuntu clang version 18.1.3 (1ubuntu1)`
  as a cache-key component would invalidate every cached model when a distro rebuilds the package.
  The id is the release triple: `libclang 18.1.3`.

---

## 3 · Next: finish the producer

Extraction works per TU. What is left is the **builder** — merge the partials into one `Model` —
and then the cache, which ships **with** the producer, not after.

The builder is the only place that knows the whole picture, so three things belong there and
nowhere else:

1. **Resolve edges against the merged node set.** An edge whose endpoint no TU declared is dropped
   and *counted*, never silently discarded.
2. **Apply the template aliases**, and only where the instantiation is not itself a node.
3. **Fill `Completeness`** — `producer_version` from `Toolchain.id`, every failed TU with its
   reason, and the unresolved-edge counts as notes.

Measured on fmt so the builder has a target: after aliasing, 5,113 of 28,470 edges still have an
endpoint outside the model. 3,361 are genuinely external (`std::`, libc, compiler builtins) and
correctly dropped. The rest are **calls to implicit special members** — copy/move constructors,
implicit destructors — which no one declared, so they can never be nodes. Synthesizing nodes for
them would add half a dozen phantom entities per class; the honest answer is to report the count,
not to invent the entity.

**Settled. Do not relitigate:**

- **libclang, not clangd** (ARCH §12) — full control over extraction, USRs free, no per-symbol LSP
  round-trips, no private index format to reverse. Use the verified `Toolchain` from
  `produce/toolchain.py`; never call `Config.set_library_file` anywhere else.
- **Dedup by USR is correctness, not optimization.** Every header is reparsed once per TU. The
  CodeAtlas measurements found 15,656 method cursors collapsing to 161 classes in FTXUI. Get this
  wrong and node counts inflate by two orders of magnitude.
- **The cache unit is the translation unit, not the commit** (ARCH §9). Key on
  `hash(TU content, transitive header content, flags, producer version)`; assemble a commit's model
  from cached partials plus a reparse of only the invalid ones. `clang-scan-deps` computes the
  invalidation set and is cheap (opencv's 1,292 TUs in 14.1 s) — but it is **not on PATH** on
  Ubuntu; it lives beside the library and must be resolved explicitly, the same way the library is.
- **Populate `Node.symbol` from the start.** It is the join key to `abidiff`/`bloaty`/`nm`. The
  evidence layer is deferred, but retrofitting this invalidates every cached model.
- **Fill in `Completeness` honestly.** Every TU that fails to parse is recorded with a reason, using
  the existing `fatal_diagnostics()`. ARCH §11.
- **v0 extraction scope** (ARCH §12): namespace / class / function / method / field nodes;
  `contains`, `calls`, `inherits`, `overrides` edges. Nothing else.

**The budget** (ARCH §9): a review touching ≤ 20 TUs completes in under 30 s warm.

**After the producer**, in order: tier-1 identity matching, the delta, then the compression metric
end to end. Build toward the print statement, not toward feature completeness.

---

## 4 · Backlog

- [ ] `Completeness.producer_version` is declared and never set. It must carry `Toolchain.id`, and
      the same string must be a component of the per-TU cache key — a model built by a different
      parser is a different model. Wire both together when the producer lands, not after.
- [ ] Terminal output is `rich` tables built inline in `cli.py`. Fine for four commands; it becomes
      the wrong shape the moment `render` exists, since JSON is the product and the terminal is a
      consumer (ARCH §10).
- [ ] The `demo` workflow measures cold and warm acquire only. Extend it as each stage lands; the
      "numerator" section is the placeholder to fill.

---

## 5 · Still open

- **Generated sources.** ARCH §3.1 states the problem, not the policy. Configure-only is the v0
  design; `clang-scan-deps` failed on 119 of opencv's 1,292 TUs (9.2 %) against an unbuilt tree,
  every one on a header CMake emits during the build. `fmt` and `spdlog` will not reveal where the
  threshold is.
- **Tier-4 similarity threshold.** Git's `-M` has one knob and decades of tuning. Assume this needs
  per-repository calibration; design the knob in early.
- **Where the reviewer actually reads this.** CLI is right for v0 and probably wrong as the
  product. The JSON-first contract defers the decision without prejudging it.

---

## 6 · Decisions — with the reason, so you can tell if it still holds

| Decision | Why |
|---|---|
| Python, standalone repo | CodeAtlas C++ is TypeScript only because it began as a VS Code extension. Ideas carry over from it, not modules. |
| Linux only during design and implementation | Windows support is dead weight while the shape is still moving. Portability rules stay (§7) — cross-platform *correctness* is cheap; cross-platform *support* is not. |
| A system libclang is a hard requirement | The wheel's bundled library truncates ASTs silently. Convenience is not worth a failure mode that produces two agreeing wrong models. |
| Base is `merge-base(main, HEAD)` | Against main's tip, every change other people merged while the branch was open gets attributed to this branch. There is a test for this; keep it. |
| Compile database required at **both** revs | Hard precondition, not a degradation path. Configure is seconds, so it is affordable. |
| Base that does not build ⇒ hard failure | The base is main. A main that does not build is a different problem, not this tool's. |
| JSON is the product, terminal is a consumer | Keeps delivery surfaces out of the analysis core — the mistake that made CodeAtlas TypeScript. |
| v0 ships **tier-1 identity only** | Renames will look bad on purpose. Measure the damage before building tiers 2–4. That measurement is a v0 deliverable. |
| Identity tiers are one-to-one | Splits and merges surface as unmatched residue rather than being forced into a match. Confident-and-wrong is the failure mode to avoid. |
| Cascade collapse at render, never at diff | JSON always carries the full event set; the view hides consequences under causes. When cause attribution is uncertain, show rather than hide. |

---

## 7 · Conventions

- **No hardcoded paths.** A tool that analyzes arbitrary repositories on arbitrary machines cannot
  carry assumptions about either. Ask the system, then fall back; document fallbacks as fallbacks.
- **Cite ARCH sections in code comments** where a non-obvious choice traces to a design decision
  (`# ARCH-001 §4.3`). It is how the reasoning survives.
- **Docs carry a metadata block** — `id`, `type`, `status`, `author`, `created`, `dependencies`.
  IDs are `RESEARCH-00x` and `ARCH-00x`, unique, never recycled.
- **Commit messages** say what changed and *why it matters*. Record findings discovered by running
  the code; several are recorded that way and every one was a real bug in waiting.
- **Tests assert the design, not the implementation.** The merge-base test exists because getting it
  wrong is invisible in output; the fingerprint test exists because "a moved entity is the same
  entity" is a claim ARCH makes. Prefer tests of that kind.
- Run `uv run ruff format . && uv run ruff check . && uv run pytest -q` before committing.
