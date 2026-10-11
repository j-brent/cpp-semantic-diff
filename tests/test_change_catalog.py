"""The change catalog: for each common change, does the reviewer get the right change event?

Each case is a before, an after and the outcome a reviewer should get, run through the real
extractor, matcher, differ and derive stage. Where the cases come from, and what each produced on
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
from typing import Any, cast

import pytest

from cpp_semantic_diff.acquire.tokens import LineHunk, TokenDelta, changed_runs
from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.classify import classify_events
from cpp_semantic_diff.derive import derive_events
from cpp_semantic_diff.diff import diff_models
from cpp_semantic_diff.diff.coverage import measure_coverage
from cpp_semantic_diff.errors import PreconditionError
from cpp_semantic_diff.match import match_models
from cpp_semantic_diff.model import (
    AnyId,
    AttrValue,
    ChangeEvent,
    ChangeEventKind,
    Confidence,
    Edge,
    EdgeKind,
    Model,
    Node,
    NodeId,
    NodeKind,
    Side,
    Unpaired,
)
from cpp_semantic_diff.produce.build import assemble
from cpp_semantic_diff.produce.extract import extract
from cpp_semantic_diff.produce.toolchain import Toolchain, load

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ADDITIONS = frozenset({ChangeEventKind.NODE_ADDED, ChangeEventKind.EDGE_ADDED})
REMOVALS = frozenset({ChangeEventKind.NODE_REMOVED, ChangeEventKind.EDGE_REMOVED})


# -- Running a case ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    try:
        return load()
    except PreconditionError as error:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable libclang: {error}")


def build_model(
    toolchain: Toolchain, root: Path, source: str, rev: str, file: str = "a.cpp"
) -> Model:
    root.mkdir()
    (root / file).write_text(source, encoding="utf-8")
    entry = CompileEntry(
        file=root / file,
        directory=root,
        arguments=["clang++", "-std=c++20", "-c", file, "-o", "a.o"],
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
    events: list[ChangeEvent]

    def name(self, node_id: str) -> str:
        key = NodeId(node_id)
        node = self.head.nodes.get(key) or self.base.nodes.get(key)
        return node.qualified_name if node else node_id

    def subject(self, event: ChangeEvent) -> str:
        """The entity an event is about. An edge's event is about the edge's source."""
        edge = self._edge(event)
        return self.name(edge[0]) if edge else self.name(event.subject)

    def names(self, event: ChangeEvent) -> set[str]:
        """Everything else the event names: an edge's target, and its payload values."""
        edge = self._edge(event)
        named = {str(value) for value in event.payload.values()}
        return named | {self.name(edge[1])} if edge else named

    def _edge(self, event: ChangeEvent) -> tuple[str, str] | None:
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
    """What the run says it could not see: each side's `Completeness.not_visible` (ADR-029)."""
    return [*outcome.base.completeness.not_visible, *outcome.head.completeness.not_visible]


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


def removed(entity: str) -> Expect:
    return Expect(
        f"a removal of {entity}",
        lambda o: any(e.kind in REMOVALS and o.subject(e) == entity for e in o.events),
    )


def untouched(entity: str) -> Expect:
    """No event about `entity`: it is the same entity, with nothing about it changed."""
    return Expect(
        f"no event about {entity}", lambda o: not any(o.subject(e) == entity for e in o.events)
    )


def nothing_added() -> Expect:
    return Expect("no addition", lambda o: not any(e.kind in ADDITIONS for e in o.events))


def not_replaced(name: str) -> Expect:
    """No removal of an entity called `name` alongside an addition of one of the same kind.

    By unqualified name, so it also catches a replacement whose scope was renamed.
    """

    def kinds(o: Outcome, of: frozenset[ChangeEventKind], model: Model) -> set[str]:
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


