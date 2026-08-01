<!-- metadata
id: RESEARCH-002
type: research-note
status: draft
author: Brent Runyan
created: 2026-08-01
dependencies: [RESEARCH-001]
origin: written during codeatlas-cpp research (RESEARCH-008 there); renumbered on move
-->

# Semantic Diff for Code Review

A code reviewer reading a unified diff is doing compiler work by hand. They parse changed lines,
reconstruct the surrounding context from memory, build a mental model of the before and after
states, and compare them — for every hunk, unaided, at human error rates. The line diff gives them
no help, because a line is not a unit of meaning.

This note asks what the reviewable units actually are, what artifacts express change in those
units, what prior art exists for each, and what this tool would have to build to produce them.
It is the founding document for the product.

Survey compiled from tool documentation and literature, not measured. The general tool inventory
it draws on is [`cpp-analysis-tool-landscape.md`](cpp-analysis-tool-landscape.md).

---

## 1 · The unit problem

Every useful review signal is a **two-point analysis**: build a model at commit A, build the same
model at commit B, diff the models. The text diff is the degenerate case where the model is "an
ordered list of lines" — the cheapest possible model, and the one furthest from what a reviewer
needs to know.

Three failure modes follow from that choice, and they are not symmetric:

**False positives.** Reformatting, renaming, and code motion produce enormous diffs with zero
semantic content. Reviewers learn to skim, and skimming is indiscriminate.

**False negatives.** A one-character change to a comparison operator, a deleted `assert`, an added
`explicit`, a changed default argument — all render identically to a whitespace tweak. Visual
prominence is proportional to line count, which is uncorrelated with risk.

**Structural blindness.** The most important consequences of a change are never *in* the change. A
new call edge, a newly introduced cycle, a function that became unreachable, a struct that grew
past a cache line — these are properties of the whole program at each commit, and the diff of the
two programs contains them nowhere.

The third is the one that motivates a graph model. The first two have partial off-the-shelf
answers; the third does not.

---

## 2 · The four criteria, and what they miss

The working criteria were: **correct**, **clean**, **non-breaking**, **no noise**. Those are real
and each maps to a genuine artifact — but they are neither a partition nor a covering. Three
problems:

**They collapse distinct questions.** "Correct" spans at least three separable claims with
different evidence: *this refactor changes nothing observable* (provable-ish), *this changes
behavior in exactly the intended way* (needs a stated intent to check against), and *this
introduces no new defect class* (needs a defect oracle). "Clean" spans structural degradation
(new cycles, new coupling) and local style, which share nothing but a word.

**They are all properties of the change.** None of them describe the *reviewer's* problem, which
is allocation of finite attention across a diff whose parts differ in risk by orders of magnitude.

**They omit whole axes.** Seven, at least:

| Missing axis | The question | Why a line diff can never answer it |
|---|---|---|
| **Completeness** | Was the change applied *everywhere* it needed to be? | A diff shows what changed and is structurally incapable of showing what should have changed but didn't. This is the single largest blind spot. |
| **Blast radius** | How much depends on what was touched? | Requires the whole-program dependency graph, not the change. |
| **Resource profile** | Did size, stack depth, allocation behavior, or build time regress? | Consequences are emergent and invisible in source. |
| **Ownership & lifetime** | Did the memory/ownership contract change? | The densest C++ defect class; expressed in types and idioms, not line counts. |
| **Evidence** | What new claims does this make, and what backs them? | A deleted assertion and an added blank line are both one line. |
| **Variant coverage** | Does it hold across every build configuration? | `#ifdef`, platform, standard level, exceptions/RTTI off. You read one variant; CI builds many. |
| **Intent conformance** | Does the change do what it says, and only that? | Requires a stated intent to diff against. |

Sections 3 and 4 treat the full set. The original four are marked ★.

---

## 3 · The reviewer's question set

Each subsection: the question, the artifact that answers it, the prior art, and how mature that
prior art is for C++ specifically.

### 3.1 ★ Did the contract change? *(non-breaking)*

**Artifact:** ABI/API surface delta — symbols added or removed, vtable layout, struct size and
member offsets, parameter and return types, enum values, exception specifications.

**Prior art:** **`abidiff`** (libabigail) is the mature answer and is under-adopted. It builds from
DWARF at both commits and classifies each change as compatible or incompatible. **`codiff`**
(dwarves) gives per-function and per-struct deltas. **`abipkgdiff`** works across released
packages. **`abi-compliance-checker`** with **`abi-dumper`** is the older alternative.

