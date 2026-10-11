<!-- metadata
id: PLAN-006
type: plan
kind: living
status: in progress
author: Brent Runyan
created: 2026-10-10
dependencies: [ARCH-001]
-->

# Make the model see ordinary changes, and say when it cannot

*Living document: kept current until it is done, then deleted.*

**Reviewed 2026-10-10.** The steps were suggested alongside the findings in
[`the-model.md`](../../reports/2026-10-10--up-to-speed/the-model.md), which is the evidence for
everything below. Steps 1, 2, 4 and 5, and step 7 except field order and layout,
are in v0 ([ADR-030](../adr/ADR-030-what-v0-is.md), [ADR-041](../adr/ADR-041-v0-scope-of-steps-6-and-7.md)). Step 1 is built:
`tests/test_change_catalog.py`. Step 2 is built except excluding third-party code and counting
unmodelled declarations ([ADR-042](../adr/ADR-042-uncovered-changes-are-tokens-on-no-event-line.md)). The v0 steps are tracked in [#5](https://github.com/j-brent/cpp-semantic-diff/issues/5); steps 3, 6, 8 and 9 in
[#12](https://github.com/j-brent/cpp-semantic-diff/issues/12), [#10](https://github.com/j-brent/cpp-semantic-diff/issues/10), [#13](https://github.com/j-brent/cpp-semantic-diff/issues/13) and [#14](https://github.com/j-brent/cpp-semantic-diff/issues/14).

Delete this file when the steps that survive review are done and every case in the change
catalog (step 1) has its expected outcome without being marked as an expected failure.

---

## 1 · The problem

Of 22 small, ordinary changes, 16 produce no change event, and the tool reports "0 change
events" instead of saying it could not see them (the-model.md §4). Changes to things identity
depends on, such as a type alias or an inline namespace, arrive as unrelated removals and
additions. Data types and virtual dispatch are missing from the blast radius.

---

## 2 · Steps

Step 1 comes first because it defines when every other step is done. Then in order of value for
cost. Steps 2 and 3 need no new entity kinds. Dependencies: step 6 needs
step 5, and every new compared fact in steps 5 and 7 needs its cascade rule (step 7).

### Step 1 · The change catalog

A test of the model as a reviewer experiences it: given this before and this after, does a change
event come out? `tests/test_extract.py` checks that constructs parse; nothing checks this.

- **One test file, one case per common change.** Each case is a before snippet, an after snippet
  and the expected outcome, run through the real extractor, matcher and differ.
- **The expected outcome** is a specific change event or, for something deliberately left out of
  the model, "reported as not visible". So the catalog can pass before everything is modelled.
- **The cases:** the 22 probes in the-model.md §4, and the two identity problems in
  step 5 (a macro's identifier moving when a line is added above it, and a `static_assert` with no
  identifier). The probe scripts are in
  [`model-probes/`](../../reports/2026-10-10--up-to-speed/model-probes/).
- **A case the tool fails today is a strict expected failure.** It passes today. When a later step
  fixes it, the test fails, and someone has to write in the expected outcome. So the catalog
  measures progress and defines when each step is done.

### Step 2 · Report what the model cannot see

Today a blind spot reads as "nothing changed". Make it a number on every run, the way
`edges_dropped` already is.

- **Uncovered changes.** List the changes in the repository's own C++ files, headers included,
  that no change event points to. Every change event already carries its locations, so this is a
  set difference against the diff. It turns all 16 silent cases into "changes the model cannot
  explain". It is also the reverse direction of provenance that RESEARCH-002 §5.2 calls
  essential.
  - **Compared as tokens, not lines**, so a whitespace or comment edit is not reported as
    uncovered.
  - **Third-party code is excluded**, as PLAN-004 defines it.
  - **Headers count even when no translation unit includes them.** This is how the-model.md §4.6's
    limit, a header nothing includes, becomes visible.
- **Unmodelled declarations.** Count the declarations in repository files that the extractor
  skipped, by kind: for example 40 enums, 12 aliases, 300 variables.

This also repairs the success metric. "580 lines changed → 181 change events" improves when the
model sees *less*. Reported beside uncovered lines, it cannot.

### Step 3 · Check that every token of an entity is accounted for

A test of fidelity that needs no independent oracle. For each modelled declaration, every token
in its source extent must be covered by exactly one of: identity, a compared attribute, the body
fingerprint, a child entity, or a list of deliberately ignored tokens, such as parameter names.
Any uncovered token is a change the model cannot see.

This would have found the default argument, the initializer list, the member initializer and the
attributes mechanically, and it keeps finding gaps as C++ grows.

### Step 4 · Make identity smaller, and treat the rest as state

Identity today includes things that are better compared than keyed on: parameter types, template
parameters, inline namespace names and, through canonical types, every alias a signature uses.
Each of these turns a change into a removal plus an addition with no cause.

Suggested direction:

- Identity is the kind and the qualified name, with inline namespaces dropped.
- Parameter types and template parameters are compared state.
- Overloads are told apart only when the name alone is ambiguous. An ambiguous pairing is left
  unpaired and reported, under the project's rule against forced matches.

For parameter types and template parameters this was done without changing identity: the USR
stays the node id, and a name tier pairs the functions and methods tier 1 leaves
([ADR-036](../adr/ADR-036-functions-pair-by-name.md)). Inline namespaces were done the same
way: a tier pairs what a bumped inline namespace renamed
([ADR-044](../adr/ADR-044-inline-namespace-bump-pairs.md)). Aliases remain. Unnamed
records and same-named statics stay as they are in v0
([ADR-045](../adr/ADR-045-same-named-statics-and-unnamed-records-stay-out-of-v0.md)).

### Step 5 · Add the entities that carry values

Each with the one thing a reviewer compares. ARCH-001 §4.1 already includes enum, enumerator,
alias and variable; macro and static assertion are new to the design. Each compared fact needs a
novelty weight in `rank/weights.py`.

| Entity | Compared |
|---|---|
| enumerator | value, order |
| variable, including static data members | type, constant value, storage, linkage, thread-local |
| alias | canonical target type |
| macro | hash of its replacement tokens |
| static assertion | hash of its condition; adding or removing one is a change event |

**Each new kind needs an identity scheme first.** Exact-USR pairing does not work for two of
them:

- **A macro's USR contains its byte offset in the file** (`c:a.cpp@8@macro@LIMIT` becomes `@23@`
  when a line is added above it). Under exact pairing, every macro below an edit would be reported
  as removed and re-added.
- **A `static_assert` has no USR at all,** and the extractor skips anything without one.

### Step 6 · Add one relationship: uses

From a function, field initializer or variable initializer to each entity it names: a variable,
enumerator, type, macro, or a function whose address it takes. This makes step 5 useful:

- **Causes.** "`kLimit` changed from 10 to 20" gets its users filed under it, as `contains` does
  for removals today.
- **Blast radius.** Data types and constants get real dependents.
- **Completeness.** An enumerator added, with each `switch` over that enum that does not name it:
  RESEARCH-002 §3.5's example, answerable from the model.

ARCH-001 already declares `uses-in-interface` and `uses-in-implementation`, and the blast radius
already counts them. Nothing emits them.

### Step 7 · Compare more on the entities that exist

| Entity | Add |
|---|---|
| function, method | default arguments; `constexpr`, `consteval`, `inline`; `[[nodiscard]]`; deprecated |
| constructor | initializer list, in the body fingerprint |
| field | default member initializer; offset |
| class, struct, union | size, alignment; `final`; deprecated |
| inherits | base order; virtual |

Each needs a novelty weight in `rank/weights.py`. Order can be an ordinal attribute, compared
without making location part of identity.

**These facts cascade.** Insert one field and every later field's position and offset change, and
so does the size of every struct that contains it. Without a cause rule in `classify/rules.py` for
each cascade, one edit becomes dozens of top-level change events. Steps 3 and 7 overlap: step 3's
token check will find the same gaps this table lists.

### Step 8 · Recover the calls the graph misses

- **Virtual dispatch:** in the blast radius, let callers of an overridden method reach its
  overrides.
- **Calls in templates** that depend on a template parameter: record them by name as
  unresolved, and count them in the completeness record. On template-heavy code this is part of
  every call graph.
- **Calls from initializers and default arguments:** attribute them to the variable, field or
  function that owns the initializer.

### Step 9 · Show sufficiency instead of assuming it

- **A trace table.** For each of RESEARCH-002's twelve reviewer questions, the entities,
  relationships and fields that answer it. The table in the-model.md §5 is a first version. It
  becomes part of ARCH-001, and the model is checked against it.
- **Coverage on real history.** The share of changed lines that some change event explains,
  measured over fmt, spdlog and leveldb. That number is the honest answer to "is the model
  sufficient?"

### Suggested first moves

Step 1, the catalog, then step 2: it is small, needs no new model content, and stops the tool from
claiming nothing changed when the model only failed to look.

---

## 3 · What libclang cannot see

**Decided 2026-10-10: accept the limit and report it** ([ADR-029](../adr/ADR-029-libclang-limits-are-reported.md)). libclang does not expose implicit
special members: the copy and move operations the compiler writes. For example, adding a
destructor to a class silently removes its implicit move operations, so every move of that class
becomes a copy. No step above can see that.

- Every run states the limit, the way it states `edges_dropped`.
- Catalog cases that depend on it expect "reported as not visible".

**If it is decided later that this must be closed:** read the full Clang AST as well, for example
through clang's JSON AST dump (`clang -Xclang -ast-dump=json`), which includes these traits.
That is a second way of reading the code. It needs its cost measured first, its own correctness
checks, and a rule for what happens when the two readers disagree.

---

## 4 · Overlap with other roadmap work

| Step | Overlaps |
|---|---|
| 4 | **[#8](https://github.com/j-brent/cpp-semantic-diff/issues/8)** (a changed signature is the same function) is the parameter-type half of this step. The inline-namespace case is not in #8, and it is the larger one on fmt. Also the carried-over item on two same-named `static`s merging into one node, which is an identity question. |
| 5 | **[#9](https://github.com/j-brent/cpp-semantic-diff/issues/9)** (enums, globals and aliases are not in the model) is the first three rows. |
| 6 | **[#16](https://github.com/j-brent/cpp-semantic-diff/issues/16)** (callers affected by a signature change) needs the same caller-to-cause links. |
| 7 | **[#15](https://github.com/j-brent/cpp-semantic-diff/issues/15)** (change events specialized by entity kind) decides how these attributes are reported. |
| 8 | The carried-over `virtual` cause rule, which is also about calls reaching an overridden method. |
