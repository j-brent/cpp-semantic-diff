"""The change catalog: for each common change, does the reviewer get the right change event?

Each case is a before, an after and the outcome a reviewer should get, run through the real
extractor, matcher and differ. Where the cases come from, and what each produced on
2026-10-10, is in reports/2026-10-10--up-to-speed/the-model.md (section 4) and the probe
scripts in `model-probes/` beside it.

**Adding a case** is one `case(...)` row in `CATALOG`: an id, the two sources, and the outcome
written with the vocabulary below (`changed`, `added`, `not_replaced`, `not_visible`, ...).

**`fails=` marks a strict expected failure**: a change the tool gets wrong today, with the step
expected to fix it. When that step lands the case passes, strict mode turns the pass into a
failure, and whoever landed it removes the marker. The catalog is done when no marker is left.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.diff import diff_models
from cpp_semantic_diff.errors import PreconditionError
from cpp_semantic_diff.match import match_models
from cpp_semantic_diff.model import (
    AnyId,
    AttrValue,
    Confidence,
    Edge,
    EdgeKind,
    Event,
    EventKind,
    Model,
    Node,
    NodeId,
    NodeKind,
    Residue,
    Side,
)
from cpp_semantic_diff.produce.build import assemble
from cpp_semantic_diff.produce.extract import extract
from cpp_semantic_diff.produce.toolchain import Toolchain, load

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ADDITIONS = frozenset({EventKind.NODE_ADDED, EventKind.EDGE_ADDED})
REMOVALS = frozenset({EventKind.NODE_REMOVED, EventKind.EDGE_REMOVED})


# -- Running a case ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    try:
        return load()
    except PreconditionError as error:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable libclang: {error}")


def build_model(toolchain: Toolchain, root: Path, source: str, rev: str) -> Model:
    root.mkdir()
    (root / "a.cpp").write_text(source, encoding="utf-8")
    entry = CompileEntry(
        file=root / "a.cpp",
        directory=root,
        arguments=["clang++", "-std=c++20", "-c", "a.cpp", "-o", "a.o"],
    )
    model = assemble(rev, [extract(entry, root=root, toolchain=toolchain)], toolchain=toolchain)
    if not model.completeness.is_complete:
        # Not an assertion: a source that does not parse would make "no event" pass for the
        # wrong reason, and an expected failure must not absorb that (see `case`).
        pytest.fail(f"case source did not parse: {model.completeness.tus}")
    return model


@dataclass(frozen=True)
class Outcome:
    """What the reviewer gets: both models, and the change events between them."""

    base: Model
    head: Model
    events: list[Event]

    def name(self, node_id: str) -> str:
        key = NodeId(node_id)
        node = self.head.nodes.get(key) or self.base.nodes.get(key)
        return node.qualified_name if node else node_id

    def subject(self, event: Event) -> str:
        """The entity an event is about. An edge's event is about the edge's source."""
        edge = self._edge(event)
        return self.name(edge[0]) if edge else self.name(event.subject)

    def names(self, event: Event) -> set[str]:
        """Everything else the event names: an edge's target, and its payload values."""
        edge = self._edge(event)
        named = {str(value) for value in event.payload.values()}
        return named | {self.name(edge[1])} if edge else named

    def _edge(self, event: Event) -> tuple[str, str] | None:
        for model in (self.base, self.head):
            for edge in model.edges:
                if edge.id == event.subject:
                    return edge.src, edge.dst
        return None

    def describe(self) -> str:
        return (
            "\n".join(
                f"  {e.kind.value} {self.subject(e)} {sorted(self.names(e))}" for e in self.events
            )
            or "  (no change events)"
        )


def reported_not_visible(outcome: Outcome) -> list[str]:
    """What the run says it could not see.

    No run reports this yet, so this returns nothing and every case that expects it is an
    expected failure. The report it stands for is decided: libclang does not expose the copy
    and move operations the compiler writes, and a run is to state that limit as a number,
    the way it states dropped edges (ARCH-001 §11). When that report exists, this function
    reads it from `outcome`. Nothing else in the catalog changes.
    """
    return []


