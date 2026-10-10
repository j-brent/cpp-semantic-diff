# cpp-semantic-diff: project review

Working notes from a review of the project's current state, started 2026-10-10. Statements
about what the tool does today refer to commit
[`4d8e367`](https://github.com/j-brent/cpp-semantic-diff/commit/4d8e3671cb464690ce3059773d5b4390001d550b).

Problems found during the review are recorded separately, in [`known-issues.md`](known-issues.md).

> **Terminology: "change event".** These documents call one difference between the two models a
> **change event**. That term was decided on 2026-10-10. The tool itself still says just
> "event": the JSON's `events` field, the terminal's "semantic events", and the code. Its wording
> will be updated when the tool is next changed ([known issue 3](known-issues.md)).

---

## What the tool works with

The examples show the desired behavior. Where the tool differs today is listed at the end of
this section.

> **Prerequisite: third-party code does not change.** In v0, third-party and vendored code must be
> identical at both points ([ARCH-001 §13](../../docs/architecture/overview.md#13--v0)). The only
> changes analyzed are to code this repository owns and contains. The tool does not work this way
> yet ([known issue 7](known-issues.md)).

### Step 0 · A codebase at two points in time

```
 one git repository                                                      time
 ─●──────●──────●──────●──────●──────●──────●──────●──────●──────●──────●───▶
                │                                                │
                ▼                                                ▼
 ┌─────────────────────────────┐                  ┌─────────────────────────────┐
 │ Codebase                    │                  │ Codebase                    │
 │                             │                  │                             │
 │ base (before)               │                  │ head (after)                │
 └─────────────────────────────┘                  └─────────────────────────────┘
```

### Step 1 · A model of each

```
 one git repository                                                      time
 ─●──────●──────●──────●──────●──────●──────●──────●──────●──────●──────●───▶
                │                                                │
                ▼                                                ▼
 ┌─────────────────────────────┐                  ┌─────────────────────────────┐
 │ Model · base (before)       │                  │ Model · head (after)        │
 │                             │                  │                             │
 │ Entities                    │                  │ Entities                    │
 │   namespace  class  struct  │                  │   namespace  class  struct  │
 │   function   method field   │                  │   function   method field   │
 │                             │                  │                             │
 │ Relationships               │                  │ Relationships               │
 │   A ──contains──▶ B         │                  │   A ──contains──▶ B         │
 │   A ──calls─────▶ B         │                  │   A ──calls─────▶ B         │
 │   A ──inherits──▶ B         │                  │   A ──inherits──▶ B         │
 │   A ──overrides─▶ B         │                  │   A ──overrides─▶ B         │
 └─────────────────────────────┘                  └─────────────────────────────┘
```

Example: this code

```cpp
void render();

class Base {
public:
    virtual void draw();
};

namespace ui {

class Widget : public Base {
public:
    void draw() override { render(); }
};

}  // namespace ui
```

becomes this model. This is the tool's actual output for that code, every entity and relationship:

```
                                       ┌──────────────┐
                                       │ namespace ui │
                                       └──────┬───────┘
                                              │ contains
                                              ▼
   ┌──────────────┐      inherits      ┌──────────────┐
   │ class Base   │ ◀───────────────── │ class Widget │
   └──────┬───────┘                    └──────┬───────┘
          │ contains                          │ contains
          ▼                                   ▼
   ┌──────────────┐      overrides     ┌──────────────┐        calls       ┌─────────────────┐
   │ method draw  │ ◀───────────────── │ method draw  │ ─────────────────▶ │ function render │
   └──────────────┘                    └──────────────┘                    └─────────────────┘
```

Each entity carries:

| Property | Example |
|---|---|
| Kind | `method` |
| Location | `widget.cpp:12` |
| Name | `ui::Widget::draw` |
| Signature | `void ()` |
| Attributes | `access: public` · `virtual: true` · `const: false` · `static: false` · `exception_spec: none`, and six more. A change to any of them is reported, for example a method that stops being virtual. |
| Body fingerprint | `9154ab59623de38994bbc2b23b92c61e`, a hash of the function's code, so a changed body is detectable |
| Identity | `c:@N@ui@S@Widget@F@draw#`, a unique id clang builds from kind, qualified name and parameter types |

Each relationship carries:

| Property | Example |
|---|---|
| Kind | `calls` |
| Location | `widget.cpp:12`, where the call is written |
| From | `ui::Widget::draw` |
| To | `render` |
| Attributes | `virtual: false`, meaning the call is not dispatched through a virtual function. Attributes depend on the kind: a call records whether it is virtual, and inheritance records its access (`public`, `protected` or `private`). The other kinds have none. |
| Identity | `c:@N@ui@S@Widget@F@draw#\|calls\|c:@F@render#`: the identities of its two ends, joined by its kind |

The two models are what get compared. Everything the tool reports comes from the differences
between them.

### Step 2 · A pairing between the two models

Each entity in one model is paired with the same entity in the other, or with nothing. No entity
is paired more than once.

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │  before                                           after                     │
 │                                                                             │
 │  Matched pairs                                                              │
 │    entity                    ◀─────────────────▶  entity                    │
 │                                                                             │
 │  Only in before                                                             │
 │    entity                                         —                         │
 │                                                                             │
 │  Only in after                                                              │
 │    —                                              entity                    │
 └─────────────────────────────────────────────────────────────────────────────┘
```

A relationship needs no pairing of its own: it is the same on both sides when its kind and both
of its ends are the same.

Example: the step 1 code, changed so that `render` takes a parameter and `draw` also calls a new
function `log`:

```diff
-void render();
+void render(int scale);
+void log();
 
 class Base {
 public:
     virtual void draw();
 };
 
 namespace ui {
 
 class Widget : public Base {
 public:
-    void draw() override { render(); }
+    void draw() override { render(2); log(); }
 };
 
 }  // namespace ui
```

The pairing of the two versions:

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │  before                                           after                     │
 │                                                                             │
 │  Matched pairs                                                              │
 │    namespace ui              ◀─────────────────▶  namespace ui              │
 │    class Base                ◀─────────────────▶  class Base                │
 │    method Base::draw         ◀─────────────────▶  method Base::draw         │
 │    class ui::Widget          ◀─────────────────▶  class ui::Widget          │
 │    method ui::Widget::draw   ◀─────────────────▶  method ui::Widget::draw   │
 │    function render()         ◀─────────────────▶  function render(int)      │
 │                                                                             │
 │  Only in before                                                             │
 │    (none)                                                                   │
 │                                                                             │
 │  Only in after                                                              │
 │    —                                              function log()            │
 └─────────────────────────────────────────────────────────────────────────────┘
```

- **`ui::Widget::draw` is paired even though its body changed.** Pairing only decides which
  entities are the same. What changed inside them appears in step 3.
- **`render` is paired even though its parameters changed.** It is the same function with a new
  signature.

### Step 3 · A list of differences

Each difference between the two models is a **change event**. Every change event comes from
the pairing:

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ Change events                                                               │
 │                                                                             │
 │  Entity change events                                                       │
 │    added                 an entity only in after                            │
 │    removed               an entity only in before                           │
 │    renamed               a matched pair with different names                │
 │    moved                 a matched pair with different parents              │
 │    signature changed     a matched pair with different signatures           │
 │    attribute changed     a matched pair with a different attribute          │
 │    body changed          a matched pair with different body fingerprints    │
 │                                                                             │
 │  Relationship change events                                                 │
 │    added                 a relationship only in after                       │
 │    removed               a relationship only in before                      │
 │    kind changed          same two ends, different kind                      │
 │    attribute changed     same relationship, a different attribute           │
 └─────────────────────────────────────────────────────────────────────────────┘
```

Each change event also records the before and after values, where there are two, and its
location in the source.

Example: the change events for the step 2 pairing.

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ Change events                                                               │
 │                                                                             │
 │  body changed          method ui::Widget::draw                              │
 │  signature changed     function render         void () → void (int)         │
 │  added                 function log                                         │
 │  relationship added    ui::Widget::draw ──calls──▶ log                      │
 └─────────────────────────────────────────────────────────────────────────────┘
```

The call from `ui::Widget::draw` to `render` is the same before and after, because `render` is
paired with its old version. So there is no change event for it.

### Step 4A · Causes linked to their consequences

The change events from step 3, unchanged, with links added: a change event that happened only
because of another one is linked to it as its consequence.

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ Change events, linked                                                       │
 │                                                                             │
 │   change event                 top-level: nothing caused it                 │
 │   ├── change event             caused by the change event above             │
 │   │   └── change event         a consequence can have consequences          │
 │   └── change event                                                          │
 │                                                                             │
 │   change event                 top-level, with no consequences              │
 │                                                                             │
 │  Links: a cause and its consequences                                        │
 │    entity removed              ──▶  everything it contained, removed        │
 │    entity added                ──▶  everything it contains, added           │
 │    entity removed              ──▶  every relationship touching it, removed │
 │    entity added                ──▶  every relationship touching it, added   │
 │    call to an overload added   ──▶  the call it replaced, removed           │
 └─────────────────────────────────────────────────────────────────────────────┘
```

Every change event is still in the list. A view can fold consequences under their cause, so a
reader starts from the top-level change events.

Example: the step 3 change events, linked.

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ Change events, linked                                                       │
 │                                                                             │
 │   body changed            method ui::Widget::draw                           │
 │                                                                             │
 │   signature changed       function render   void () → void (int)            │
 │                                                                             │
 │   added                   function log                                      │
 │   └── relationship added  ui::Widget::draw ──calls──▶ log                   │
 └─────────────────────────────────────────────────────────────────────────────┘
```

The call from `ui::Widget::draw` to `log` exists only because `log` was added, so it is that
addition's consequence. The other three change events are top-level.

### Step 4B · A risk score for each change event

Each change event from step 3 gets a risk score: how much a reviewer should care about it. A
change event is scored on its own, without the links from step 4A, so steps 4A and 4B are
independent of each other.

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ Change events, scored                                                       │
 │                                                                             │
 │  Each change event carries                                                  │
 │    risk score      the weighted sum of the four factors, 0 to 1             │
 │    impact          the same sum without uncertainty, 0 to 1                 │
 │                                                                             │
 │  Factors           weight    measures                                       │
 │    blast radius    35 %      how many entities depend on the changed one    │
 │    exposure        25 %      who can see it: public, internal or test       │
 │    novelty         25 %      how serious this kind of change is             │
 │    uncertainty     15 %      how unsure the pairing is                      │
 └─────────────────────────────────────────────────────────────────────────────┘
```

Impact leaves uncertainty out so that the tool's doubt about its own pairing cannot make a change
look more dangerous than it is.

Example: the step 3 change events, scored. These are the tool's own numbers for this code.

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ Change events, scored                                                       │
 │                                                                             │
 │                                 blast  expo-  nov-   uncer- risk   impact   │
 │                                 radius sure   elty   tainty score           │
 │                                                                             │
 │   body changed       draw       0.24   0.60   0.30   0.00   0.31   0.36     │
 │   signature changed  render     0.30   0.60   ?      ?      ?      ?        │
 │   added              log        0.30   0.60   0.85   1.00   0.62   0.55     │
 │   relationship added draw→log   0.24   0.60   0.35   1.00   0.47   0.38     │
 └─────────────────────────────────────────────────────────────────────────────┘
```

- **Blast radius.** `render` and `log` have three dependents each: `draw` calls them, `Widget`
  contains `draw`, and `ui` contains `Widget`. `draw` has two.
- **Exposure.** Everything is internal (0.60), because `widget.cpp` is not a public header.
- **`render`** cannot be scored yet. "Signature changed" has no novelty value, because the change
  event does not exist today. Its uncertainty depends on how pairing will recognize a changed
  signature.

### Step 5 · A review

The linked change events from step 4A and their scores from step 4B, combined into what a
reviewer reads:

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ Review                                                                      │
 │                                                                             │
 │   change events · top-level · high-risk · groups                            │
 │                                                                             │
 │   Group 1                                                                   │
 │    0.86   ● change event                 ● high-risk                        │
 │    0.71     └── change event             capped at its cause's score        │
 │    0.40   change event                   highest score first                │
 │                                                                             │
 │   Group 2                                                                   │
 │    0.51   change event                   groups ordered by their top score  │
 └─────────────────────────────────────────────────────────────────────────────┘
```

- **Cap.** A consequence never scores above its cause. If it would, its score is lowered to its
  cause's.
- **High-risk.** A change event is high-risk when its impact, after the cap, is at least 0.6.
- **Groups.** Change events belong to one group when they are about the same entity, when one
  caused the other, when their entities are directly related (one calls, inherits from or
  overrides the other) and both have change events, or when their entities are in the same
  class. Sharing a namespace does not count. Unconnected groups are separate pieces of work.
- **Order.** Highest score first. At equal scores, a cause comes before its consequence.

Example: the step 4A links and step 4B scores, combined.

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ Review                                                                      │
 │                                                                             │
 │   4 change events · 3 top-level · 0 high-risk · 1 group                     │
 │                                                                             │
 │   Group 1                                                                   │
 │    0.62   added                   function log                              │
 │    0.47   └── relationship added  ui::Widget::draw ──calls──▶ log           │
 │    0.31   body changed            method ui::Widget::draw                   │
 │    ?      signature changed       function render   void () → void (int)    │
 └─────────────────────────────────────────────────────────────────────────────┘
```

All four change events form one group, because `ui::Widget::draw` calls both `render` and `log`
and all three have change events. Nothing is capped: the call to `log` already scores below
`log`'s addition. `render` takes its place in the order, and may turn out to be high-risk, once
"signature changed" can be scored.

#### The JSON document

The review is written out as one JSON document per run. This document is the product: the
terminal output is only one way of displaying it. Its sections, in the order of the steps that
produce them, with the values for the example:

| Step | Section | What it holds | Example |
|---|---|---|---|
| 0 | `meta` | The tool and its version, the two commits compared, and how long each step took | the before and after versions |
| 1 | `completeness` | What could not be modelled on each side: source files that failed to parse, and relationships whose far end lies outside the repository | 1 of 1 file parsed on each side |
| 1 | `entities` | For every entity a change event mentions: identity, kind, name, signature, location, and whether it exists before, after or both. Not attributes or body fingerprint. Only these entities, not the whole model. Relationships have no section of their own: each appears only as the subject of a change event. | `ui::Widget::draw`, `render`, `log` |
| 2 | `summary.residue` | How many entities are matched pairs, only in before, and only in after | 6 · 0 · 1 |
| 3 | change events (`events`) | Every change event, none hidden: its kind, what it is about, the before and after values, and its location | 4 |
| 4A | `events[].caused_by` | The change event that caused this one, if any | 1, the call to `log`, caused by `log` added |
| 4B | `scores` | Each change event's risk score, impact, and the four factors behind them | `log` added: risk score 0.62, impact 0.55 |
| 5 | `scores[].bounded_by` | Set when the cap lowered a score: the cause whose score capped it | none |
| 5 | order (`ranking`) | Change event ids, highest risk score first | `log` added, its call, `draw` body changed, `render` (not yet scored) |
| 5 | groups (`components`) | Change events grouped into separate pieces of work | 1 group |
| 5 | `summary` | The headline counts: change events, top-level, high-risk, groups | 4 · 3 · 0 · 1 |
| — | `findings` | Reserved for evidence from other tools, such as tests, coverage and sanitizers. Always empty in v0, because that stage has not been built. | empty |

### Overview

All the steps in one picture. 4A and 4B are independent of each other: both start from step 3,
and step 5 combines them.

```
      ┌────────────────────────┐              ┌────────────────────────┐
0     │   Codebase · before    │              │    Codebase · after    │
      └────────────┬───────────┘              └────────────┬───────────┘
                   │                                       │
      ┌────────────┴───────────┐              ┌────────────┴───────────┐
1     │     Model · before     │              │     Model · after      │
      └────────────┬───────────┘              └────────────┬───────────┘
                   └───────────────────┬───────────────────┘
                          ┌────────────┴───────────┐
2                         │        Pairing         │
                          └────────────┬───────────┘
                                       │
                          ┌────────────┴───────────┐
3                         │     Change events      │
                          └────────────┬───────────┘
                   ┌───────────────────┴───────────────────┐
      ┌────────────┴───────────┐              ┌────────────┴───────────┐
4A    │ Change events · linked │              │ Change events · scored │   4B
      └────────────┬───────────┘              └────────────┬───────────┘
                   └───────────────────┬───────────────────┘
                          ┌────────────┴───────────┐
5                         │         Review         │
                          └────────────────────────┘
```

### Where each step is implemented

| Step | What exists | Implemented in | Notes |
|---|---|---|---|
| 0 | A codebase at two points in time | [`acquire/`](../../src/cpp_semantic_diff/acquire/) | |
| 1 | A model of each | [`produce/`](../../src/cpp_semantic_diff/produce/), with the model types in [`model/ir.py`](../../src/cpp_semantic_diff/model/ir.py) | The model is incomplete. For example, enums, global variables and type aliases are missing. See [`the-model.md`](the-model.md) and [known issue 2](known-issues.md). |
| 2 | A pairing between the two models | [`match/`](../../src/cpp_semantic_diff/match/) | Today two entities are paired only when their identities are exactly equal. A function whose parameters changed is therefore not paired with its old version, unlike the example. See [known issue 1](known-issues.md). |
| 3 | A list of differences | [`diff/`](../../src/cpp_semantic_diff/diff/) | Today the example produces seven change events, not four: `render` is reported as removed and added, and its call as removed and added ([known issue 1](known-issues.md)). There is no "signature changed" change event. "Renamed" cannot occur, because the name is part of the identity. |
| 4A | Causes linked to their consequences | [`classify/`](../../src/cpp_semantic_diff/classify/), with the links in [`classify/rules.py`](../../src/cpp_semantic_diff/classify/rules.py) | Today the example has four top-level change events and three consequences. `render()` removed and `render(int)` added each carry their call as a consequence, and so does `log` ([known issue 1](known-issues.md)). Two more links are written as rules in [`classify/rules.py`](../../src/cpp_semantic_diff/classify/rules.py) but cannot occur today. *A rename linked to the calls it retargets* (e.g. `render` renamed to `paint`) is never needed: once a renamed entity is paired, its calls are the same relationships before and after, so there is nothing to link. *A signature change linked to the callers it may break* (e.g. `render(int)` becoming `render(double)` while a caller still passes `2`) needs a change event no stage produces yet ([known issue 5](known-issues.md)). |
| 4B | A risk score for each change event | [`rank/score.py`](../../src/cpp_semantic_diff/rank/score.py), with every weight and value in [`rank/weights.py`](../../src/cpp_semantic_diff/rank/weights.py) | Today 4B and step 5 are one stage, [`rank/`](../../src/cpp_semantic_diff/rank/). Exposure is decided from file paths. `render` cannot be scored, because "signature changed" does not exist ([known issue 1](known-issues.md)). |
| 5 | A review | [`rank/ranker.py`](../../src/cpp_semantic_diff/rank/ranker.py), with groups in [`rank/components.py`](../../src/cpp_semantic_diff/rank/components.py); written out by [`render/`](../../src/cpp_semantic_diff/render/) | Today the example gives seven change events, four top-level, none high-risk, in one group ([known issue 1](known-issues.md)). The tool calls groups *components* and numbers them from 0. |