def callers_flagged(*expected: tuple[str, str, bool]) -> Expect:
    """Exactly these calls are flagged as calls into a changed signature: (caller, callee,
    whether the caller was itself edited). None given means no call is flagged.

    Read off the payload's `caller`, `callee` and `caller_edited` rather than the kind, like
    the rest of the vocabulary (ADR-040).
    """

    def flags(o: Outcome) -> list[tuple[str, str, bool]]:
        return sorted(
            (o.name(str(e.payload["caller"])), o.name(str(e.payload["callee"])), edited)
            for e in o.events
            if {"caller", "callee"} <= e.payload.keys()
            and isinstance(edited := e.payload.get("caller_edited"), bool)
        )

    says = ", ".join(
        f"{caller} -> {callee} ({'edited' if edited else 'not edited'})"
        for caller, callee, edited in expected
    )
    return Expect(f"calls flagged: {says or 'none'}", lambda o: flags(o) == sorted(expected))


def refused(name: str) -> Expect:
    """Every event about an entity called `name` is a removal or an addition, each reported
    as a pairing the tool declined to choose. Which old one became which new one is a guess
    the tool does not make."""

    def holds(o: Outcome) -> bool:
        nodes = [
            e
            for e in o.events
            if e.kind in (ChangeEventKind.NODE_ADDED, ChangeEventKind.NODE_REMOVED)
            and o.subject(e).rsplit("::", 1)[-1] == name
        ]
        changes = [
            e
            for e in o.events
            if e.kind not in ADDITIONS | REMOVALS and o.subject(e).rsplit("::", 1)[-1] == name
        ]
        return (
            bool(nodes)
            and not changes
            and all(e.confidence.unpaired is Unpaired.AMBIGUOUS for e in nodes)
        )

    return Expect(f"every {name} left unpaired as an ambiguous pairing", holds)


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


def case(
    id: str, before: str, after: str, *expect: Expect, fails: str = "", renamed_to: str = ""
) -> object:
    """One row. `fails` names the step expected to fix a case the tool gets wrong today.

    `renamed_to` names the file the source is in after the change, given to the matcher as a
    rename of `a.cpp`, as `acquire` would. By default the file keeps its name.

    `raises=AssertionError` because only an unmet expectation is the expected failure. A
    source that does not parse, or a crash in the pipeline, fails the suite.
    """
    marks = (
        pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"PLAN-006 {fails}")
        if fails
        else ()
    )
    return pytest.param(before, after, expect, renamed_to or "a.cpp", id=id, marks=marks)