**Maturity: high.** This question is solved. The cost is that both sides must build, which is also
why it is rarely wired into review.

**Header-level alternative:** diffing declarations from a parsed model catches source-level API
breaks (a changed default argument, a new overload creating ambiguity) that ABI diff misses
because they never reach the symbol table. The two are complementary, not redundant.

### 3.2 ★ Is most of this noise? *(no noise)*

**Artifact:** a structural edit script instead of a line script — insert / delete / update / **move**
over tree nodes.

**Prior art:** **difftastic** (tree-sitter, drop-in as `GIT_EXTERNAL_DIFF`) and **diffsitter** work
today at zero setup cost. **srcDiff** (from the srcML group) produces syntactic edit scripts for
C++ specifically. **GumTree** (Falleri et al., 2014) is the reference algorithm — top-down
isomorphic subtree matching followed by bottom-up similarity — and crucially emits *move* as a
first-class operation, so relocating a function unchanged is one edit rather than two hundred
line changes. **`git diff --color-moved=zebra -M -C -w`** is free and catches motion and renames
without any tooling.

Also worth doing cheaply: mark generated and vendored files `linguist-generated=true` in
`.gitattributes` so they collapse by default.

**Maturity: high for syntax, low for semantics.** All of the above suppress *formatting* noise
well. None of them know that a changed line is inside a template that only instantiates on one
platform.

### 3.3 ★ Is it correct? *(split into three)*

**3.3a — Behavioral equivalence.** The claim "this refactor changes nothing observable." Evidence:
binary-level function matching (**BinDiff**, **Diaphora**, `radiff2`) showing which bodies actually
differ after inlining; identical `abidiff`; deterministic instruction counts from **callgrind**
matching across builds. Research-grade equivalence checkers (**SymDiff**, **RVT**, **Reve**)
target C, not C++. *Maturity: moderate, via binary evidence rather than proof.*

**3.3b — Intended behavioral change.** Requires an intent statement to check against, which is why
this stays human. What tooling can do is show *where* behavior can now differ: call graph delta,
reachability delta, new data-flow paths. From `-fdump-ipa-cgraph` (GCC), CodeQL, Understand, or a
graph IR. *Maturity: low — nothing packages this for review.*

**3.3c — New defect classes.** Diff-scoped static analysis: **CodeQL** in PR mode alerts only on
paths the diff introduced rather than the standing backlog; **Cppcheck** and **PVS-Studio** both
support diff-scoped runs; **Semgrep** for pattern-level rules. **Sanitizer** runs on changed tests.
*Maturity: high, and the diff-scoping is the entire reason these are usable in review.*

### 3.4 ★ Did the structure degrade? *(clean)*

**Artifact:** architecture and coupling delta — new include edges, new target dependencies, new
cycles, new layer violations, complexity delta on changed functions only.

**Prior art:** **CodeScene** delta analysis productizes exactly this (code-health delta on a PR,
hotspot warnings). **Axivion**, **Lattix**, and **Structure101** enforce architecture rules and
report violations introduced by a change. `cmake --graphviz` at both commits, diffed, gives target
edges. **cpp-dependencies** and **cppdep** give include cycles. **Lizard** or **Metrix++** scoped
to changed functions gives complexity delta without drowning in whole-repo metrics.

**Maturity: high commercially, fragmented in open source.**

### 3.5 Was the change complete?

**The biggest blind spot, and the one with the least tooling.** The classic review failure is
omission: five of seven call sites updated, an enum value added without the matching `switch` arm,
one copy of duplicated logic fixed and the other left. A diff cannot show this, by construction.

**Artifacts:**
- **Sibling analysis** — clone detection (**PMD-CPD**, **NiCad**, **Simian**) restricted to the
  changed regions, to surface near-duplicates of changed code that were *not* changed.
- **Exhaustiveness** — enum/`switch` coverage after an enum changes. Compiler warnings
  (`-Wswitch`) catch part of this and are frequently disabled.
- **Evolutionary coupling** — files or entities that historically change together but didn't this
  time. From version history mining (Zimmermann et al., *Mining Version Histories to Guide
  Software Changes*, 2004 — whose tool was confusingly also named ROSE, unrelated to the LLNL
  compiler framework). **CodeScene** ships change coupling.
