<!-- metadata
id: ARCH-001
type: architecture
status: draft
author: Brent Runyan
created: 2026-08-01
dependencies: [RESEARCH-001, RESEARCH-002]
kind: living
-->

# Architecture

*Living document: kept true as the code changes.*

How `cpp-semantic-diff` turns two commits into a ranked list of things a reviewer should look at.

The founding document ([RESEARCH-002](research/RESEARCH-002-semantic-diff-for-review.md)) establishes *what*
questions matter. This document establishes *how* the system answers them: the pipeline, the data
model, the identity scheme that everything else depends on, and the performance design that decides
whether the tool is usable at all.

Status: draft. Section 13 marks what v0 actually builds. This document is the design and stays
ahead of the code; what is *implemented*, and what is next, is in [`ROADMAP.md`](../ROADMAP.md), not here.

**Section numbers are permanent.** Code comments cite them. A section that is removed leaves a stub
saying it is retired and what replaced it, and a test fails if a comment cites a section that does
not exist.

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
here with the real extractor against `fmt` (LLVM 18, WSL): **2.5 s per TU**, so a full model is
~125 s per side and a review pays that twice. Roughly half is clang parsing and half is walking the
AST; the cost is the *work itself*, not the Python binding layer, which means only avoided work can
rescue it. Section 9 makes the marginal cost proportional to *what the diff touched*, not to
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
                                                       diff          (§6 change events)
                                                         │
                                                         ▼
                                                      derive         (§6.1 derived change events)
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
| **diff** | two `Model`s + `Correspondence` | `ChangeEvent` list, deterministically ordered |
| **derive** | change events + two `Model`s + `Correspondence` | the same change events plus derived ones (§6.1) |
| **classify** | change events + two `Model`s + `Correspondence` | change events with `caused_by` links |
| **enrich** | change events + head model + tool output | change events with blast radius and `Finding`s attached |
| **rank** | enriched change events | ordered change events with risk scores |
| **render** | ranked change events | JSON document; terminal/HTML views consume it |

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
  sites         every declaration's location ── provenance
  tu            defining translation unit (identity-relevant for internal linkage)