# -- The outcome vocabulary -------------------------------------------------------------------
#
# Each expectation is in a reviewer's terms: which entity an event is about, and whether it
# is a change, an addition or a removal. None names an event kind, so the cases survive
# change events being specialized by entity kind. The one kind distinction used is addition
# and removal, because a change reported as a removal plus an addition is the failure several
# cases exist to catch.


@dataclass(frozen=True)
class Expect:
    says: str
    holds: Callable[[Outcome], bool]


def changed(*entities: str, was: object = None, now: object = None, naming: str = "") -> Expect:
    """A change event, not an addition or removal, about one of `entities`.

    `was` and `now` are values the event must carry, in any field. `naming` is another entity
    it must name, such as the base class of a changed inheritance.
    """

    def holds(o: Outcome) -> bool:
        return any(
            e.kind not in ADDITIONS | REMOVALS
            and o.subject(e) in entities
            and all(str(v) in o.names(e) for v in (was, now) if v is not None)
            and (not naming or naming in o.names(e))
            for e in o.events
        )

    says = f"a change event on {' or '.join(entities)}"
    says += f" saying it went from {was} to {now}" if was is not None else ""
    return Expect(says + (f" naming {naming}" if naming else ""), holds)


def added(entity: str) -> Expect:
    return Expect(
        f"an addition of {entity}",
        lambda o: any(e.kind in ADDITIONS and o.subject(e) == entity for e in o.events),
    )


def removed_at(line: int) -> Expect:
    """A removal of whatever stood at `line` before. For an entity with no name."""
    return Expect(
        f"a removal of what was on line {line}",
        lambda o: any(
            e.kind in REMOVALS
            and any(h.side is Side.BASE and h.contains(line) for h in e.provenance)
            for e in o.events
        ),
    )


def nothing_added() -> Expect:
    return Expect("no addition", lambda o: not any(e.kind in ADDITIONS for e in o.events))


def not_replaced(name: str) -> Expect:
    """No removal of an entity called `name` alongside an addition of one of the same kind.

    By unqualified name, so it also catches a replacement whose scope was renamed.
    """

    def kinds(o: Outcome, of: frozenset[EventKind], model: Model) -> set[str]:
        return {
            model.nodes[NodeId(e.subject)].kind
            for e in o.events
            if e.kind in of
            and e.subject in model.nodes
            and model.nodes[NodeId(e.subject)].name == name
        }

    return Expect(
        f"no removal and addition of {name}",
        lambda o: not (kinds(o, REMOVALS, o.base) & kinds(o, ADDITIONS, o.head)),
    )


def about(entity: str) -> Expect:
    """Any event about `entity`: the least a reviewer needs to see that it was touched."""
    return Expect(
        f"an event about {entity}", lambda o: any(o.subject(e) == entity for e in o.events)
    )


def relation(entity: str, other: str) -> Expect:
    """An event about `entity` that names `other`: a relationship between them changed."""
    return Expect(
        f"an event about {entity} naming {other}",
        lambda o: any(o.subject(e) == entity and other in o.names(e) for e in o.events),
    )


def some_event() -> Expect:
    return Expect("at least one change event", lambda o: bool(o.events))


def no_events() -> Expect:
    return Expect("no change events", lambda o: not o.events)


def not_visible(topic: str) -> Expect:
    return Expect(
        f"reported as not visible: {topic}",
        lambda o: any(topic in item.lower() for item in reported_not_visible(o)),
    )


# -- The catalog ------------------------------------------------------------------------------


def case(id: str, before: str, after: str, *expect: Expect, fails: str = "") -> object:
    """One row. `fails` names the step expected to fix a case the tool gets wrong today.

    `raises=AssertionError` because only an unmet expectation is the expected failure. A
    source that does not parse, or a crash in the pipeline, fails the suite.
    """
    marks = (
        pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"PLAN-006 {fails}")
        if fails
        else ()
    )
    return pytest.param(before, after, expect, id=id, marks=marks)


