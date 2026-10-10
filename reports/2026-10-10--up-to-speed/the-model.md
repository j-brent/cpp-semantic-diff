<!-- metadata
type: report
kind: frozen
status: draft, frozen once reviewed
author: Brent Runyan
created: 2026-10-10
-->

# The model: is it sufficient?

*Frozen document once reviewed: part of the 2026-10-10 review. The work it suggests is planned in
[PLAN-006](../../docs/plans/PLAN-006-model-sufficiency.md).*

**Draft, not yet reviewed.** It combines two documents written on 2026-10-10: the first asked
where the model was defined and whether it is sufficient; the second answered its closing
question, what C++ itself defines, and tested the model against ordinary changes. Their
suggestions moved to PLAN-006.

The design documents and code were read at commit `4d8e367`, the head of `dev` and `main` on
2026-10-10. The probes in §4 ran the real extractor, matcher and differ at `b934fc1`, against
system libclang 18. The sources are listed at the end.

---

## 1 · Short answer

The model was specified in two design documents and then cut down deliberately for v0. Nobody
has shown that it is sufficient, and the probes show that it is not.

Of 22 small, ordinary changes a reviewer would want to see (§4), **16 produce no change event
at all**. The tool reports "0 change events" for a commit that changed an enum value, a constant, a
default argument or the order of fields in a struct. **The tool does not report this blind
spot.** That is the failure the project's founding principle forbids: absence of evidence
presented as evidence of absence.

Most of the missing facts are cheap to get. libclang 18 already exposes nearly all of them,
including struct size and field offsets.

---

## 2 · Where the model was defined

In four layers, each narrower than the one before.

1. **RESEARCH-002, the founding research note.**
   - §3 lists **twelve questions a reviewer is trying to answer**: did the API contract change,
     is it noise, what is the blast radius, was the change complete, and so on.
   - §5 claims they can be answered as "a graph diff over a semantic model of the codebase".
   - It says what a model needs in general terms only. It lists no entity or relationship
     types.
