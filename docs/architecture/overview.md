<!-- metadata
id: ARCH-001
type: architecture
status: draft
author: Brent Runyan
created: 2026-08-01
dependencies: [RESEARCH-001, RESEARCH-002]
-->

# Architecture

How `cpp-semantic-diff` turns two commits into a ranked list of things a reviewer should look at.

The founding document ([RESEARCH-002](../research/semantic-diff-for-review.md)) establishes *what*
questions matter. This document establishes *how* the system answers them: the pipeline, the data
model, the identity scheme that everything else depends on, and the performance design that decides
whether the tool is usable at all.

Status: draft. Section 12 marks what v0 actually builds. This document is the design and stays
ahead of the code; what is *implemented* is tracked in [`STATUS.md`](../../STATUS.md), not here.

---

## 1 · Scope and non-goals

**In scope.** Given two commits of a C++ repository, produce a ranked, typed list of semantic
changes with provenance back to source lines, plus evidence attached from external analysis tools.

**Not in scope.**

- *Being a linter.* Diff-scoped static analysis (`clang-tidy`, Cppcheck, CodeQL) already exists and
  is good. This tool consumes their output as evidence; it does not reimplement them.
- *Judging intent.* The tool reports what changed and what it touches. Whether that was the right
  change is the reviewer's job.
- *Being an IDE.* No editor integration in the product core. JSON output; consumers are separate.
- *Whole-repo analysis for its own sake.* Every feature must earn its place by improving a *diff*.

---

## 2 · The central constraint

Every useful signal is a **two-point analysis**: model at commit A, model at commit B, diff the
models. Three consequences drive the entire design.

**Correspondence is the hard problem, not extraction.** Parsing C++ is solved. Deciding that
`Foo::bar` at commit A *is the same entity as* `Foo::baz` at commit B — or is not — is where the
product succeeds or fails. Get it wrong and the output is worse than `git diff`, because it is
wrong with authority. Section 5 is the most important section here.

**Cost is doubled by default, and that is unaffordable.** Measurements from the CodeAtlas research
(LLVM 21.1.8): FTXUI's 76 TUs took 137 s to parse through Python libclang bindings. Measured again
here against `fmt` (LLVM 18, WSL): 1.02 s per TU with the traversal pruned, so a full model is 51 s
per side and a review pays that twice. The split is parse 68 % / extract 32 % — the cost is the
*parser*, not the Python binding layer, which means faster traversal cannot rescue it and only
avoided work can. Section 9 makes the marginal cost proportional to *what the diff touched*, not to
repository size.

**Absence of a second point is fatal, not degrading.** A shallow clone, a base commit that never
built, a fixture with no history — these do not reduce output quality, they eliminate it. The
system must detect and report this condition as a hard failure rather than emitting a confident
half-answer.

---

## 3 · Pipeline

```
  commit A ──┐
             ├─→ acquire ─→ produce ─→ [ model cache ] ──┐
  commit B ──┘                                           │
                                                         ▼
                                                      match          (§5 identity)
                                                         │
                                                         ▼
                                                       diff          (§6 events)
                                                         │
                                                         ▼
                                                     classify        (§6.3 cause/cascade)
                                                         │
  external tools ─→ evidence adapters ──────────────→  enrich        (§8)
  head model ─────→ blast radius ───────────────────→    │
                                                         ▼
                                                       rank          (§7)
                                                         │
                                                         ▼
                                                      render         (§10 JSON → terminal/html)
```

Each stage is a pure function of the previous stage's output wherever it can be. That buys
testability (every stage has a fixture-in, fixture-out test), cacheability, and the ability to stop
and inspect at any boundary — which matters because most debugging here is "why did it think
*that*".

| Stage | Input | Output |
|---|---|---|
| **acquire** | two revs | two worktrees + compile databases + completeness record |
| **produce** | worktree + compile db | `Model` (§4) |
| **match** | two `Model`s | `Correspondence` — bipartite matching with per-pair tier |
| **diff** | two `Model`s + `Correspondence` | unordered `Event` list |
| **classify** | `Event` list | events with `caused_by` links |
| **enrich** | events + head model + tool output | events with blast radius and `Finding`s attached |
| **rank** | enriched events | ordered events with risk scores |
| **render** | ranked events | JSON document; terminal/HTML views consume it |