CATALOG = [
    # Changes that produced no change event on 2026-10-10.
    case(
        "default-argument-value",
        "void f(int x = 1) {}",
        "void f(int x = 2) {}",
        changed("f"),
        not_replaced("f"),
        fails="step 7: default arguments are not compared",
    ),
    case(
        "constructor-initializer-list",
        "struct S { int x; S() : x(1) {} };",
        "struct S { int x; S() : x(2) {} };",
        changed("S::S"),
        fails="step 7: the initializer list is outside the body fingerprint",
    ),
    case(
        "default-member-initializer",
        "struct S { int x = 1; };",
        "struct S { int x = 2; };",
        changed("S::x"),
        fails="step 7: default member initializers are not compared",
    ),
    case(
        "macro-value",
        "#define LIMIT 10\nint f() { return LIMIT; }",
        "#define LIMIT 20\nint f() { return LIMIT; }",
        changed("LIMIT"),
        fails="step 5: macros are not modelled",
    ),
    case(
        "constexpr-global-value",
        "constexpr int kLimit = 10;\nint f() { return kLimit; }",
        "constexpr int kLimit = 20;\nint f() { return kLimit; }",
        changed("kLimit", was=10, now=20),
        fails="step 5: variables are not modelled",
    ),
    case(
        "enumerator-value",
        "enum E { A = 1, B = 2 };\nint f() { return B; }",
        "enum E { A = 1, B = 3 };\nint f() { return B; }",
        changed("E::B", was=2, now=3),
        fails="step 5: enumerators are not modelled",
    ),
    case(
        "enumerator-added",
        "enum E { A, B };",
        "enum E { A, B, C };",
        added("E::C"),
        fails="step 5: enumerators are not modelled",
    ),
    case(
        "static-data-member-value",
        "struct S { static constexpr int n = 4; };",
        "struct S { static constexpr int n = 8; };",
        changed("S::n", was=4, now=8),
        fails="step 5: static data members are not modelled",
    ),
    case(
        "field-order",
        "struct S { char a; double b; int c; };",
        "struct S { double b; char a; int c; };",
        changed("S", "S::a", "S::b"),
        fails="step 7: field order and offset are not compared",
    ),
    case(
        "base-order",
        "struct A {}; struct B {}; struct S : A, B {};",
        "struct A {}; struct B {}; struct S : B, A {};",
        changed("S"),
        fails="step 7: base order is not compared",
    ),
    case(
        "base-becomes-virtual",
        "struct A {}; struct S : A {};",
        "struct A {}; struct S : virtual A {};",
        changed("S", naming="A"),
        fails="step 7: virtual inheritance is not compared",
    ),
    case(
        "constexpr-removed",
        "constexpr int f() { return 1; }",
        "int f() { return 1; }",
        changed("f"),
        fails="step 7: constexpr is not compared",
    ),
    case(
        "class-becomes-final",
        "struct S { virtual void f(); };",
        "struct S final { virtual void f(); };",
        changed("S"),
        fails="step 7: final is not compared",
    ),
    case(
        "nodiscard-added",
        "int f();",
        "[[nodiscard]] int f();",
        changed("f"),
        fails="step 7: [[nodiscard]] is not compared",
    ),
    case(
        "deprecated-added",
        "void f();",
        "[[deprecated]] void f();",
        changed("f"),
        fails="step 7: deprecation is not compared",
    ),
    case(
        "static-assert-removed",
        "struct S { int x; };\nstatic_assert(sizeof(S) == 4);",
        "struct S { int x; };",
        removed_at(2),
        fails="step 5: static assertions are not modelled",
    ),
    # Changes that arrived without their cause on 2026-10-10. Each has a second row for what
    # the tool already gets right, so that does not regress while the first is being fixed.
    case(
        "alias-target-changed",
        "using T = int;\nvoid f(T) {}\nvoid g() { f(1); }",
        "using T = long;\nvoid f(T) {}\nvoid g() { f(1); }",
        changed("T", was="int", now="long"),
        not_replaced("f"),
        fails="steps 4 and 5: aliases are not modelled, and parameter types are identity",
    ),
    case(
        "alias-target-changed-f-is-reported",
        "using T = int;\nvoid f(T) {}\nvoid g() { f(1); }",
        "using T = long;\nvoid f(T) {}\nvoid g() { f(1); }",
        about("f"),
    ),
    case(
        "inline-namespace-bumped",
        "namespace lib { inline namespace v1 { void f() {} } }",
        "namespace lib { inline namespace v2 { void f() {} } }",
        not_replaced("f"),
        some_event(),
        fails="step 4: inline namespace names are identity",
    ),
    case(
        "inline-namespace-bumped-is-reported",
        "namespace lib { inline namespace v1 { void f() {} } }",
        "namespace lib { inline namespace v2 { void f() {} } }",
        some_event(),
    ),
    case(
        "parameter-added",
        "void f(int) {}\nvoid g() { f(1); }",
        "void f(int, int = 0) {}\nvoid g() { f(1); }",
        changed("f"),
        not_replaced("f"),
        fails="step 4: parameter types are identity",
    ),
    case(
        "parameter-added-f-is-reported",
        "void f(int) {}\nvoid g() { f(1); }",
        "void f(int, int = 0) {}\nvoid g() { f(1); }",
        about("f"),
    ),
    # The three probes that produced change events, but not all the ones a reviewer needs.
    case(
        "template-stops-calling",
        "void h(int);\ntemplate<class T> void f(T t) { h(t); g(t); }",
        "void h(int);\ntemplate<class T> void f(T t) { g(t); }",
        relation("f", "h"),
        fails="step 8: calls that depend on a template parameter are dropped",
    ),
    case(
        "template-stops-calling-body-changed",
        "void h(int);\ntemplate<class T> void f(T t) { h(t); g(t); }",
        "void h(int);\ntemplate<class T> void f(T t) { g(t); }",
        changed("f"),
    ),
    case(
        "callback-replaced",
        "void cb();\nvoid reg(void(*)());\nvoid s() { reg(cb); }",
        "void cb();\nvoid cb2();\nvoid reg(void(*)());\nvoid s() { reg(cb2); }",
        relation("s", "cb"),
        relation("s", "cb2"),
        fails="step 6: taking a function's address is not a relationship",
    ),
    case(
        "callback-replaced-body-changed",
        "void cb();\nvoid reg(void(*)());\nvoid s() { reg(cb); }",
        "void cb();\nvoid cb2();\nvoid reg(void(*)());\nvoid s() { reg(cb2); }",
        changed("s"),
        added("cb2"),
    ),
    # Adding a destructor removes the implicit move operations, and libclang does not expose
    # them. Accepted as a limit, so the expected outcome is that the run says so.
    case(
        "destructor-added-implicit-move-lost",
        "struct S { int* p; };",
        "struct S { int* p; ~S(); };",
        not_visible("move"),
        fails="section 3 (decided) and step 2: the run does not state that implicit special "
        "members are not visible; when it does, make reported_not_visible() read it",
    ),
    case(
        "destructor-added",
        "struct S { int* p; };",
        "struct S { int* p; ~S(); };",
        added("S::~S"),
    ),
    # Identity. A macro's identifier carries its byte offset, and a static_assert has none.
    # The "-shifted" rows pass today only because neither is modelled; they are there to
    # fail if either is modelled with an identity that moves with the text.
    case(
        "macro-shifted",
        "#define LIMIT 10\nint f() { return LIMIT; }",
        "// the limit\n#define LIMIT 10\nint f() { return LIMIT; }",
        no_events(),
    ),
    case(
        "macro-shifted-and-changed",
        "#define LIMIT 10\nint f() { return LIMIT; }",
        "// the limit\n#define LIMIT 20\nint f() { return LIMIT; }",
        changed("LIMIT"),
        not_replaced("LIMIT"),
        fails="step 5: macros are not modelled, and their identifier moves with the text",
    ),
    case(
        "static-assert-shifted",
        "struct S { int x; };\nstatic_assert(sizeof(S) == 4);",
        "// layout\nstruct S { int x; };\nstatic_assert(sizeof(S) == 4);",
        no_events(),
    ),
    case(
        "static-assert-one-of-two-removed",
        "struct S { int x; };\nstatic_assert(sizeof(S) == 4);\nstatic_assert(alignof(S) == 4);",
        "// layout\nstruct S { int x; };\nstatic_assert(alignof(S) == 4);",
        removed_at(2),
        nothing_added(),
        fails="step 5: static assertions are not modelled, and have no identifier",
    ),
    # From the relationship probe (model-probes/tmpl.py): a namespace-scope initializer.
    case(
        "global-initializer-calls-another",
        "int g();\nint h();\nint x = g();",
        "int g();\nint h();\nint x = h();",
        relation("x", "h"),
        fails="steps 5 and 8: variables are not modelled, nor calls from initializers",
    ),
]


