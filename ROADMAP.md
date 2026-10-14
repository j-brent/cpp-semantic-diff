<!-- metadata
type: roadmap
kind: living
status: active
author: Brent Runyan
created: 2026-10-10
-->

# Roadmap

*Living document: the scope of the work, its order, and the reasons for it.*

The work itself, and its status, is on GitHub ([ADR-032](docs/adr/ADR-032-work-is-tracked-on-github.md)):
the overview issue [#25](https://github.com/j-brent/cpp-semantic-diff/issues/25), the
[v0 milestone](https://github.com/j-brent/cpp-semantic-diff/milestone/1) and the
[project board](https://github.com/users/j-brent/projects/1).

The history behind every item is in the frozen
[v0 tracker](reports/2026-08-01--v0-build/STATUS.md) (2026-08-01 to 2026-10-10) and the frozen
[2026-10-10 review](reports/2026-10-10--up-to-speed/project-review.md). Conventions are in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## 1 · Scope

v0 is a tool whose output can be trusted on changes the repository controls. What is in and out is
[ADR-030](docs/adr/ADR-030-what-v0-is.md), amended by
[ADR-041](docs/adr/ADR-041-v0-scope-of-steps-6-and-7.md) and
[ADR-045](docs/adr/ADR-045-same-named-statics-and-unnamed-records-stay-out-of-v0.md). The issues in
v0 are in the v0 milestone; issues with no milestone come later.

---

## 2 · Order

**v0** was built in this order:

1. **Third-party code outside the model:**
   [#3](https://github.com/j-brent/cpp-semantic-diff/issues/3), with
   [#4](https://github.com/j-brent/cpp-semantic-diff/issues/4) in the same schema change. First,
   because reporting what the model cannot see ([#6](https://github.com/j-brent/cpp-semantic-diff/issues/6))
   excludes third-party code, and needed the boundary drawn first
   ([ARCH-001 §4.5](docs/ARCH-001-architecture.md#45-the-boundary-of-the-model)).
2. **The model:** [#5](https://github.com/j-brent/cpp-semantic-diff/issues/5)
   ([PLAN-006](docs/plans/PLAN-006-model-sufficiency.md)). Most ordinary changes produced no change
   event at all ([`the-model.md`](reports/2026-10-10--up-to-speed/the-model.md)), so the model came
   before anything that reads its output. The change catalog defines when each step is done.
3. **Not ordered in advance:** [#15](https://github.com/j-brent/cpp-semantic-diff/issues/15),
   [#16](https://github.com/j-brent/cpp-semantic-diff/issues/16),
   [#17](https://github.com/j-brent/cpp-semantic-diff/issues/17),
   [#18](https://github.com/j-brent/cpp-semantic-diff/issues/18),
   [#19](https://github.com/j-brent/cpp-semantic-diff/issues/19),
   [#20](https://github.com/j-brent/cpp-semantic-diff/issues/20). #15 and #16 depended on
   [#8](https://github.com/j-brent/cpp-semantic-diff/issues/8).

Three pieces of v0's planned scope moved out, each to its own issue:
[#10](https://github.com/j-brent/cpp-semantic-diff/issues/10), the uses relationship
([ADR-041](docs/adr/ADR-041-v0-scope-of-steps-6-and-7.md));
[#34](https://github.com/j-brent/cpp-semantic-diff/issues/34), field order and layout (ADR-041);
and [#49](https://github.com/j-brent/cpp-semantic-diff/issues/49), counting same-named statics, from
#7's cut slice
([ADR-045](docs/adr/ADR-045-same-named-statics-and-unnamed-records-stay-out-of-v0.md)).

**After v0:**
- **Bitcoin Core probe:** [#23](https://github.com/j-brent/cpp-semantic-diff/issues/23). It waited
  for work that is now in v0: the confirmed issues, PLAN-006's v0 steps and the
  rename-map fix ([#18](https://github.com/j-brent/cpp-semantic-diff/issues/18)). Probing scale
  before the model was right would have measured the wrong thing, and large diffs are where git
  silently stops detecting renames, so probing without that fix could measure a wrong answer
  with no signal.
- **Not ordered:** [#2](https://github.com/j-brent/cpp-semantic-diff/issues/2),
  [#10](https://github.com/j-brent/cpp-semantic-diff/issues/10),
  [#12](https://github.com/j-brent/cpp-semantic-diff/issues/12),
  [#13](https://github.com/j-brent/cpp-semantic-diff/issues/13),
  [#14](https://github.com/j-brent/cpp-semantic-diff/issues/14),
  [#21](https://github.com/j-brent/cpp-semantic-diff/issues/21),
  [#22](https://github.com/j-brent/cpp-semantic-diff/issues/22),
  [#24](https://github.com/j-brent/cpp-semantic-diff/issues/24),
  [#34](https://github.com/j-brent/cpp-semantic-diff/issues/34),
  [#49](https://github.com/j-brent/cpp-semantic-diff/issues/49).

---

## 3 · Dropped

Items from the v0 tracker that were reviewed on 2026-10-10 and will not be worked on, so have no
issue:

- **Identity tiers 2–4.** [#7](https://github.com/j-brent/cpp-semantic-diff/issues/7) and
  [#8](https://github.com/j-brent/cpp-semantic-diff/issues/8) replace them.
- **A cause rule for a method that becomes `virtual`.** Revisit with
  [#15](https://github.com/j-brent/cpp-semantic-diff/issues/15).
- **`cppsd cache gc` collects by age, not identity.** An accepted design, not a defect
  ([ADR-021](docs/adr/ADR-021-cache-gc-collects-by-age.md)).
- **Showing the model fingerprint; exposure using `Node.tu`.** No use yet.
- **Where the reviewer reads the output.** Out of scope for now
  ([ADR-030](docs/adr/ADR-030-what-v0-is.md)). Ad hoc views, such as the renderer in
  `reports/2026-10-10--up-to-speed/demo-run-a/`, suffice while developing.

---

## 4 · Decisions

Decisions and their reasons are architecture decision records in [`docs/adr/`](docs/adr/). The
ones in force are those whose status is accepted.