2. **ARCH-001 §4, the full design.**
   - It defines **14 entity kinds and 14 relationship kinds** (§4.1, §4.2). Beyond the ones v0
     extracts, the entities include enum, enumerator, alias, variable, template, file and build
     target. The relationships include instantiates, has-a, holds-a, includes and needs-symbol.
   - The relationship kinds come from an earlier project's list of 61 C++ relations, the
     [CodeAtlas C++](https://github.com/j-brent/codeatlas-cpp) ontology. The only reason given
     for the choice is "a subset chosen for diff-relevance".
   - Its one firm rule is that every field is identity, comparable state, or source location.
3. **ARCH-001 §13, the v0 cut.** Namespace, class, function, method and field, plus contains,
   calls, inherits and overrides. The stated reason is "the smallest slice that tests the thesis
   end to end": chosen to test whether the idea works at all, not to be complete.
4. **The code.** `model/ir.py` declares the full 14 + 14 vocabulary (`NodeKind`, `EdgeKind`).
   `produce/extract.py` (`MODELLED_KINDS`) decides what is actually extracted: the v0 set plus
   **struct** and **union**. So the step 1 diagram in
   [`project-review.md`](project-review.md) is slightly wrong: it lists six entity kinds and
   leaves out union.

---

## 3 · What C++ defines

Three sources say what a C++ program is made of. Each answers a different question.

### 3.1 · The C++ standard: what can be named

The standard's list of **entities** ([basic.pre]/3): value, object, reference, structured
binding, function, enumerator, type, class member, bit-field, template, template
specialization, namespace, pack. A **declaration** introduces a name for an entity.

Its relationships are mostly implicit in the wording: a scope contains declarations, a class
has bases and members, a function overrides another, a name refers to an entity, a template is
specialized.

### 3.2 · Clang's AST: what the parser builds

Clang builds three trees: declarations (about 80 kinds), statements and expressions, and types.
The preprocessor adds macros and includes, which the standard treats as text, not entities. The
relationships clang computes include the semantic parent, the redeclaration chain, bases (with
`virtual` and access), overridden methods, the declaration a name refers to, the template a
specialization came from, and friends.

libclang, the C interface the tool uses, exposes most of this. It does not expose implicit
special members (the copy and move operations the compiler writes).

### 3.3 · The abstract machine: what a program does

The standard defines a program's meaning by an abstract machine that runs it. What counts is
**observable behavior**: volatile accesses, file output, and interactive I/O. Objects have a
storage duration (static, thread, automatic, dynamic), a lifetime and a layout.

This vocabulary has no entities to extract. It answers a different question: **where can
behavior come from?** Not only from function bodies. Also from initializers, default arguments,
constants, enum values, macros, the layout of objects, and which function a call dispatches to
at run time.

### 3.4 · How v0 covers them

| Standard entity | Clang declaration | In the v0 model |
|---|---|---|
| namespace | `NamespaceDecl` | yes |
| type: class, struct, union | `CXXRecordDecl` | yes |
| type: enumeration | `EnumDecl` | **no** |
| type: alias | `TypedefDecl`, `TypeAliasDecl` | **no** |
| function | `FunctionDecl` and its method kinds | yes |
| class member: non-static data | `FieldDecl` | yes |
| class member: static data | `VarDecl` in a class | **no** |
| class member: nested enum or alias | as above | **no** |
| enumerator | `EnumConstantDecl` | **no** |
| object or reference with a name | `VarDecl` | **no** |
| template | `ClassTemplateDecl`, `FunctionTemplateDecl` | as an attribute of a class or function |
| template specialization | explicit ones are declarations | explicit ones only |
| template: variable, alias, concept | `VarTemplateDecl`, `TypeAliasTemplateDecl`, `ConceptDecl` | **no** |
| (not an entity) macro | `MacroDefinition` | **no** |
| (not an entity) static assertion | `StaticAssertDecl` | **no** |
| (not an entity) friend, using-declaration | `FriendDecl`, `UsingDecl` | **no** |

Of clang's relationships, v0 extracts four: contains, calls, inherits and overrides.

---

## 4 · What the model sees

Each probe is one small before and after, run through the real extractor, matcher and differ.
There are 22: the 16 in §4.1, the 4 in §4.2, and the template call and callback in §4.3. The
scripts are in [`model-probes/`](model-probes/), and the cases are now a test suite,
`tests/test_change_catalog.py`.

### 4.1 · Changes that produce no change event

| Change | Example | Change events |
|---|---|---|
| default argument | `f(int x = 1)` → `= 2` | 0 |
| constructor initializer list | `S() : x(1)` → `x(2)` | 0 |
| default member initializer | `int x = 1;` → `= 2` | 0 |
| macro constant | `#define LIMIT 10` → `20` | 0 |
| `constexpr` global constant | `kLimit = 10` → `20` | 0 |
| enumerator value | `B = 2` → `B = 3` | 0 |
| enumerator added | `{A, B}` → `{A, B, C}` | 0 |
| static data member | `static constexpr int n = 4` → `8` | 0 |
| field order | `char a; double b;` → `double b; char a;` | 0 |
| base class order | `S : A, B` → `S : B, A` | 0 |
| base becomes virtual | `S : A` → `S : virtual A` | 0 |
| `constexpr` removed from a function | `constexpr int f()` → `int f()` | 0 |
| class becomes `final` | `struct S` → `struct S final` | 0 |
| `[[nodiscard]]` added | | 0 |
| `[[deprecated]]` added | | 0 |
| `static_assert` removed | | 0 |

Field order alone changes layout, ABI, and the order members are initialized in. Removing a
`static_assert` deletes a checked invariant, which RESEARCH-002 §3.9 calls "the most
review-worthy thing that can appear in a PR".

### 4.2 · Changes that arrive without their cause

| Change | What the tool reports |
|---|---|
| type alias changed: `using T = int` → `long` | `f(T)` removed, `f(T)` added, its call removed and added. Nothing says `T` changed. |
| inline namespace bumped: `v1` → `v2` | every entity inside removed and re-added. fmt uses `fmt::v12`, so a version bump rewrites the whole library. |
| destructor added | `~S` added. Nothing says the class lost its implicit move operations, so every move is now a copy (§4.5: libclang cannot see them). |
| parameter added | [roadmap issue 1](../../ROADMAP.md#issue-1--a-function-whose-parameters-changed-is-reported-as-deleted-and-re-added) |

In each, the cause is something identity depends on but the model does not contain, so the
consequences arrive as unrelated removals and additions.

### 4.3 · Relationships the model misses

| Code | Expected relationship | Extracted |
|---|---|---|
| `template<class U> void f(U u) { h(u); }` | `f` calls `h` | nothing. Calls that depend on a template parameter are dropped. |
| `reg(cb);` | `s` uses `cb` as a callback | only `s` calls `reg` |
| `int x = g();` at namespace scope | `x` depends on `g` | nothing; `x` is not modelled |
| `struct S { int y = g(); };` | `S` depends on `g` | nothing |
| `void f(int z = g());` | every caller of `f` calls `g` | nothing |
| any code that reads a field or names a type | uses | nothing; `uses` is declared but not extracted |

These feed the blast radius. Two consequences:

- **A data type has almost no blast radius.** Its dependents are only derived classes and its
  enclosing scope. A struct used across the whole codebase ranks like one used nowhere.
- **Virtual dispatch points the wrong way.** A caller of `Base::draw` runs `Widget::draw` at run
  time, but the walk goes from `Widget::draw` to `Base::draw` and not back. A changed override
  gets no credit for the callers that reach it through the base.

### 4.4 · Why: what each part of the model compares

An entity is compared on its identity, its signature, its attributes and its body fingerprint.
The gaps above fall between them:

- **The body fingerprint hashes only the `{ ... }` block.** A constructor's initializer list and
  a parameter's default value are outside it.
- **It hashes tokens as written, before macro expansion.** `return LIMIT;` hashes the same
  whatever `LIMIT` expands to.
- **The signature is the canonical type, which has no default arguments.** ARCH-001 §4.1 says
  "defaults included". The code does not do that.
- **Location is never compared, by design.** So order, which is only visible in location, is
  never compared either.
- **Attributes are a fixed list per kind**, and the list omits `constexpr`, `final`, `inline`,
  `[[nodiscard]]`, deprecation, linkage, storage, and everything about layout.

### 4.5 · What libclang 18 offers for these

Checked by parsing one file through the tool's own toolchain.

| Fact | libclang |
|---|---|
| enumerator value | `enum_value` |
| constant value of a variable | `clang_Cursor_Evaluate` (gave `20` for `kLimit`, `8` for `S::n`) |
| alias target | `underlying_typedef_type` |
| storage, thread-local, linkage | `storage_class`, `tls_kind`, `linkage` |
| struct size and alignment | `type.get_size()`, `get_align()` (gave 32 and 8) |
| field offset | `get_field_offsetof()` |
| virtual base | `clang_isVirtualBase` |
| `final`, `[[nodiscard]]` | attribute child cursors |
| deprecated | `availability` |
| default argument, initializer | child expression cursors |
| macros and where they expand | with the detailed preprocessing record option |
| friend, `static_assert` | cursors |
| implicit copy and move operations | **not available** |

Layout does not need a compiled build. Clang computes it during parsing for the target triple,
which is already part of the toolchain identity. It is unavailable only for templates that are
not instantiated.

### 4.6 · Two more limits

- **A header no translation unit includes is invisible.** The model is built from the
  translation units in the compile database. A new header that nothing includes yet produces no
  entities and no change events.
- **One model is one configuration.** Known, and stated in ARCH-001 §11.

---

## 5 · The reviewer's twelve questions

Nothing in the design traces each of RESEARCH-002's twelve questions to the model fields that
would answer it. Read against v0:

| Reviewer question | Can v0 answer it? |
|---|---|
| Did the contract change? | Partly. It sees signatures, return types, virtual/const/access, and field types. It does not see enum values, global variables, type aliases, default arguments or layout (§4.1). |
| Is most of this noise? | Yes. Body fingerprints ignore formatting and comments. |
| Is it correct? | Only *where* behavior could differ, through changes to calls. |
| Did the structure degrade? | No. Includes and build-target dependencies are not modelled. |
| Was the change complete? | Partly. It can show which callers of a changed function changed. It cannot check whether a `switch` covers a new enum value, because there are no enums. |
| What is the blast radius? | A lower bound from calls and containment, which misses data types and virtual dispatch (§4.3). |
| Did the resource profile change? | No. That needs a compiled build. |
| Did ownership or lifetime change? | Mostly no. The has-a and holds-a relationships are not extracted. Only field types are visible. |
| What evidence backs it? | No. The evidence stage is not built. |
| Does it hold across build variants? | No. A model covers one build configuration. |
| Does it match the stated intent? | Partly, through clusters of connected changes. |
| Is it reversible? | No. |

**Does the model faithfully capture what is in the code?** The project checks part of this: the
parser setup is verified so it cannot silently drop declarations (`produce/toolchain.py`), parse
failures are reported per file, and `tests/test_extract.py` covers specific constructs. Nothing
checks the extracted model against an independent source, and §4 shows what that misses.

---

## Sources

Read at commit
[`4d8e367`](https://github.com/j-brent/cpp-semantic-diff/commit/4d8e3671cb464690ce3059773d5b4390001d550b),
with the commit that last changed each file. Paths are as they were then.

| Source | At `4d8e367` | Last changed in |
|---|---|---|
| RESEARCH-002, founding research note | [`docs/research/semantic-diff-for-review.md`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/research/semantic-diff-for-review.md) | `e4cafac`, 2026-08-01 |
| ARCH-001, architecture | [`docs/architecture/overview.md`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/architecture/overview.md) | `08db50e`, 2026-09-20 |
| Model types | [`src/cpp_semantic_diff/model/ir.py`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/model/ir.py) | `07ed88b`, 2026-08-04 |
| Extractor | [`src/cpp_semantic_diff/produce/extract.py`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/produce/extract.py) | `826209c`, 2026-09-20 |
| Extractor tests | [`tests/test_extract.py`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/tests/test_extract.py) | `826209c`, 2026-09-20 |
| Parser setup check | [`src/cpp_semantic_diff/produce/toolchain.py`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/produce/toolchain.py) | `dc8738e`, 2026-09-20 |