```

`symbol` exists from day one even though the evidence layer (§8) is deferred. It is the join key
between the model and every binary-level tool — `abidiff`, `bloaty`, `nm`, coverage. Adding it
later means reprocessing every cached model.

`body_hash` is deliberately separate from `attrs`. An attribute change (`virtual` added, `const`
dropped, access widened) is a *specific* change event with a known meaning. A body change is
"something inside changed, look closer". Different change events, different weights, different rendering.

A line span starts where the declaration's text starts, not at its name: a `template <...>`
header or a return type on the line above is part of the declaration. `sites` holds every place
the entity is declared or defined, such as a header declaration and the definition in a source
file. It is the one field merged as a union across observations rather than taken from the
observation that wins, and it is never compared or hashed. A change event points at every site,
so an edited header declaration leads to the change event about it (§6.2).

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
change event**. `attrs["virtual"] = false → true` becomes `NodeAttrChanged(virtual, false, true)`
mechanically. Nested or free-form attributes would require bespoke diff logic per kind, which is
how these systems rot.

### 4.4 Completeness record

Every `Model` carries how it was built: TUs attempted, parsed, failed (with reasons); whether a
build was available; which producers ran; producer version; and what the producer cannot see
whatever the source (§11). This is not diagnostics — it is
load-bearing input to §11.

---

## 5 · Identity

The crux. Without stable correspondence, a renamed-but-unchanged function reads as delete + add and
the tool is a worse `git diff` with more machinery.

No single scheme survives all edits, so match in **tiers, most-confident first**, each tier running
only over the residue the previous tiers left unmatched. The tier that produced a match is recorded
on the match and propagates into the confidence of every change event derived from it.

| Tier | Key | Survives | Broken by |
|---|---|---|---|
| **1 · declared** | libclang USR — kind + qualified name + signature | body edits, file moves, reformatting | rename, signature change, namespace move |
| **inline namespace** | tier 1's key, with a bumped inline namespace's old path rewritten to its new one | an inline namespace version bump (`v1` to `v2`) | a plain namespace rename, a bump in which nothing inside pairs |
| **name** | kind + name + file and TU for internal linkage + paired parent; functions and methods only | signature change: parameters, return type, `const` | rename, namespace move, an ambiguous name |
| **2 · structural** | `body_hash` — normalized subtree hash | rename, move, reformatting | any body edit |
| **3 · positional** | (parent id, kind, ordinal) | renames of unnamed things | reordering, parent change |
| **4 · similarity** | weighted name + signature + body similarity over candidate pairs | rename *and* edit together | genuine rewrites |

The **inline namespace** tier runs over what tier 1 left. Where one inline namespace replaced
one other under the same paired parent, it rewrites the old namespace's path in base USRs, only
where a USR starts, and keys the result as tier 1 does. The mapping stands only if something
inside pairs through it, so the namespaces read as one rename and nothing in them moves
([ADR-044](adr/ADR-044-inline-namespace-bump-pairs.md)).

The **name** tier runs over what the tiers before it left, and only over functions and methods, the only
entities whose USR holds more than their kind, name and scope. It pairs a function with itself
after its signature changed. Its parent must already be paired, so it never reports a move. A
name with one leftover on each side pairs; leftovers on both sides that are not one to one pair
none and are reported as an ambiguous pairing; there is no tie-break
([ADR-036](adr/ADR-036-functions-pair-by-name.md)). The signature, the template parameters and
the rest are then compared state.

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
- **Overloads.** Name is never identity on its own. Signature normalization (typedef
  resolution, default arguments, cv/ref qualification) is required for tier 1 to work at all,
  and the name tier pairs by name only where the name leaves no choice.
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

### 6.1 Change event types

A closed, typed set. New signals become new change event kinds, never free-text.

**Node** — `NodeAdded` · `NodeRemoved` · `NodeMoved` (parent changed) · `NodeRenamed` ·
`NodeAttrChanged(attr, old, new)` · `NodeBodyChanged` ·
`NodeSignatureChanged(old_signature, new_signature)`

Most node change events can happen to any kind of entity. Two cannot, and the set says which
kinds they apply to rather than encoding it in their names
([ADR-035](adr/ADR-035-signature-and-body-apply-to-functions.md)):

| Change event | Applies to |
|---|---|
| `NodeSignatureChanged` | function, method |
| `NodeBodyChanged` | function, method |
| every other node change event | every node kind |

"Method" includes constructors, destructors and conversion operators. The table is
`APPLIES_TO` in `model/events.py`, and the diff stage reads it.

`NodeSignatureChanged` is its own kind, not `NodeAttrChanged(signature)`, because the
signature is a field of the node (§4.1), not an attribute. The signature spells the return
type and a method's `const`, so where it changed, those two attributes are not also reported
as `NodeAttrChanged`.

**Edge** — `EdgeAdded` · `EdgeRemoved` · `EdgeRetargeted` · `EdgeKindChanged` ·
`EdgeAttrChanged(attr, old, new)`

`EdgeAttrChanged` was added after v0 shipped without it, under this section's own rule that new
signals become new kinds. §4.2 gives edges `attrs` and the producer populates them — `access` on
`inherits`, virtual/direct on `calls` — so a base class going **public → private** between two
otherwise unchanged classes was a real C++ breaking change with no change event to carry it. Worth
knowing before relying on it: it fires correctly but is *rare* on real repositories. Neither
fixture produced one across 300 commits, because both edge attributes tend to co-occur with edits
that change a USR, and the entities are then reported as removed and added instead.

`EdgeRetargeted` is **refused, not deferred**. An edge has no identity of its own — its id *is*
`src|kind|dst` (§4.2) — so "this edge was retargeted from B to C" is a claim that B and C are the
same entity, which is a node-identity judgement the one-to-one matcher (§5) has already declined to
make. Remaking it here, on weaker evidence than the matcher had, is the confident-and-wrong failure
this design exists to avoid. It surfaces as remove + add: the same information, without the invented
correspondence. The kind stays in the closed set so that the refusal is explicit rather than an
omission someone later reads as an oversight.

`EdgeKindChanged` *is* emitted, and the asymmetry is the argument: an edge's endpoints are identity
and belong to the matcher, but its kind is diffable state. Between two already-paired entities, a
relation that was `calls` and is now `overrides` needs no new identity claim.

**Derived** — `BecameUnreachable` · `BecameReachable` · `CycleIntroduced` · `FanInChanged` ·
`LayerViolationIntroduced` · `CallerOfChangedSignature(caller, callee, old_signature, new_signature, caller_edited)`

Derived change events are computed from the two models plus graph algorithms, not read off either one.
They are the answer to the founding document's §1 point that the most important consequences of a
change are never *in* the change. The derive stage (§3) computes them.

`CallerOfChangedSignature` is the only one v0 emits
([ADR-040](adr/ADR-040-callers-of-a-changed-signature-are-derived.md)). One per call G → F that
exists on both sides, where F has a `NodeSignatureChanged`. The subject is the head call edge, so
its home is the caller and its provenance is the call sites. A new caller, or a new call, is left
out: it is already an `EdgeAdded`, written against the new signature. `caller_edited` says whether
G has its own body or signature change in the diff. The kind states an observation, not a verdict:
nothing checks whether the call still compiles or behaves the same. It is a lower bound: calls the
model misses (calls that depend on a template parameter, calls from initializers, calls through a
function pointer) get none. The earlier draft named it `SignatureIncompatible`.

### 6.2 Change event shape

```
ChangeEvent
  id
  kind
  subject       node or edge id
  payload       kind-specific
  caused_by     optional change event id (§6.3)
  provenance    source hunks: (side, file, line span) — bidirectional with the text diff
  confidence    derived from match tier (or why unpaired) + model completeness