CATALOG = [
    # Changes that produced no change event on 2026-10-10.
    case(
        "default-argument-value",
        "void f(int x = 1) {}",
        "void f(int x = 2) {}",
        changed("f"),
        not_replaced("f"),
    ),
    case(
        "constructor-initializer-list",
        "struct S { int x; S() : x(1) {} };",
        "struct S { int x; S() : x(2) {} };",
        changed("S::S"),
    ),
    case(
        "default-member-initializer",
        "struct S { int x = 1; };",
        "struct S { int x = 2; };",
        changed("S::x"),
    ),
    case(
        "macro-value",
        "#define LIMIT 10\nint f() { return LIMIT; }",
        "#define LIMIT 20\nint f() { return LIMIT; }",
        changed("LIMIT"),
    ),
    case(
        "constexpr-global-value",
        "constexpr int kLimit = 10;\nint f() { return kLimit; }",
        "constexpr int kLimit = 20;\nint f() { return kLimit; }",
        changed("kLimit", was=10, now=20),
    ),
    case(
        "enumerator-value",
        "enum E { A = 1, B = 2 };\nint f() { return B; }",
        "enum E { A = 1, B = 3 };\nint f() { return B; }",
        changed("E::B", was=2, now=3),
    ),
    case(
        "enumerator-added",
        "enum E { A, B };",
        "enum E { A, B, C };",
        added("E::C"),
    ),
    case(
        "enumerator-added-unhandled-in-switch",
        "enum E { A, B };\nint f(E e) { switch (e) { case A: return 1; case B: return 2; } }",
        "enum E { A, B, C };\nint f(E e) { switch (e) { case A: return 1; case B: return 2; } }",
        relation("f", "E::C"),
        fails="step 6 (#10, post-v0 per ADR-041): a switch is not checked against its enum",
    ),
    case(
        "static-data-member-value",
        "struct S { static constexpr int n = 4; };",
        "struct S { static constexpr int n = 8; };",
        changed("S::n", was=4, now=8),
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
    ),
    case(
        "base-becomes-virtual",
        "struct A {}; struct S : A {};",
        "struct A {}; struct S : virtual A {};",
        changed("S", naming="A"),
    ),
    case(
        "constexpr-removed",
        "constexpr int f() { return 1; }",
        "int f() { return 1; }",
        changed("f"),
    ),
    case(
        "class-becomes-final",
        "struct S { virtual void f(); };",
        "struct S final { virtual void f(); };",
        changed("S"),
    ),
    case(
        "nodiscard-added",
        "int f();",
        "[[nodiscard]] int f();",
        changed("f"),
    ),
    case(
        "deprecated-added",
        "void f();",
        "[[deprecated]] void f();",
        changed("f"),
    ),
    case(
        "static-assert-removed",
        "struct S { int x; };\nstatic_assert(sizeof(S) == 4);",
        "struct S { int x; };",
        removed_at(2),
    ),
    # Changes that arrived without their cause on 2026-10-10. Each has a second row for what
    # the tool already gets right, so that does not regress while the first is being fixed.
    case(
        "alias-target-changed",
        "using T = int;\nvoid f(T) {}\nvoid g() { f(1); }",
        "using T = long;\nvoid f(T) {}\nvoid g() { f(1); }",
        changed("T", was="int", now="long"),
        not_replaced("f"),
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
        changed("lib::v2", was="v1", now="v2"),
        not_replaced("f"),
        some_event(),
    ),
    case(
        "inline-namespace-bumped-is-reported",
        "namespace lib { inline namespace v1 { void f() {} } }",
        "namespace lib { inline namespace v2 { void f() {} } }",
        some_event(),
    ),
    # An inline namespace bump is one rename of the namespace, not a removal and an addition
    # of everything in it (ADR-044). Only an inline namespace, only one for one, and only
    # where something inside pairs through it.
    case(
        "inline-namespace-bumped-and-signature-changed",
        "namespace lib { inline namespace v1 { void f(int) {} void g() {} } }",
        "namespace lib { inline namespace v2 { void f(long) {} void g() {} } }",
        changed("lib::v2::f", was="void (int)", now="void (long)"),
        not_replaced("f"),
    ),
    case(
        "inline-namespace-bumped-type-in-signature",
        "namespace lib { inline namespace v1 { struct S {}; void f(S) {} } }",
        "namespace lib { inline namespace v2 { struct S {}; void f(S) {} } }",
        untouched("lib::v2::f"),
        untouched("lib::v2::S"),
        not_replaced("f"),
    ),
    # fmt's shape: a template argument names a template in the bumped namespace, and the USR
    # spells it after `>`. Overloaded, so the name tier cannot rescue what the rewrite misses.
    case(
        "inline-namespace-bumped-template-argument",
        "namespace lib { inline namespace v1 { template <class C> struct view {};\n"
        "template <class C> void show(view<C>) {}\n"
        "template <class C> void show(view<C>, int) {} } }",
        "namespace lib { inline namespace v2 { template <class C> struct view {};\n"
        "template <class C> void show(view<C>) {}\n"
        "template <class C> void show(view<C>, int) {} } }",
        untouched("lib::v2::show"),
        not_replaced("show"),
    ),
    case(
        "inline-namespace-bumped-class-template-specialization",
        "namespace lib { inline namespace v1 { template <class C> struct view {};\n"
        "template <class T> struct fmt {};\n"
        "template <> struct fmt<view<int>> { void go() {} }; } }",
        "namespace lib { inline namespace v2 { template <class C> struct view {};\n"
        "template <class T> struct fmt {};\n"
        "template <> struct fmt<view<int>> { void go() {} }; } }",
        untouched("lib::v2::fmt"),
        untouched("lib::v2::fmt::go"),
        not_replaced("fmt"),
        not_replaced("go"),
    ),
    case(
        "inline-namespace-bumped-nested-namespace",
        "namespace lib { inline namespace v1 { namespace detail { void g() {} } } }",
        "namespace lib { inline namespace v2 { namespace detail { void g() {} } } }",
        untouched("lib::v2::detail"),
        untouched("lib::v2::detail::g"),
        not_replaced("g"),
    ),
    case(
        "inline-namespace-bumped-at-global-scope",
        "inline namespace v1 { void f() {} }",
        "inline namespace v2 { void f() {} }",
        changed("v2"),
        not_replaced("f"),
    ),
    case(
        "inline-namespace-bumped-leaves-a-namesake-alone",
        "namespace lib { inline namespace v1 { void f() {} } }\n"
        "namespace other { namespace v1 { void g() {} } }",
        "namespace lib { inline namespace v2 { void f() {} } }\n"
        "namespace other { namespace v1 { void g() {} } }",
        untouched("other::v1"),
        untouched("other::v1::g"),
        not_replaced("f"),
    ),
    case(
        "plain-namespace-renamed-does-not-pair",
        "namespace lib { namespace v1 { void f() {} } }",
        "namespace lib { namespace v2 { void f() {} } }",
        removed("lib::v1::f"),
        added("lib::v2::f"),
    ),
    case(
        "inline-namespace-split-in-two-stays-unpaired",
        "namespace lib { inline namespace v1 { void f() {} void g() {} } }",
        "namespace lib { inline namespace v2 { void f() {} } inline namespace v3 { void g() {} } }",
        refused("v1"),
        refused("v2"),
        refused("v3"),
    ),
    case(
        "inline-namespace-kept-for-abi-and-new-one-added",
        "namespace lib { inline namespace v1 { void f() {} } }",
        "namespace lib { namespace v1 { void f() {} } inline namespace v2 { void f() {} } }",
        changed("lib::v1", was=True, now=False),
        added("lib::v2"),
        untouched("lib::v1::f"),
    ),
    case(
        "unrelated-inline-namespaces-do-not-pair",
        "namespace lib { inline namespace v12 { void a() {} } }",
        "namespace lib { inline namespace literals { void b() {} } }",
        removed("lib::v12"),
        added("lib::literals"),
    ),
    case(
        "namespace-becomes-inline",
        "namespace lib { namespace v1 { void f() {} } }",
        "namespace lib { inline namespace v1 { void f() {} } }",
        changed("lib::v1", was=False, now=True),
        not_replaced("f"),
    ),
    case(
        "parameter-added",
        "void f(int) {}\nvoid g() { f(1); }",
        "void f(int, int = 0) {}\nvoid g() { f(1); }",
        changed("f"),
        not_replaced("f"),
    ),
    case(
        "parameter-added-f-is-reported",
        "void f(int) {}\nvoid g() { f(1); }",
        "void f(int, int = 0) {}\nvoid g() { f(1); }",
        about("f"),
    ),
    # A changed signature is one change event on the function, not a removal plus an addition
    # (ADR-035, ADR-036). Where several old and new functions share a name and none is
    # unchanged, which is which is a guess, and the tool leaves them unpaired and says so.
    case(
        "const-added",
        "struct S { int f(); };",
        "struct S { int f() const; };",
        changed("S::f", was="int ()", now="int () const"),
        not_replaced("f"),
    ),
    case(
        "return-type-changed",
        "int f() { return 0; }",
        "long f() { return 0; }",
        changed("f", was="int ()", now="long ()"),
        not_replaced("f"),
    ),
    case(
        "class-template-constructor-gains-a-parameter",
        "template <class T> struct S { S(int) {} };",
        "template <class T> struct S { S(int, int) {} };",
        changed("S::S<T>", was="void (int)", now="void (int, int)"),
        not_replaced("S<T>"),
    ),
    case(
        "template-parameter-added",
        "template <class T> void f(T) {}",
        "template <class T, class U = int> void f(T) {}",
        changed("f", was="<class>", now="<class, class>"),
        not_replaced("f"),
    ),
    case(
        "overload-set-replaced-stays-unpaired",
        "void f(int) {}\nvoid f(double) {}",
        "void f(long) {}\nvoid f(char) {}",
        refused("f"),
    ),
    case(
        "one-of-two-overloads-changed",
        "void f(int) {}\nvoid f(double) {}",
        "void f(int) {}\nvoid f(long) {}",
        changed("f", was="void (double)", now="void (long)"),
        not_replaced("f"),
    ),
    case(
        "static-in-a-renamed-file-changes-parameters",
        "static int helper(int x) { return x; }\nint use() { return helper(1); }",
        "static int helper(int x, int y = 0) { return x + y; }\nint use() { return helper(1); }",
        changed("helper", was="int (int)", now="int (int, int)"),
        not_replaced("helper"),
        renamed_to="b.cpp",
    ),
    # Each call into a changed signature that existed before is flagged, saying whether the
    # caller was edited (ADR-040). A new call was written against the new signature.
    case(
        "unedited-caller-of-changed-signature",
        "void f(int) {}\nvoid g() { f(1); }",
        "void f(double) {}\nvoid g() { f(1); }",
        callers_flagged(("g", "f", False)),
    ),
    case(
        "edited-caller-of-changed-signature",
        "void f(int) {}\nvoid g() { f(1); }",
        "void f(double) {}\nvoid g() { f(2); }",
        callers_flagged(("g", "f", True)),
    ),
    case(
        "new-caller-of-changed-signature",
        "void f(int) {}\nvoid g() {}",
        "void f(double) {}\nvoid g() { f(1); }\nvoid h() { f(1); }",
        callers_flagged(),
    ),
    case(
        "caller-of-two-changed-signatures",
        "void f(int) {}\nvoid h(int) {}\nvoid g() { f(1); h(1); }",
        "void f(double) {}\nvoid h(long) {}\nvoid g() { f(1); h(1); }",
        callers_flagged(("g", "f", False), ("g", "h", False)),
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
    ),
    # From the relationship probe (model-probes/tmpl.py): a namespace-scope initializer.
    case(
        "global-initializer-calls-another",
        "int g();\nint h();\nint x = g();",
        "int g();\nint h();\nint x = h();",
        relation("x", "h"),
        fails="step 8 (out of v0, ADR-030): calls from initializers are not relationships",
    ),
]