### 3.1 What "two commits" means

The primary use case is **merging a branch back into main**, which fixes the comparison semantics:

```
base = git merge-base main HEAD        head = HEAD
```

Three-dot, not two-dot. Comparing against `main`'s current tip would attribute every change
other people merged in the meantime to this branch — the reviewer would be shown work that is not
under review. The merge base is the only defensible base, and it is what `git diff main...HEAD`
already means.

**A compile database is required at both revisions.** This is a hard precondition, not a
degradation path (§11). Two consequences:

- It requires **configure, not a full build** — `cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`
  takes seconds where a build takes minutes. v0 configures both worktrees.
- **The tool chooses the generator.** Only Ninja and the Makefile generators emit a compile
  database. The others — Visual Studio, Xcode — configure *successfully* and emit nothing, so
  leaving the choice to CMake turns a hard precondition into a silent one. Configure selects Ninja
  unless the caller passes an explicit `-G`, and discards a cached build tree whose generator
  cannot emit one.
- **Repositories with generated sources need more than configure.** The CodeAtlas measurements
  found `clang-scan-deps` failing on 119 of opencv's 1,292 TUs (9.2 %), every one on a
  `opencl_kernels_*.hpp` that CMake emits during the build. Where this happens the acquire stage
  must either run enough of the build to generate them or report the shortfall as incompleteness
  (§4.4) — never parse the remainder silently.

**If the base does not build, the tool does not run.** This is deliberate and not an edge case
worth engineering around: the base is main, and a main branch that does not build is a problem this
tool is not the right response to. Fail with an actionable message and stop.

---

## 4 · The model

The IR at a single commit. Design constraint: **every field is either identity, diffable state, or
provenance** — nothing else belongs in it. If a field cannot be compared across commits or used to
locate a change in source, it does not go in the model.

### 4.1 Nodes

```
Node
  id            stable identity key (§5)
  kind          namespace | class | struct | union | function | method | field
                | enum | enumerator | alias | variable | template | file | target
  name          unqualified
  qualified_name
  signature     normalized: typedefs resolved, defaults included, cv/ref qualifiers explicit
  symbol        mangled symbol name, when the entity has linkage
  attrs         flat, comparable key→scalar map (§4.3)
  body_hash     normalized subtree hash — formatting- and comment-insensitive
  location      file, line span   ── provenance only, never identity
  tu            defining translation unit (identity-relevant for internal linkage)
```

`symbol` exists from day one even though the evidence layer (§8) is deferred. It is the join key
between the model and every binary-level tool — `abidiff`, `bloaty`, `nm`, coverage. Adding it
later means reprocessing every cached model.

`body_hash` is deliberately separate from `attrs`. An attribute change (`virtual` added, `const`
dropped, access widened) is a *specific* semantic event with a known meaning. A body change is
"something inside changed, look closer". Different events, different weights, different rendering.

### 4.2 Edges

```
Edge
  src, dst      node ids
  kind          contains | declares | defines | inherits | overrides | calls
                | instantiates | has-a | holds-a | uses-in-interface
                | uses-in-implementation | includes | needs-symbol | resolves-to
  attrs         e.g. virtual/direct for calls, access for inherits
  location      call site or declaration site — provenance
```

Edge kinds are drawn from the CodeAtlas C++ relation ontology (`RESEARCH-ONT-003` in `dev-team`),
which enumerated 61 relations across the compilation phases. This is a subset chosen for
diff-relevance; the full vocabulary is the reference, not the target.

### 4.3 Attributes must be flat

Every attribute is a scalar keyed by a stable name, because **an attribute diff is a first-class
event**. `attrs["virtual"] = false → true` becomes `NodeAttrChanged(virtual, false, true)`
mechanically. Nested or free-form attributes would require bespoke diff logic per kind, which is
how these systems rot.

### 4.4 Completeness record

Every `Model` carries how it was built: TUs attempted, parsed, failed (with reasons); whether a
build was available; which producers ran; producer version. This is not diagnostics — it is
load-bearing input to §11.

---

## 5 · Identity

The crux. Without stable correspondence, a renamed-but-unchanged function reads as delete + add and
the tool is a worse `git diff` with more machinery.