```

**Provenance must be bidirectional.** The reviewer has to get from a change event to the lines,
*and* from a line in the ordinary diff to the change events it caused. Without the reverse
direction the tool is a second disconnected artifact to read alongside the diff, which is a net
increase in work.

**A signature change has its callers as change events.** The call edges of a paired function map
through the correspondence (§6.1), so a changed signature produces no edge change events. Each call
into it that exists on both sides is a `CallerOfChangedSignature` instead, caused by the signature
change (§6.3). They replace the list of callers the output carried beside each
`NodeSignatureChanged` until then
([ADR-040](adr/ADR-040-callers-of-a-changed-signature-are-derived.md)).

### 6.3 Cause and cascade

Rename one function and 200 call edges "change". That is one change event with 200 consequences, not
201 change events. Consequences are linked to causes by **an explicit rule table**, not inference:

| Rule | Cause | Consequence | Status |
|---|---|---|---|
| contained-removal | `NodeRemoved(C)` | `NodeRemoved` for everything C contained | active |
| contained-addition | `NodeAdded(C)` | `NodeAdded` for everything C contains | active |
| edge-lost-its-endpoint | `NodeRemoved(N)` | `EdgeRemoved` for edges touching N | active |
| call-moved-to-another-overload | `EdgeAdded(caller→G)` | `EdgeRemoved(caller→F)`, F and G overloads of one name | active |
| edge-gained-its-endpoint | `NodeAdded(N)` | `EdgeAdded` for edges touching N | active |
| rename-retargets-edges | `NodeRenamed(F)` | `EdgeRetargeted` at F | **unreachable** |
| signature-change-breaks-callers | `NodeSignatureChanged(F)` | `CallerOfChangedSignature` for each call into F | active |

**That is the whole table, and it is closed the way §6.1's change event set is closed.** The rows are
`CAUSE_RULES` in `classify/rules.py` and the Status column is its `RuleStatus`; nothing outside that
module decides what causes what. There is no second class of rule — no "implementation detail" tier
that the design declines to name — because a cause link is exactly the claim this document exists to
justify: it is what render hides a change event under, and a rule that lives only in the code is a
reviewer's change disappearing for a reason the design cannot be read to explain. So a rule found by
running the tool belongs here at the moment it is added, with where it came from stated in place —
which is what the rest of this section is. Order is part of the table: the first rule that finds a
cause wins, which matters wherever two rules share a consequence kind.

The two **addition** rules and the two **edge-endpoint** rules were missing from the earlier draft,
and between them they carry nearly all of v0's actual cascade volume: without them, removing one
class leaves every containment and call edge it touched standing as an unexplained top-level change
event, and adding a class of twenty methods reads as twenty-one change events while the identical
deletion reads as one.

`call-moved-to-another-overload` is the row this section did not reason its way to. It came from
running the tool: googletest gave `AssertHelper::AssertHelper` a `std::string_view` overload, every
`TEST` body stopped calling the `const char *` one, and the 28 resulting `EdgeRemoved` change events have
*both* endpoints still present — the old overload is still declared, it is merely no longer called —
so no endpoint rule reaches them and they were the bulk of what stood at top level after every other
cascade had collapsed. With the rule in, leveldb `HEAD~10` goes from **39 top-level change events to 11**,
and `fmt` and `spdlog` are unchanged to the digit, which is the check that it collapses a real
relation rather than whatever happens to be nearby. A call genuinely dropped between two survivors
still gets no cause and is still shown; that is this section's last paragraph, not a gap in this
rule.

**It is not `EdgeRetargeted` readmitted under another name**, and §6.1 is why that distinction has
to be argued rather than assumed. That change event is refused because "retargeted from F to G" asserts
that F and G are the same entity, which is a node-identity claim the one-to-one matcher declined.
This rule asserts nothing of the kind: F and G are two distinct overloads, both present at head, and
the claim is only that one call site moved *within* an overload set — a fact about the caller's own
outgoing edges that needs no opinion about whether F and G are the same thing. The set is
`qualified_name` equality with the edge kind held fixed, which is what an overload set is, since
overloads differ exactly in signature (§5). The cause is the `EdgeAdded` rather than G's own
`NodeAdded`, so that every link carries one fact: the added edge is already linked to the added node
by `edge-gained-its-endpoint`, and both collapse under it. The row sits below
`edge-lost-its-endpoint` because both explain an `EdgeRemoved` and the order is the tie-break — an
endpoint that actually went away is a better explanation than one the caller merely stopped using.

`rename-retargets-edges` is **unreachable, not dormant** — and it is not revived by tier 2. Because
base edges are rewritten into head coordinates through the correspondence before comparison, a
rename produces *no edge change events at all*, rather than 200 that need collapsing. That is a stronger
answer to the problem this section opens with. Tier 2 makes `NodeRenamed` reachable but adds no edge
change events beside it; only reversing the edge-identity decision in §6.1 would make this rule fire. The
rule is kept and implemented rather than deleted, because deleting it would lose that argument.

`signature-change-breaks-callers` links each `CallerOfChangedSignature` to the signature change of
its callee: the head destination of its subject edge, so there is exactly one candidate. The derive
stage creates those change events
([ADR-040](adr/ADR-040-callers-of-a-changed-signature-are-derived.md)). Classify links causes among
change events that already exist; it never synthesizes one. A stage that may create change events is
a stage that can hide a change inside a cause. Under one signature change, views list unedited
callers first.

Change events with no matching rule get no cause link and are shown. **Collapse happens at render, never
at diff** — the JSON always carries the full change event set; the view hides consequences under causes by
default. When cause attribution is uncertain, default to showing, because a hidden real change is a
far worse failure than a redundant one.

---

## 7 · Ranking and attention

The founding document's §4 argues that telling the reviewer *what to look at first* may be worth
more than any individual diff. Ranking is therefore a first-class stage, not a rendering detail.

Risk score per change event, from:

- **Blast radius** — transitive reverse reachability of the subject over `calls` + `contains` +
  `uses` edges, computed on the head model. Bounded depth; the count matters more than the set.
- **Exposure** — is the subject reachable from a public header, or internal?
- **Novelty** — added and removed outrank modified; attribute changes to `virtual`, `const`,
  access, and signature outrank body changes.
- **Confidence** — a low-confidence change event ranks *up*, not down. Uncertainty is a reason for a human
  to look, not a reason to hide.

**Decomposition** falls out of the same data: partition the change event graph into connected components.
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
[RESEARCH-002 §7](research/RESEARCH-002-semantic-diff-for-review.md) recommends: `abidiff`, `diff-cover`,
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
  completeness:{ per-side completeness records (§4.4), standing limits (§11),
                 where the uncovered changes are }
  entities:    { node id → node summary, for change events to reference }
  events:      [ ChangeEvent ]        -- complete, uncollapsed
  findings:    [ Finding ]            -- evidence, attached by node id
  ranking:     [ change event id ]    -- ordered
  summary:     { lines_changed, events_reported, compression, high_risk_count,
                 changed_cpp_lines, uncovered_lines }
}
```

