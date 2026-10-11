<!-- metadata
id: ADR-038
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-038 · The entities that carry values are in the model, identified by their USR

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

ARCH-001 §4.1 lists enum, enumerator, alias and variable as node kinds, and the v0 extractor
produced none of them, so a changed enumerator value, global constant or type alias produced no
change event (PLAN-006 step 5, issue
[#9](https://github.com/j-brent/cpp-semantic-diff/issues/9)). Each needs an identity, the facts a
reviewer compares, and a novelty weight for each fact.

## Decision

**Identity is the USR**, as for every other kind. Tier 1 pairs these entities; the name tier
([ADR-036](ADR-036-functions-pair-by-name.md)) does not, because their USR holds nothing beyond
kind, name and scope.

**Enum** (`enum`): `underlying_type`, canonical; `scoped`, whether it is an `enum class`.

**Enumerator** (`enumerator`): `value`, the integer it stands for; `explicit_value`, whether the
value is written or implied by the one before. Contained by its enum.

- **No `order` attribute.** For an implied value, the value already carries the order: move an
  enumerator and its value changes. Where every value is written, order changes nothing a program
  can observe. Order as such is the field-order cascade
  ([ADR-041](ADR-041-v0-scope-of-steps-6-and-7.md), issue
  [#34](https://github.com/j-brent/cpp-semantic-diff/issues/34)).
- **Inside a template, `value` is None.** Clang computes no enumerator of an enum declared in a
  template: every one reads 0, even one written `= 4`.
- **One cause rule, `enumerator-implicit-value-shifted`.** An enumerator whose value is implied
  on both sides and changed is caused by the nearest change above it: an enumerator inserted,
  removed, or whose value changed. Changes farther up are linked by the enumerators between, one
  step per fact. A value written on either side is an edit of its own and is not explained.
  "Above" is source order, read from locations. Location orders siblings and nothing else; it is
  never identity (ARCH-001 §4.1).

**Alias** (`alias`): `typedef`, `using` and alias templates. `target`, the canonical type it
stands for; `template`.

- **A typedef that gives a tag its own name is not an alias node.** `typedef struct X {...} X;`
  and `typedef struct {...} X;` declare one type under one name, and the struct is already the
  node called `X`.
- **A function carries its signature as written,** `sugared_signature`, beside the canonical one,
  without its exception specification for the reason the canonical one has none.
  `NodeSignatureChanged` covers it: an event for it alone means the spelling changed and the type
  did not.
- **One cause rule, `alias-target-changes-signature`.** A function's `NodeSignatureChanged` is
  caused by an alias's target change when its written signature names the alias and did not
  change.

**Variable** (`variable`): at namespace scope, and static data members. A function's local
variables, `static` or not, are part of its body.

- `type`, canonical, the key a field uses.
- `value`: what clang's constant evaluator (`clang_Cursor_Evaluate`) folds the initializer to,
  an integer, a floating-point number or a string, so `2 * 5` and `10` are no change. An infinite
  or NaN value is spelled as a string: NaN is unequal to itself, and JSON has no infinity.
- `initializer_hash`: a hash of the initializer's tokens, only where clang cannot evaluate it --
  a call, a constructor, an address. A macro in it is hashed by name, not by what it expands to.
- Inside a template neither is evaluated, since the initializer may depend on a template
  argument and clang's evaluator is not safe to ask; the initializer is hashed.
- `constexpr`, read from the tokens before the name; `storage` (`none`, `static`, `extern`);
  `linkage`; `thread_local`.
- **The evaluator binding lives in `produce/extract.py`.** The producer's cache key hashes that
  module and `model/ir.py` only, so code that computes a value anywhere else could change while
  every key stayed the same.
- **A definition outranks a declaration** when one entity is seen several ways. A variable has no
  body to show it, so `extern` marks the declaration: `extern int x;` in a header and `int x = 5;`
  in a source file give the definition's value, `storage` and location.

**Weights** (`rank/weights.py`): `underlying_type` 0.95, `scoped` 0.90, `value` 0.95,
`explicit_value` 0.30, `target` 0.95, `sugared_signature` 0.30, `linkage` 0.85, `constexpr`
0.80, `thread_local` 0.80, `storage` 0.75, `initializer_hash` 0.50. A variable's `type` and
`value` share the weights of a field's type (1.00) and an enumerator's value.

## Consequences

- `enumerator-value` and `enumerator-added` in the change catalog pass.
- Inserting an enumerator reports one addition, with the value changes it caused under it.
- An unnamed enum's USR is named after its first enumerator (`c:@Ea@Z`), so renaming or inserting
  a first enumerator reports the whole enum as removed and added. That is tier 3's case (ARCH-001
  §5), which v0 does not build.
- `alias-target-changed` in the change catalog passes, and the function's signature change is
  filed under the alias.
- Clang writes the file name into a `typedef`'s USR (`c:a.h@T@Count`), though not into a
  `using` declaration's. Moving a typedef to another file reports it removed and added, and two
  typedefs of one name in two files of one name merge, as statics do
  ([ADR-022](ADR-022-same-basename-statics-merge.md)). A `using` alias has no linkage either,
  and its USR names no file, so two source files that each declare their own alias of one
  qualified name give one node. The merge is silent: the observation that ranks highest is
  kept whatever the TU order, and nothing reports that the two disagreed.
- `constexpr-global-value` and `static-data-member-value` in the change catalog pass.
  `global-initializer-calls-another` now reports the initializer's hash changing; it stays an
  expected failure because it asks for the call, which is step 8, out of v0
  ([ADR-030](ADR-030-what-v0-is.md)).
- A variable template is not modelled: libclang exposes it only as an unexposed declaration.
- A static data member's `storage` reads `static` where only its declaration is seen and `none`
  where its out-of-line definition is, so it changes when the definition stops being parsed.
- A changed value is reported without the code that uses it, and an added enumerator is not
  checked against the `switch` statements over its enum: both need the uses relationship, out of
  v0 ([ADR-041](ADR-041-v0-scope-of-steps-6-and-7.md)). ARCH-001 §6.3 records both rules for when
  it lands.

## Alternatives rejected

- **Always hash a variable's initializer.** `2 * 5` and `10` would differ, and a hash says that
  something changed where the value says what.
- **Compare an alias's target as written.** `using T = U` would then hide a change to `U`.

- **Identify an enumerator by its enum and position.** An insertion would then read as every
  later enumerator renamed.
- **Three cause rules, one per kind of cause.** The first rule to find a cause wins, so a farther
  insertion would win over a nearer value change. One rule tries every kind at each enumerator,
  nearest first (`CauseRule.also_caused_by`).
- **Link every shifted value straight to the insertion.** Correct for one insertion, and wrong
  once two changes stand above: each value is the one before plus one, so the nearest change is
  what it follows.