@pytest.mark.parametrize(("before", "after", "expect"), CATALOG)
def test_change_catalog(
    toolchain: Toolchain, tmp_path: Path, before: str, after: str, expect: tuple[Expect, ...]
) -> None:
    base = build_model(toolchain, tmp_path / "base", before, "a" * 40)
    head = build_model(toolchain, tmp_path / "head", after, "b" * 40)
    outcome = Outcome(base, head, diff_models(base, head, match_models(base, head)))
    unmet = [e.says for e in expect if not e.holds(outcome)]
    assert not unmet, f"expected {'; '.join(unmet)}. Got:\n{outcome.describe()}"


# -- The vocabulary discriminates -------------------------------------------------------------
#
# An expected failure that fails for the wrong reason, such as a helper that can never hold,
# would sit in the catalog looking like a gap in the tool. So the helpers are checked against
# hand-built events: the event a fixed tool would plausibly emit, and the near misses.


def hand_built(nodes: list[Node], edges: list[Edge], *events: Event) -> Outcome:
    def side(rev: str) -> Model:
        model = Model(rev=rev * 40, edges=edges)
        for node in nodes:
            model.add_node(node)
        return model

    return Outcome(side("a"), side("b"), list(events))


def event(kind: EventKind, subject: AnyId, **payload: AttrValue) -> Event:
    return Event(
        kind=kind,
        subject=subject,
        payload=payload,
        confidence=Confidence(tier=None, residue=Residue.NONE, models_complete=True),
    )