`summary.compression` is the product's own success metric and prints on every run: *2,431 lines
changed → 9 change events, 2 high-risk*. If that ratio is not dramatic on real commits, the
thesis in RESEARCH-002 is wrong, and the design should surface that in week one rather than month
six.

On its own the ratio improves when the model sees less. So the sentence also states how many of
the changed C++ lines no change event explains: *580 lines changed → 181 change events, 2
high-risk · 37 of 412 changed C++ lines explained by no change event*. A changed line is one
carrying a changed token, so whitespace and comment edits do not count; it is explained when a
change event's provenance contains it, which makes the uncovered count a lower bound
([ADR-042](adr/ADR-042-uncovered-changes-are-tokens-on-no-event-line.md)).

---

## 11 · Confidence and degradation

**A tool that silently degrades is worse than no tool**, because it converts absence of evidence
into apparent evidence of absence. Three rules:

1. Every change event carries confidence derived from its match tier and the completeness of both models.
   An unpaired subject has no tier; it carries why it is unpaired instead: no candidate, a candidate,
   or an ambiguous pairing. Having no partner is not doubt
   ([ADR-034](adr/ADR-034-uncertainty-is-doubt-about-the-pairing.md)).
2. Any output built on an incomplete model states what was missing, at the top, not in a log.
3. Conditions that eliminate the analysis — shallow clone, unresolvable base, no compile database —
   are **hard failures with actionable messages**, never partial output.