No single scheme survives all edits, so match in **tiers, most-confident first**, each tier running
only over the residue the previous tiers left unmatched. The tier that produced a match is recorded
on the match and propagates into the confidence of every event derived from it.

| Tier | Key | Survives | Broken by |
|---|---|---|---|
| **1 · declared** | libclang USR — kind + qualified name + signature | body edits, file moves, reformatting | rename, signature change, namespace move |
| **2 · structural** | `body_hash` — normalized subtree hash | rename, move, reformatting | any body edit |
| **3 · positional** | (parent id, kind, ordinal) | renames of unnamed things | reordering, parent change |
| **4 · similarity** | weighted name + signature + body similarity over candidate pairs | rename *and* edit together | genuine rewrites |

Tier 3 exists for entities with no name at all: lambdas, anonymous namespaces and structs, unnamed
parameters. Tier 4 is the `git -M` analogue, with the same threshold-tuning problem and the same
answer: a configurable similarity floor, bounded to candidate pairs sharing a parent and kind so it
stays O(n²) over a small residue rather than the repository.

**Matching is bipartite and one-to-one.** Splits (one function became two) and merges are not
forced into a match — they surface as correlated add/remove residue and are reported as such.
Pretending a split is a rename is exactly the confident-and-wrong failure mode to avoid.

### C++ hazards the scheme must handle explicitly

- **Templates.** The template and each instantiation need separate identity. Instantiation sets
  differ between commits for reasons unrelated to the change — an unrelated new call site can
  materialize an instantiation. Instantiation-only deltas must be suppressible.
- **Overloads.** Name is never identity. Signature normalization (typedef resolution, default
  arguments, cv/ref qualification) is required for tier 1 to work at all.
- **Internal linkage and anonymous namespaces.** USRs incorporate the file path, so a file rename
  breaks tier 1 for everything in them. Identity must include the TU, and file renames must be
  resolved from git before matching — not discovered by the matcher.
- **Macros.** An entity's very existence may depend on a macro that itself changed. The model is
  one preprocessor configuration; see §11.
- **ODR.** The same declared identity can appear in multiple TUs with different definitions. Dedup
  by USR is a correctness requirement, not an optimization — the CodeAtlas measurements found
  15,656 method cursors collapsing to 161 classes in FTXUI, because every header is reparsed per TU.

---

## 6 · The delta

### 6.1 Event types

A closed, typed set. New signals become new event kinds, never free-text.

**Node** — `NodeAdded` · `NodeRemoved` · `NodeMoved` (parent changed) · `NodeRenamed` ·
`NodeAttrChanged(attr, old, new)` · `NodeBodyChanged`

**Edge** — `EdgeAdded` · `EdgeRemoved` · `EdgeRetargeted` · `EdgeKindChanged`

**Derived** — `BecameUnreachable` · `BecameReachable` · `CycleIntroduced` · `FanInChanged` ·
`LayerViolationIntroduced` · `SignatureIncompatible`

Derived events are computed from the two models plus graph algorithms, not read off either one.
They are the answer to the founding document's §1 point that the most important consequences of a
change are never *in* the change.

### 6.2 Event shape

```
Event
  id
  kind
  subject       node or edge id
  payload       kind-specific
  caused_by     optional event id (§6.3)
  provenance    source hunks: (side, file, line span) — bidirectional with the text diff
  confidence    derived from match tier + model completeness
```

**Provenance must be bidirectional.** The reviewer has to get from an event to the lines, *and*
from a line in the ordinary diff to the events it caused. Without the reverse direction the tool is
a second disconnected artifact to read alongside the diff, which is a net increase in work.

### 6.3 Cause and cascade

Rename one function and 200 call edges "change". That is one event with 200 consequences, not 201
events. Consequences are linked to causes by **an explicit rule table**, not inference:

- `NodeRenamed(F)` causes every `EdgeRetargeted` whose endpoint is F
- `NodeRemoved(C)` causes `NodeRemoved` for everything C contained
- `NodeAttrChanged(signature)` causes `SignatureIncompatible` on callers

Events with no matching rule get no cause link and are shown. **Collapse happens at render, never
at diff** — the JSON always carries the full event set; the view hides consequences under causes by
default. When cause attribution is uncertain, default to showing, because a hidden real change is a
far worse failure than a redundant one.

---