def entity(kind: NodeKind, qualified: str) -> Node:
    name = qualified.split("::")[-1]
    return Node(id=NodeId(f"c:{qualified}"), kind=kind, name=name, qualified_name=qualified)


def test_an_enumerator_value_change_is_told_apart_from_its_near_misses() -> None:
    a, b = entity(NodeKind.ENUMERATOR, "E::A"), entity(NodeKind.ENUMERATOR, "E::B")
    expect = changed("E::B", was=2, now=3)

    def holds(*events: Event) -> bool:
        return expect.holds(hand_built([a, b], [], *events))

    assert holds(event(EventKind.NODE_ATTR_CHANGED, b.id, attr="value", old=2, new=3))
    assert not holds(event(EventKind.NODE_ATTR_CHANGED, b.id, attr="value", old=2, new=4))
    assert not holds(event(EventKind.NODE_ATTR_CHANGED, a.id, attr="value", old=2, new=3))
    assert not holds(event(EventKind.NODE_REMOVED, b.id), event(EventKind.NODE_ADDED, b.id))
    assert not holds()


def test_a_replaced_callback_needs_the_relationship_not_only_the_body_change() -> None:
    s, cb, cb2 = (entity(NodeKind.FUNCTION, name) for name in ("s", "cb", "cb2"))
    uses = [Edge(src=s.id, dst=f.id, kind=EdgeKind.USES_IN_IMPLEMENTATION) for f in (cb, cb2)]
    fixed = hand_built(
        [s, cb, cb2],
        uses,
        event(EventKind.EDGE_REMOVED, uses[0].id),
        event(EventKind.EDGE_ADDED, uses[1].id),
    )
    today = hand_built(
        [s, cb, cb2],
        [],
        event(EventKind.NODE_ADDED, cb2.id),
        event(EventKind.NODE_BODY_CHANGED, s.id),
    )
    for expect in (relation("s", "cb"), relation("s", "cb2")):
        assert expect.holds(fixed)
        assert not expect.holds(today)
    assert not_replaced("cb").holds(fixed)
    assert not not_replaced("s").holds(
        hand_built([s], [], event(EventKind.NODE_REMOVED, s.id), event(EventKind.NODE_ADDED, s.id))
    )