@pytest.mark.parametrize(("before", "after", "expect", "head_file"), CATALOG)
def test_change_catalog(
    toolchain: Toolchain,
    tmp_path: Path,
    before: str,
    after: str,
    expect: tuple[Expect, ...],
    head_file: str,
) -> None:
    base = build_model(toolchain, tmp_path / "base", before, "a" * 40)
    head = build_model(toolchain, tmp_path / "head", after, "b" * 40, file=head_file)
    renames = {"a.cpp": head_file} if head_file != "a.cpp" else None
    corr = match_models(base, head, renames=renames)
    # Diff, then derive: the derived change events are part of what the reviewer gets.
    events = derive_events(diff_models(base, head, corr), base, head, corr)
    outcome = Outcome(base, head, events)
    unmet = [e.says for e in expect if not e.holds(outcome)]
    assert not unmet, f"expected {'; '.join(unmet)}. Got:\n{outcome.describe()}"


def unmarked(row: Any) -> object:
    """A catalog row without its expected-failure mark."""
    return pytest.param(*row.values, id=row.id)


@pytest.mark.parametrize(
    ("before", "after", "expect", "head_file"), [unmarked(row) for row in CATALOG]
)
def test_no_change_in_the_catalog_is_silent(
    toolchain: Toolchain,
    tmp_path: Path,
    before: str,
    after: str,
    expect: tuple[Expect, ...],
    head_file: str,
) -> None:
    """Whatever the model makes of a change, a change the tokens show is never reported as
    nothing: it produces change events, or it is counted as uncovered or moved (ADR-042).
    Every case, the expected failures included -- this is what holds while they are fixed."""
    base = build_model(toolchain, tmp_path / "base", before, "a" * 40)
    head = build_model(toolchain, tmp_path / "head", after, "b" * 40, file=head_file)
    renames = {"a.cpp": head_file} if head_file != "a.cpp" else None
    events = diff_models(base, head, match_models(base, head, renames=renames))
    hunk = LineHunk(1, before.count("\n") + 1, 1, after.count("\n") + 1)
    delta = TokenDelta(runs=tuple(changed_runs("a.cpp", before, head_file, after, [hunk])))
    coverage = measure_coverage(delta, events)
    if delta.runs:
        assert events or coverage.uncovered_tokens or coverage.moved_tokens
    # Every changed token is accounted for exactly once.
    assert coverage.changed_tokens == sum(len(run.tokens) for run in delta.runs)
    assert (
        coverage.explained_tokens + coverage.uncovered_tokens + coverage.moved_tokens
        == coverage.changed_tokens
    )