Rule 2 covers inputs as well as models. When a diff exceeds `diff.renameLimit`, git skips inexact
rename detection and says so only on stderr; the rename map (§5) then holds exact renames only.
The output states this beside the completeness records, and every unpaired entity in a file the
diff added or deleted counts as having a candidate, never as having none (rule 1). The limit is
not lifted: `-l0` is potentially quadratic on the large diffs that hit it.

Rule 2 also covers what a complete model still misses. Every run counts the changes in C++ files,
headers included, that no change event explains (§10), so a change the model cannot see reads as
unexplained rather than as no change.

Three systematic limits get stated in the output itself, every time:

- **One model is one preprocessor configuration.** Claims are conditional on the configuration
  analyzed. Variant coverage (RESEARCH-002 §3.10) is unsolved and must not be implied.
- **Static call graphs are unsound.** Virtual dispatch, function pointers, and inlining mean a
  reachability delta is a lower bound, not a fact, and must be rendered as one.
- **Some of the code is not visible to the model.** libclang does not expose implicit special
  members, so a destructor that removes a class's implicit move operations changes nothing the
  model holds; and macros are not modelled. Each model's completeness record lists these
  (`not_visible`), and the output states them beside the other two
  ([ADR-029](adr/ADR-029-libclang-limits-are-reported.md)).

---

## 12 · Reproducibility

§11 is about not claiming more than the model supports. This section is about the model being the
same thing twice — which is a precondition for §11's confidence numbers meaning anything, and which
the rest of the document assumes without ever requiring.

Two distinct properties, and the first is where the damage is:

**1 · Same-run consistency — both sides share one toolchain identity.** A two-point analysis
subtracts one model from another. If the two were produced by different parsers, different system
headers, or different target triples, the subtraction yields change events that describe the *environment*
rather than the change. This is a worse failure than anything in §11: not a missed change, an
invented one, rendered with full confidence and indistinguishable in the output from a real one.
`std::string` gaining a member because the base was parsed against libstdc++ 12 and the head
against 14 is a `NodeAdded` like any other.

**2 · Cross-run equality — identical inputs produce an identical canonical document.** Same revs,
same toolchain identity, any machine, any day: byte-identical output over the canonical subset
defined below. Without this there are no golden-file tests, the §10 compression metric cannot be a
CI gate, and a user's bug report cannot be reproduced.

### 12.1 Toolchain identity

Identity is the full set of inputs that can change an AST. The determinants are:

| Component | Why it changes the model |
|---|---|
| libclang release triple | parser behaviour, USR construction |
| target triple | `sizeof`, alignment, and every `__x86_64__`-conditional declaration |
| resource directory | which builtin headers the parse resolves |
| C++ standard library and version | libstdc++ 12 vs 14 vs libc++ declare different `std::` |
| extractor code digest | the walk is half the producer — already hashed in `produce/cache.py` |
| include-path environment | `CPATH`, `CPLUS_INCLUDE_PATH` silently rewrite the search order |

**Anything that can change the AST without changing the identity is a cache-correctness bug**, not
a documentation gap, because §9 keys the per-TU cache on this value.