- **Call-site completeness** — from the call graph: which callers of a changed signature were
  updated, which weren't. In C++ the compiler catches signature breaks, but not semantic contract
  changes where the signature is stable.

**Maturity: low.** Evolutionary coupling is the only mature piece and it is history-based, not
semantic. A graph model plus history is the natural way to do better.

### 3.6 What is the blast radius?

Not a diff at all — **context on** the diff, and arguably the highest-value thing a graph model
offers. The reviewer needs to spend attention proportional to what is at stake:

- Transitive reverse reachability of every changed entity — how many things can reach this.
- Fan-in delta and whether the entity sits on a critical path.
- Whether the change lands in a historical hotspot (**CodeScene**), a security-sensitive module,
  or code with a single owner who isn't the reviewer.
- Public-API proximity: is this internal, or is it two hops from a published header?

**Maturity: low as a review artifact,** though every input exists. Nothing presents "this 3-line
change has 400 transitive dependents; that 300-line change has 2."

### 3.7 Did the resource profile change?

A change can be correct, clean, non-breaking, and noise-free while tripling build time or blowing
an embedded stack budget.

- **Binary size** — **Bloaty `--diff`** attributes growth to symbols and compile units, including
  template instantiation cost.
- **Struct layout** — **`pahole`** before and after; a struct crossing a cache line is a silent
  performance change with no visible diff signal.
- **Stack depth** — `-fstack-usage`, **AbsInt StackAnalyzer** for worst-case bounds.
- **Build time** — `-ftime-trace` (clang), **vcperf** (MSVC), `-ftime-report` (GCC), delta'd. A new
  header include in a widely-included header is a real and invisible cost.
- **Instruction counts** — **callgrind** is deterministic, so it works as a CI regression gate in a
  way wall-clock profiling does not.

**Maturity: high per-tool, nonexistent as an integrated review artifact.**

### 3.8 Did the ownership and lifetime contract change?

The densest C++-specific defect class, and entirely invisible to line counts. Reviewable events:

- A `unique_ptr` became a raw pointer, or ownership moved between entities.
- A function began returning a reference or pointer to something with a shorter lifetime.
- A non-owning view (`string_view`, `span`) became a *member*, silently acquiring a lifetime
  requirement on its source.
- A type gained or lost copy/move semantics, or became implicitly convertible.
- `const` was removed from a method, or a `const&` parameter became by-value.

**Prior art:** lifetime static analysis exists (the C++ Core Guidelines lifetime profile,
**PVS-Studio**, **Coverity**), but *diffing* the ownership relation across commits does not exist as
a product. It is expressible as a typed edge delta in a graph model.

**Maturity: none as a diff.** Strong candidate for differentiation.

### 3.9 What evidence backs the change?

Not coverage percentage — coverage *of the changed lines*, plus what happened to the assertions.

- **`diff-cover`** over lcov/gcov: "you added 40 lines, 6 are tested" is a one-line review verdict.
- Test deltas as first-class events: a **deleted assertion, a disabled test, a weakened
  expectation, or a widened tolerance** is the most review-worthy thing that can appear in a PR and
  currently gets the visual prominence of one deleted line.
- New public entity without any test reaching it — a reachability query, not a coverage number.

**Maturity: moderate for coverage, none for assertion-delta.** The assertion-weakening detector is
cheap to build on a parsed model and would earn its keep immediately.

### 3.10 Does it hold across build variants?

C++-specific and badly served. The diff you read is one preprocessor variant; CI builds several,
and users build more.

- Which `#ifdef` branches does the change touch, and which configurations exercise them.
- Does the change compile under the other standard level, without exceptions, without RTTI, on
  MSVC, on 32-bit.
- Header changes that are variant-dependent are the worst case: correct-looking, broken elsewhere.

**Prior art:** variability-aware analysis exists in research (TypeChef, SuperC for C) and in the
Linux configuration-analysis world; almost nothing for C++ in review. Practically, the answer today
is "build the matrix and diff the failures."

**Maturity: low.**

### 3.11 Does the change match its stated intent?

Distinct from noise: noise is meaningless change, this is *meaningful change that doesn't belong* —
an unrelated fix, a drive-by refactor, a smuggled behavior change inside a "rename" PR.

**Artifact:** the set of subsystems touched, versus the set implied by the stated purpose. Cheaply
approximated by clustering the model delta into connected components and flagging components that
share no edges with the rest. A PR whose delta has three disconnected components is three PRs.