#: The step 7 cases in v0 (ADR-041). Each is one change to one entity, so it must be one
#: top-level change event: anything more is a cascade with no cause rule (ADR-023).
STEP_7_CASES = (
    "default-argument-value",
    "default-member-initializer",
    "constexpr-removed",
    "class-becomes-final",
    "nodiscard-added",
    "deprecated-added",
    "constructor-initializer-list",
    "base-becomes-virtual",
    "base-order",
)


def sources(case_id: str) -> tuple[str, str]:
    """The before and after of the catalog row `case_id`."""
    for row in CATALOG:
        param = cast(Any, row)  # a `pytest.param`, whose type pytest does not export
        if param.id == case_id:
            return param.values[0], param.values[1]
    raise KeyError(case_id)


@pytest.mark.parametrize("case_id", STEP_7_CASES)
def test_a_step_7_change_is_one_top_level_event(
    toolchain: Toolchain, tmp_path: Path, case_id: str
) -> None:
    before, after = sources(case_id)
    base = build_model(toolchain, tmp_path / "base", before, "a" * 40)
    head = build_model(toolchain, tmp_path / "head", after, "b" * 40)
    corr = match_models(base, head)
    events = derive_events(diff_models(base, head, corr), base, head, corr)
    events = classify_events(events, base, head, corr)
    top = [e for e in events if e.caused_by is None]
    assert len(top) == 1, Outcome(base, head, events).describe()