## 7 · Ranking and attention

The founding document's §4 argues that telling the reviewer *what to look at first* may be worth
more than any individual diff. Ranking is therefore a first-class stage, not a rendering detail.

Risk score per event, from:

- **Blast radius** — transitive reverse reachability of the subject over `calls` + `contains` +
  `uses` edges, computed on the head model. Bounded depth; the count matters more than the set.
- **Exposure** — is the subject reachable from a public header, or internal?
- **Novelty** — added and removed outrank modified; attribute changes to `virtual`, `const`,
  access, and signature outrank body changes.
- **Confidence** — a low-confidence event ranks *up*, not down. Uncertainty is a reason for a human
  to look, not a reason to hide.

**Decomposition** falls out of the same data: partition the event graph into connected components.
A change whose delta has three mutually disconnected components is three changes, and saying so is
the founding document's §3.11 intent-conformance signal at nearly zero cost.

---

## 8 · Evidence

Each external tool is an adapter with a uniform contract:

```
adapter(artifacts_a, artifacts_b) -> [Finding(node_id, kind, severity, detail, source_tool)]
```

Findings attach to the nodes they concern, which is what turns four separate reports into one
sentence: *this class — ABI-incompatible, +2 KB, 0 % coverage on new members*.

The join is via `Node.symbol` (demangled and matched for `abidiff`, `bloaty`, `nm`) or via
file/line range containment (coverage, `clang-tidy`, Cppcheck). Planned adapters, in the order
[RESEARCH-002 §7](../research/semantic-diff-for-review.md) recommends: `abidiff`, `diff-cover`,
`bloaty --diff`, `pahole`, then diff-scoped static analysis.

All deferred past v0 — but the join key ships in v0 (§4.1), because retrofitting it invalidates
every cached model.

---

## 9 · Caching and the performance budget

**Budget: a review touching ≤ 20 TUs completes in under 30 s on a warm cache.** This number is a
design constraint, not an aspiration — it is what forces the design below, and it should be
measured continuously rather than asserted.

Naive per-commit model caching is not enough, because the first review against any new base pays
full price. The unit of caching is therefore the **translation unit**, not the commit:

```
per-TU cache key = hash(TU source content, transitive header content, compile flags, producer version)
```

A commit's model is assembled from cached per-TU partial models plus a reparse of only the invalid
ones. The marginal cost of a review becomes proportional to what the diff touched.

Computing the invalidation set requires the transitive include closure, which `clang-scan-deps`
provides cheaply — the CodeAtlas measurements put opencv's 1,292 TUs at 14.1 s, rocksdb's 369 at
4.3 s. A header change invalidates every TU that includes it, so the pathological case (touch a
widely-included header) degrades to a full rebuild and must be reported as such rather than
silently taking ten minutes.

Two further consequences:

- **The base model is almost always reusable.** Every review against the same base shares it, and
  CI can populate it ahead of time.
- **Cache correctness is a test target.** A stale cache produces confidently wrong output, which is
  the worst failure this system can have. Cache-invalidation tests are not optional.

---

## 10 · Output contract

**JSON is the product; the terminal is a consumer.** A versioned JSON document is the primary
output, and every view — terminal, HTML, future PR annotation, future editor plugin — reads it.
This keeps delivery surfaces from leaking into the analysis core, which is the mistake that made
CodeAtlas TypeScript.

```
{
  meta:        { tool version, schema version, revs, timings }
  completeness:{ per-side completeness records (§4.4) }
  entities:    { node id → node summary, for events to reference }
  events:      [ Event ]              -- complete, uncollapsed
  findings:    [ Finding ]            -- evidence, attached by node id
  ranking:     [ event id ]           -- ordered
  summary:     { lines_changed, events_reported, compression, high_risk_count }
}
```

`summary.compression` is the product's own success metric and prints on every run: *2,431 lines
changed → 9 semantic events, 2 high-risk*. If that ratio is not dramatic on real commits, the
thesis in RESEARCH-002 is wrong, and the design should surface that in week one rather than month
six.

---

## 11 · Confidence and degradation

**A tool that silently degrades is worse than no tool**, because it converts absence of evidence
into apparent evidence of absence. Three rules:

1. Every event carries confidence derived from its match tier and the completeness of both models.
2. Any output built on an incomplete model states what was missing, at the top, not in a log.
3. Conditions that eliminate the analysis — shallow clone, unresolvable base, no compile database —
   are **hard failures with actionable messages**, never partial output.

Two systematic limits get stated in the output itself, every time:

- **One model is one preprocessor configuration.** Claims are conditional on the configuration
  analyzed. Variant coverage (RESEARCH-002 §3.10) is unsolved and must not be implied.
- **Static call graphs are unsound.** Virtual dispatch, function pointers, and inlining mean a
  reachability delta is a lower bound, not a fact, and must be rendered as one.

---

## 12 · v0

The smallest slice that tests the thesis end to end.

**In:** namespace / class / function / method / field nodes · `contains`, `calls`, `inherits`,
`overrides` edges · **tier-1 identity only** · node add/remove/attr/body and edge add/remove
events · JSON + terminal output · the compression metric · fixtures `fmt` and `spdlog`.

**Out, deliberately:** rename detection (tiers 2–4), cascade collapse, evidence adapters, derived
events, HTML, blast radius.

Tiers 2–4 are excluded on purpose: RESEARCH-002 §7 step 4 says ship tier-1 and *measure how badly
renames hurt* before building the machinery. That measurement is a v0 deliverable.

### Technology

| Concern | Choice | Rationale |
|---|---|---|
| IR types, validation, JSON Schema | **pydantic v2** | schema generated from types, not maintained beside them |
| C++ parsing | **libclang Python bindings** over a **discovered system LLVM ≥ 16** | full control over extraction; USRs free; clangd needs O(N) round-trips and keeps a private RIFF index. The bindings come from the wheel, the library does not — see below |
| Graph algorithms | **networkx** | sufficient at fixture scale; `rustworkx` is the drop-in escape hatch if opencv-scale hurts |
| Git | **subprocess `git`** | `git worktree add` is exactly the primitive needed; fewer dependencies than pygit2 |
| CLI | **typer** | |
| Terminal render | **rich** | |
| Env, test, lint | **uv · pytest · ruff** | matches the existing `codeatlas-cpp/graph` convention |

Python 3.12+.

**The parser is a precondition, not a dependency.** A libclang that cannot reach the compiler's
builtin headers does not fail — it returns a *truncated* AST and raises nothing, and two truncated
models omit the same declarations and agree with each other perfectly. That is §11's failure mode
with no symptom, so the parser gets the §11 treatment: the library is discovered on the system
(never the one bundled in the `libclang` wheel, which ships without builtin headers), probe-parsed
against `<stddef.h>` and `<stdarg.h>` before any model is built, and refused outright if the probe
is not clean. Per-TU fatal diagnostics are checked for the same reason at a finer grain. Its release
triple identifies the producer and is a component of every cache key, so a model built by a
different parser is a different model.

### Package layout

Present tense is what exists; the rest is planned.

```
cpp_semantic_diff/
  model/       IR types, schema                        ✓
  acquire/     git revisions · worktrees · compile db  ✓
  produce/     toolchain ✓ · libclang extraction · per-TU cache
  identity/    tiered matching
  delta/       events, cause rules
  evidence/    tool adapters                (post-v0)
  rank/        blast radius, scoring
  render/      json · terminal · html
  errors.py    the precondition taxonomy (§11)         ✓
  cli.py                                               ✓
```

`acquire` and `produce` are stages of the pipeline (§3) and named for them; the earlier draft called
the latter `producers` and grouped git with it, which put the two-point machinery inside a
single-point stage.

---

## 13 · Open questions

Resolved and moved into the body: the compile-database requirement at both revs and the
base-does-not-build case (§3.1); which libclang, and proving it is complete (§12, and it was not an
academic question — the bundled library was silently truncating every AST). Remaining:

- **Generated sources.** §3.1 states the problem but not the policy. Configure-only is the v0
  design; the threshold at which a repository needs a partial build to be analyzable at all is
  unmeasured, and `fmt`/`spdlog` will not reveal it.
- **Threshold tuning for tier 4.** Git's `-M` has one knob and decades of tuning. Assume this needs
  per-repository calibration and design the knob in early.
- **Where does the reviewer actually read this?** CLI is right for v0 and probably wrong as the
  product. The JSON-first contract defers the decision without prejudging it.