**Maturity: none.** Also the basis for automatic *decomposition* — see §4.

### 3.12 Is it reversible, and does it require coordination?

Non-breaking at the ABI level can still be breaking at the data level: serialization formats,
on-disk layouts, protocol versions, persisted enums. These need migration and coordinated rollout,
and reviewers routinely miss them because the source change is small.

**Artifact:** delta on types that cross a persistence or wire boundary. Requires those boundaries
to be marked — a schema annotation, or a heuristic (types reachable from a serialization function).

**Maturity: low; domain-specific.** Worth naming because the failure is expensive.

---

## 4 · The meta-axis: attention allocation

Every question above assumes the reviewer is already looking at the right thing. The higher-value
output is telling them what to look at.

- **Ordering.** Present the model delta ranked by risk (blast radius × novelty × hotspot), not by
  file path alphabetically — which is what every review tool does today.
- **Decomposition.** Partition the diff into independently reviewable units. "These 2,000 lines are
  one mechanical rename; these 40 lines are the actual change" is the single most useful sentence
  a review tool can produce.
- **Cascade suppression.** Rename a function and 200 call edges "change." That is *one* event, not
  201. Model deltas must be classified into causes and consequences, with consequences collapsed
  under their cause by default.
- **Confidence labelling.** When the model is degraded — no build available, a TU failed to parse,
  a template wasn't instantiated — every claim derived from it must say so. A review tool that
  silently degrades is worse than no tool, because it converts absence of evidence into apparent
  evidence of absence.

---

## 5 · What this tool has to build

The product thesis: a semantic review diff *is* a graph diff over a semantic model of the
codebase, projected to the reviewer's current scope. Nothing in the landscape does this for C++,
because everyone else would have to build the model first.

**Relationship to CodeAtlas C++.** That project (`dev-team/projects/codeatlas-cpp`) has already
solved parts of the modelling problem: a graph IR with a JSON Schema and validator, a clangd-backed
producer, a CMake file-API provider, and a scope-projecting renderer. **Its designs carry over; its
code does not.** CodeAtlas is TypeScript because it began as a VS Code extension — an accident of
delivery, not a modelling requirement. This tool is Python, and inherits ideas rather than modules:
the IR's shape, the relation ontology, the file-API approach to build metadata, the scope
projection concept.

The products also differ in kind, which limits how much would transfer even in one language.
CodeAtlas answers *what is this codebase* — one point in time. This tool answers *what did this
change do* — two points, which imposes requirements a single-snapshot design does not have: stable
identity across commits, model caching keyed by commit SHA, and a presentation built around events
rather than entities.

Five pieces are missing regardless of what gets reused.

### 5.1 Node identity across commits

The hard problem, and the one that decides whether the rest works. Without stable identity, a
renamed-but-unchanged entity reads as delete + add and the diff is worse than the text diff.

No single identity scheme survives all edits, so use a **three-tier match with fallback**, in the
spirit of GumTree and of `git`'s own `-M`/`-C` similarity detection:

1. **Declared identity** — fully qualified name plus signature (clang's USR is this shape).
   Survives body edits and moves between files. Broken by any rename or signature change.
2. **Structural identity** — hash of the entity's subtree, normalized for formatting. Survives
   renames and moves. Broken by any body edit.
3. **Positional identity** — parent entity plus ordinal. The fallback for things with no name at
   all: lambdas, anonymous namespaces, unnamed structs.

Match tier 1 first, then tier 2 over the unmatched remainder, then tier 3, then similarity-threshold
matching for the rest (an entity whose name changed *and* whose body changed is a rename plus edit
only if similarity clears a bar — same tradeoff git makes, same tunability problem).

C++-specific identity hazards to design for explicitly:
- **Templates** — the template and each instantiation need separate identity, and instantiation
  sets differ between commits for reasons unrelated to the change.
- **Overloads** — name alone is never identity; signature must be normalized (typedef resolution,
  default arguments, `const` qualification).
- **Internal linkage and anonymous namespaces** — identity must include the TU.
- **Macros** — an entity's identity may depend on a macro that itself changed.
- **ODR** — the same declared identity may appear in multiple TUs with different definitions.

### 5.2 IR diff semantics

Define the delta as a typed, closed set of events, not an ad-hoc struct diff:

- **Node:** added, removed, moved (parent changed), modified (which attributes), renamed.
- **Edge:** added, removed, retargeted, kind-changed (e.g. a `calls` edge that became `virtual-calls`).
- **Derived:** reachability delta, fan-in/fan-out delta, cycle introduced or removed, layer
  violation introduced, entity became unreachable.

Each event carries a **cause link** (which other event it follows from) so §4's cascade suppression
is a query rather than a heuristic pass, and a **provenance link** to the source hunks that produced
it. Provenance must be bidirectional: a reviewer has to be able to click from a model event to the
lines, and from a line to the events it caused. Without the reverse direction the tool is a
second, disconnected thing to read.

### 5.3 Two-point model acquisition

Building the full model twice per review is the cost that kills naive implementations. Options,
roughly in order of preference:

- **Cache models by commit SHA.** The base commit's model is usually already built by CI and is
  reusable across every PR targeting it.
- **Incremental** — re-analyze only changed TUs plus their dependents, using the existing model
  for the rest.
- **Degraded fallback** — a build-free fuzzy model when no compilation database exists, with §4
  confidence labelling making the degradation explicit and non-silent.

### 5.4 Projection and scoping

The whole-graph delta is unreadable. The delta must render at the altitude the reviewer selected —
the same zoom concept already in the product, applied to events rather than entities. Deltas below
the current scope aggregate into a badge on the containing node ("14 changes inside"), not a wall
of edges.

### 5.5 Evidence attachment

Each answer in §3 comes from a different tool. The IR is the join key: attach `abidiff` findings,
coverage, complexity delta, and size delta to the *nodes they concern*, so the reviewer sees
"this class: ABI-incompatible, +2KB, 0% coverage on new members" rather than four separate reports
to correlate by hand. This is also what makes the survey in
[`cpp-analysis-tool-landscape.md`](cpp-analysis-tool-landscape.md) actionable rather than
inventory — those tools become evidence producers feeding one model.

---

## 6 · Hard problems, named up front

- **Identity is the whole game.** If §5.1 is wrong, everything downstream is noise with extra steps.
- **Both commits must produce a model.** Every high-value answer is two-point. Any design that
  can't cheaply obtain the base model doesn't ship.
- **Templates degrade everything.** Identity, call graphs, instantiation-dependent behavior, and
  size attribution all get harder, and template-heavy code is exactly the code humans review worst.
- **Preprocessor variance.** One model is one configuration. Claims derived from it are conditional
  and must be labelled as such.
- **Static call graphs are unsound** (virtual dispatch, function pointers, inlining). A reachability
  delta is a lower bound, not a fact, and must be presented as one.
- **Cascade classification is a judgment call.** Which event is cause and which is consequence is
  sometimes ambiguous; getting it wrong hides the real change under a collapsed group. Default to
  showing, not hiding, when confidence is low.

---

## 7 · A staged plan

Ordered by evidence-per-unit-of-effort, not by ambition.

1. **Cheap wins requiring no model.** `difftastic` as external diff; `--color-moved=zebra -M -C`;
   `linguist-generated` on generated files. Zero build cost, immediate noise reduction. Establishes
   the baseline everything else must beat.
2. **`abidiff` in CI.** Answers §3.1 outright, mature, needs only two builds. The highest-value
   single integration available today.
3. **Assertion-delta detector.** Flag deleted/disabled/weakened tests as first-class review events
   (§3.9). Cheap on any parsed model, disproportionately valuable.
4. **IR diff v0** — node and edge add/remove/modify with tier-1 identity only, rendered at scope.
   Deliberately no rename detection yet; measure how badly it hurts before building tiers 2 and 3.
5. **Blast radius annotation** (§3.6). No diff required — reverse reachability on the head model,
   attached to changed nodes. Likely the first thing reviewers actually feel.
6. **Cascade suppression and decomposition** (§4). The step that turns a delta list into a review
   plan.
7. **Evidence attachment** (§5.5) — coverage, size, complexity joined onto the model.
8. **Ownership/lifetime edge diff** (§3.8). Highest differentiation, highest cost, and dependent on
   the model expressing those relations in the first place — the CodeAtlas C++ relation ontology
   (`docs/research/cpp-ontology/cpp-ontology-relations.md`, RESEARCH-ONT-003 in `dev-team`) is the
   existing prior work to draw the vocabulary from.

Steps 1–3 are useful independently of this tool and worth wiring into any C++ project today; they
also establish the baseline the product must measurably beat. Steps 4–8 require a semantic model,
and are the actual product.