# -- The vocabulary discriminates -------------------------------------------------------------
#
# An expected failure that fails for the wrong reason, such as a helper that can never hold,
# would sit in the catalog looking like a gap in the tool. So the helpers are checked against
# hand-built events: the event a fixed tool would plausibly emit, and the near misses.


def hand_built(nodes: list[Node], edges: list[Edge], *events: ChangeEvent) -> Outcome:
    def side(rev: str) -> Model:
        model = Model(rev=rev * 40, edges=edges)
        for node in nodes:
            model.add_node(node)
        return model

    return Outcome(side("a"), side("b"), list(events))


def event(kind: ChangeEventKind, subject: AnyId, **payload: AttrValue) -> ChangeEvent:
    return ChangeEvent(
        kind=kind,
        subject=subject,
        payload=payload,
        confidence=Confidence(tier=None, unpaired=Unpaired.NONE, models_complete=True),
    )


def entity(kind: NodeKind, qualified: str) -> Node:
    name = qualified.split("::")[-1]
    return Node(id=NodeId(f"c:{qualified}"), kind=kind, name=name, qualified_name=qualified)


def test_an_enumerator_value_change_is_told_apart_from_its_near_misses() -> None:
    a, b = entity(NodeKind.ENUMERATOR, "E::A"), entity(NodeKind.ENUMERATOR, "E::B")
    expect = changed("E::B", was=2, now=3)

    def holds(*events: ChangeEvent) -> bool:
        return expect.holds(hand_built([a, b], [], *events))

    assert holds(event(ChangeEventKind.NODE_ATTR_CHANGED, b.id, attr="value", old=2, new=3))
    assert not holds(event(ChangeEventKind.NODE_ATTR_CHANGED, b.id, attr="value", old=2, new=4))
    assert not holds(event(ChangeEventKind.NODE_ATTR_CHANGED, a.id, attr="value", old=2, new=3))
    assert not holds(
        event(ChangeEventKind.NODE_REMOVED, b.id), event(ChangeEventKind.NODE_ADDED, b.id)
    )
    assert not holds()