But the identity is not the whole key, and that distinction turned out to matter more than the
list. §9's key also content-hashes every member of the include closure, and the closure contains
the system headers: a translation unit including `<string>`, `<cstddef>` and `<stdarg.h>` yields
151 files, 68 of them libstdc++ and 14 of them clang's own builtin headers. **So the standard
library and the resource directory are already keyed by content** — two hosts agreeing on
`libclang 18.1.3` and differing in libstdc++ read different bytes and miss, correctly. The same
holds for `CPATH`-style search-order changes, which resolve to different files and so change the
closure.

What content-hashing cannot see is the **target triple**: identical headers under a different
target yield different `__x86_64__`-conditional declarations and different `sizeof`, with
byte-identical digests. That is the one determinant the identity has to carry itself, and it is
read from the verified library's own probe translation unit rather than from a sibling binary.

The lesson generalizes: before adding a component to an identity, establish which hazard the
*whole* key already covers. Every redundant component costs a mass invalidation to defend against
something already defended.

### 12.2 The invariant is checked, not assumed

**`diff` refuses two models whose toolchain identity differs**, with the §11 treatment: a hard
failure naming both identities, never a diff. The cache key makes this true *incidentally* —
a changed producer misses cache and forces a reparse — but that is a consequence of a caching
decision, not a guarantee. A property this load-bearing is asserted where it is relied on: the
diff stage compares both sides' `Completeness.producers` and `producer_version` and raises
`ToolchainMismatchError` (`diff/differ.py`, since `9cdd2e3`).

### 12.3 What is deliberately not pinned

§13 discovers a system libclang rather than using the one bundled in the wheel, because the
bundled library ships without builtin headers and returns truncated ASTs silently. That is the
right trade — a variable parser beats a quietly wrong one — but it means **the single most
determinative input to the model is host-dependent by choice.** The compensation is that identity
is *recorded and folded into every cache key*, so environment variation is detected and invalidates
rather than being prevented. Reproducibility here is achieved by detection, not by pinning, and
that only holds while §12.1 is complete.

The compile database is the same shape of problem one layer up: `compile_commands.json` is
generated by configure (§3.1), which depends on the CMake version and the detected host compiler.
Compile flags are in the cache key, so a change is caught — but **both worktrees must be configured
by the same CMake and generator**, or the flag differences are attributed to the branch.

### 12.4 The canonical document

§10's `meta.timings` makes two runs over identical input differ by construction, so the output
cannot be compared to itself. The JSON therefore has a **canonical subset**: everything except
wall-clock timings, absolute paths, and the tool's own invocation details. Paths are repo-relative;
`meta` retains tool, schema, revs and toolchain identity, which are inputs, and excludes timings,
which are observations. Equality tests and CI gates run over the canonical subset; the full
document is what a human reads.

**Ordering must come from explicit keys.** §3 calls change events "deterministically ordered" without
saying from what. Every stage that emits a sequence sorts on a stable total order over ids, and no
stage may let iteration order stand in for it — Python randomizes string hashing per process under
`PYTHONHASHSEED`, so a set of node ids iterates differently on every run and produces output that
is non-reproducible on one machine, let alone across two.

---

## 13 · v0

The smallest slice that tests the thesis end to end.

**In:** namespace / class / function / method / field nodes · `contains`, `calls`, `inherits`,
`overrides` edges · **tier-1 identity, plus the inline namespace tier and the name tier for functions and methods** · node
add/remove/attr/body/signature and edge add/remove/kind/attr change events · one derived change
event, `CallerOfChangedSignature` · JSON + terminal output · the compression metric · fixtures `fmt` and
`spdlog`, with `leveldb` added later ([v0 tracker](../reports/2026-08-01--v0-build/STATUS.md) §3).

**Out, deliberately:** rename detection (tiers 2–4), evidence adapters, derived change events, HTML.

**One exception to the derived change events being out:** `CallerOfChangedSignature`, from a
derive stage between diff and classify. It is how ADR-030's v0 tells a reviewer which callers a
signature change may affect ([ADR-040](adr/ADR-040-callers-of-a-changed-signature-are-derived.md)).

**Prerequisite: third-party code does not change.** v0 analyzes only code the repository owns
and contains. Third-party code, whether a git submodule or a library copied into the tree, must be
identical at both points. A comparison where it differs is refused, not analyzed. Its headers are
still parsed where the repository's code includes them, but its own translation units are not, and
none of it is in the model, exactly like the standard library. Analyzing third-party changes is
deferred, not rejected. Decided in [ADR-025](adr/ADR-025-third-party-code-must-not-change.md); not
yet implemented ([PLAN-004](plans/PLAN-004-third-party-boundary.md)).