def test_a_replaced_callback_needs_the_relationship_not_only_the_body_change() -> None:
    s, cb, cb2 = (entity(NodeKind.FUNCTION, name) for name in ("s", "cb", "cb2"))
    uses = [Edge(src=s.id, dst=f.id, kind=EdgeKind.USES_IN_IMPLEMENTATION) for f in (cb, cb2)]
    fixed = hand_built(
        [s, cb, cb2],
        uses,
        event(ChangeEventKind.EDGE_REMOVED, uses[0].id),
        event(ChangeEventKind.EDGE_ADDED, uses[1].id),
    )
    today = hand_built(
        [s, cb, cb2],
        [],
        event(ChangeEventKind.NODE_ADDED, cb2.id),
        event(ChangeEventKind.NODE_BODY_CHANGED, s.id),
    )
    for expect in (relation("s", "cb"), relation("s", "cb2")):
        assert expect.holds(fixed)
        assert not expect.holds(today)
    assert not_replaced("cb").holds(fixed)
    assert not not_replaced("s").holds(
        hand_built(
            [s],
            [],
            event(ChangeEventKind.NODE_REMOVED, s.id),
            event(ChangeEventKind.NODE_ADDED, s.id),
        )
    )


def test_a_refused_pairing_is_told_apart_from_a_plain_replacement_and_a_change() -> None:
    old, new = entity(NodeKind.FUNCTION, "f"), entity(NodeKind.FUNCTION, "g")
    expect = refused("f")

    def ambiguous(kind: ChangeEventKind, subject: AnyId) -> ChangeEvent:
        return ChangeEvent(
            kind=kind,
            subject=subject,
            confidence=Confidence(tier=None, unpaired=Unpaired.AMBIGUOUS, models_complete=True),
        )

    assert expect.holds(
        hand_built(
            [old],
            [],
            ambiguous(ChangeEventKind.NODE_REMOVED, old.id),
            ambiguous(ChangeEventKind.NODE_ADDED, old.id),
        )
    )
    assert not expect.holds(
        hand_built(
            [old],
            [],
            event(ChangeEventKind.NODE_REMOVED, old.id),
            event(ChangeEventKind.NODE_ADDED, old.id),
        )
    )
    assert not expect.holds(
        hand_built(
            [old],
            [],
            event(
                ChangeEventKind.NODE_SIGNATURE_CHANGED, old.id, old_signature="a", new_signature="b"
            ),
        )
    )
    assert not expect.holds(
        hand_built([old, new], [], ambiguous(ChangeEventKind.NODE_ADDED, new.id))
    )


def test_a_flagged_caller_is_told_apart_from_its_near_misses() -> None:
    f, g, h = (entity(NodeKind.FUNCTION, name) for name in ("f", "g", "h"))
    call = Edge(src=g.id, dst=f.id, kind=EdgeKind.CALLS)

    def flagged(caller: Node, edited: bool) -> ChangeEvent:
        return event(
            ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE,
            call.id,
            caller=caller.id,
            callee=f.id,
            old_signature="void (int)",
            new_signature="void (double)",
            caller_edited=edited,
        )

    def holds(expect: Expect, *events: ChangeEvent) -> bool:
        return expect.holds(hand_built([f, g, h], [call], *events))

    assert holds(callers_flagged(("g", "f", False)), flagged(g, False))
    assert not holds(callers_flagged(("g", "f", False)), flagged(g, True))
    assert not holds(callers_flagged(("g", "f", False)), flagged(h, False))
    assert not holds(callers_flagged(("g", "f", False)), flagged(g, False), flagged(h, False))
    assert not holds(callers_flagged(("g", "f", False)))
    assert holds(callers_flagged(), event(ChangeEventKind.EDGE_ADDED, call.id))