**v0 was redefined on 2026-10-10** as a tool whose output can be trusted on changes the repository
controls ([ADR-030](adr/ADR-030-what-v0-is.md)). The slice above is what was built first; ADR-030
and [ADR-041](adr/ADR-041-v0-scope-of-steps-6-and-7.md) say what v0 still needs.

Two things listed here as "out" in the original draft shipped anyway, and the reason is the same in
both cases: they were cheaper *inside* the stage that needed them than as a later bolt-on.
**Cascade collapse** is in `render` (§6.3) — without it a deleted class reads as one change
event per member plus one per edge, which is most of the volume. **Blast radius** is in `rank`
(§7) rather than in `enrich`, because it is rank's first input and computing it from the head
model needs no external tool; §3's table still lists it under `enrich`, which is where the
*evidence* adapters will attach.

Tiers 2–4 are excluded on purpose: RESEARCH-002 §7 step 4 says ship tier-1 and *measure how badly
renames hurt* before building the machinery. That measurement is a v0 deliverable, and it is now
in — see the [v0 tracker](../reports/2026-08-01--v0-build/STATUS.md) §3. The answer was that on `fmt` and `spdlog` the upper
bound on change events tiers 2–4 could remove is 3, 5 and 1, and on `leveldb` 0. That is an argument
against building them next, which is what asking the question early was for.

On 2026-10-10 that answer was reopened for one case: a function whose parameters changed was
reported as removed plus added, and on current upstream spdlog that was every removal in a
30-commit range ([ADR-026](adr/ADR-026-changed-parameters-same-function.md)). The name tier (§5)
pairs it with its old version. It keys on the name, so it detects no rename. The inline
namespace tier (§5) pairs one rename, a bumped inline namespace's version tag, on evidence from
what is inside it. The measurement above still stands for every other rename.

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
  model/       IR types ✓ · JSON schema (not generated)
  acquire/     git revisions · worktrees · compile db · changed tokens  ✓
  produce/     toolchain ✓ · extraction ✓ · assembly ✓ · per-TU cache ✓
  match/       tiered matching (tier 1, name tier)      ✓
  diff/        model × model → change events · uncovered changes  ✓
  derive/      derived change events (v0: callers)      ✓
  classify/    cause and cascade rules                  ✓
  evidence/    tool adapters                (post-v0)
  rank/        blast radius, scoring                     ✓
  render/      json ✓ · terminal ✓ · html
  errors.py    the precondition taxonomy (§11)         ✓
  cli.py                                               ✓
```

`acquire` and `produce` are stages of the pipeline (§3) and named for them; the earlier draft called
the latter `producers` and grouped git with it, which put the two-point machinery inside a
single-point stage.

Every package is now named for its stage, which the earlier draft's `identity/` and `delta/` were
not. `delta/` in particular grouped two stages with two different contracts — diff may not set
`caused_by`, classify may not create a change event — and a shared package is the wrong place to keep a
rule that says what its neighbour is forbidden to do.

---

## 14 · Open questions

Resolved and moved into the body: the compile-database requirement at both revs and the
base-does-not-build case (§3.1); which libclang, and proving it is complete (§13, and it was not an
academic question — the bundled library was silently truncating every AST). Remaining:

- **Generated sources.** §3.1 states the problem but not the policy. Configure-only is the v0
  design; the threshold at which a repository needs a partial build to be analyzable at all is
  unmeasured, and `fmt`/`spdlog` will not reveal it.
- **Threshold tuning for tier 4.** Git's `-M` has one knob and decades of tuning. Assume this needs
  per-repository calibration and design the knob in early.
- **Where does the reviewer actually read this?** *Out of scope for now*
  ([ADR-030](adr/ADR-030-what-v0-is.md)). CLI is right for v0 and probably wrong as the
  product. The JSON-first contract defers the decision without prejudging it.
- **How much environment must identity capture before a cache is safe to share?** §12.1 lists the
  determinants; which of them actually move a model is unmeasured. The cheap experiment is to build
  the same fixture on two hosts and diff the partial models — if it is empty, the current narrow
  identity is adequate and the rest is theory. Until that is run, a cache should not be shared
  across machines, which contradicts §9's advice that CI pre-populate the base.
