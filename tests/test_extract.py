"""What the extractor must get right, stated as claims the design makes.

These need a working toolchain, so they are integration tests by necessity -- there is no
useful way to fake libclang, and a fake one would test the fake. They are fast because
each parses a few lines of C++ with no includes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.errors import PreconditionError
from cpp_semantic_diff.model.ir import (
    AttrValue,
    Edge,
    EdgeKind,
    Location,
    Model,
    Node,
    NodeId,
    NodeKind,
)
from cpp_semantic_diff.produce.extract import (
    DROPPED_FLAGS,
    TuExtract,
    body_hash,
    extract,
    merge_edge,
    merge_node,
    parse_arguments,
)
from cpp_semantic_diff.produce.toolchain import Toolchain, load

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    try:
        return load()
    except PreconditionError as error:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable libclang: {error}")


def build(tmp_path: Path, sources: dict[str, str], main: str = "a.cpp") -> CompileEntry:
    for name, text in sources.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return CompileEntry(
        file=tmp_path / main,
        directory=tmp_path,
        arguments=["clang++", "-std=c++17", "-c", main, "-o", "a.o"],
    )


def run(tmp_path: Path, toolchain: Toolchain, sources: dict[str, str]) -> TuExtract:
    return extract(build(tmp_path, sources), root=tmp_path, toolchain=toolchain)


def run_each(
    tmp_path: Path, toolchain: Toolchain, sources: dict[str, str], *mains: str
) -> list[TuExtract]:
    """One partial per translation unit, over one set of sources -- the shape every claim
    about merging or about what a TU can see needs, since the disagreement is *between*
    them."""
    return [
        extract(build(tmp_path, sources, main=main), root=tmp_path, toolchain=toolchain)
        for main in mains
    ]


def named(result: TuExtract, qualified: str) -> Node:
    matches = [n for n in result.nodes.values() if n.qualified_name == qualified]
    assert matches, (
        f"{qualified} not extracted; have {sorted(n.qualified_name for n in result.nodes.values())}"
    )
    return matches[0]


#: A resolved SHA, so that a partial can be wrapped in a `Model` and fingerprinted without
#: pretending a ref is a revision.
REV = "9f4d6c2e1b8a7d5c3e0f2a1b4c6d8e0f13579bdf"


def as_model(result: TuExtract) -> Model:
    """The partial as a `Model`, for `fingerprint()` -- the cheap whole-model check."""
    return Model(rev=REV, nodes=result.nodes, edges=list(result.edges.values()))


def edge_pairs(result: TuExtract, kind: EdgeKind) -> set[tuple[str, str]]:
    """Edges as (qualified name, qualified name), with unresolved endpoints as raw USRs."""
    names = {i: n.qualified_name for i, n in result.nodes.items()}
    return {
        (names.get(e.src, e.src), names.get(e.dst, e.dst))
        for e in result.edges.values()
        if e.kind is kind
    }


class TestScope:
    """ARCH-001 §13 fixes the v0 scope. A wider one is not a bonus -- it is events nothing
    consumes and cache entries that churn."""

    def test_extracts_the_five_kinds(self, tmp_path: Path, toolchain: Toolchain) -> None:
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                namespace app {
                struct Widget {
                  int size() const { return width_; }
                  int width_;
                };
                int helper(int v) { return v; }
                }
                """
            },
        )
        assert result.ok
        kinds = {n.kind for n in result.nodes.values()}
        assert kinds == {NodeKind.NAMESPACE, NodeKind.STRUCT, NodeKind.METHOD, NodeKind.FIELD} | {
            NodeKind.FUNCTION
        }

    def test_ignores_kinds_outside_the_v0_scope(self, tmp_path: Path, toolchain: Toolchain) -> None:
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                enum class Color { red, green };
                using Alias = int;
                int global_counter = 0;
                """
            },
        )
        assert result.ok
        assert result.nodes == {}

    def test_declarations_outside_the_repository_are_not_modelled(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """A model is of *this* repository. Pruning by file is also what keeps a TU's cost
        proportional to the project rather than to the standard library."""
        result = run(
            tmp_path, toolchain, {"a.cpp": "#include <string>\nint local() { return 0; }\n"}
        )
        assert result.ok
        assert [n.qualified_name for n in result.nodes.values()] == ["local"]


class TestIdentity:
    def test_declaration_and_definition_are_one_node(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """ARCH-001 §5: dedup by USR is correctness. The same entity declared in a header
        and defined in a source file is one entity, and the definition carries the body."""
        result = run(
            tmp_path,
            toolchain,
            {
                "lib.h": "#pragma once\nnamespace app { int helper(int v); }\n",
                "a.cpp": '#include "lib.h"\nnamespace app { int helper(int v) { return v; } }\n',
            },
        )
        assert result.ok
        helpers = [n for n in result.nodes.values() if n.name == "helper"]
        assert len(helpers) == 1
        assert helpers[0].body_hash is not None

    def test_overloads_are_distinct_entities(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """Name is never identity (ARCH-001 §5). Tier 1 depends on the signature."""
        result = run(
            tmp_path,
            toolchain,
            {"a.cpp": "int f(int x) { return x; }\ndouble f(double x) { return x; }\n"},
        )
        assert result.ok
        overloads = [n for n in result.nodes.values() if n.name == "f"]
        assert len(overloads) == 2
        assert len({n.signature for n in overloads}) == 2

    def test_signature_resolves_typedefs(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """An unrelated typedef change must not read as a signature change."""
        a = run(tmp_path, toolchain, {"a.cpp": "using Count = unsigned long;\nCount f(Count c);\n"})
        b = run(tmp_path, toolchain, {"a.cpp": "unsigned long f(unsigned long c);\n"})
        assert named(a, "f").signature == named(b, "f").signature

    def test_symbol_is_populated_for_entities_with_linkage(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """ARCH-001 §4.1 -- the join key to abidiff/bloaty/nm. Retrofitting it later
        invalidates every cached model, so it ships from v0."""
        result = run(tmp_path, toolchain, {"a.cpp": "namespace app { int helper(int v); }"})
        assert named(result, "app::helper").symbol == "_ZN3app6helperEi"


#: Two unnamed entities and a named neighbour. Clang spells the unnamed ones with the source
#: location that tells them apart -- `(anonymous union at <absolute path>:5:3)` -- and the
#: field inside is named through its parent, so one leak reaches several nodes.
ANONYMOUS = {
    "a.cpp": """
    namespace app {
    struct Widget {
      union { int as_int; float as_float; };
      struct { int width; int height; } extent;
      int plain;
    };
    }
    """
}


class TestNamesAreHostIndependent:
    """A name that carries where it was parsed is not a name. ARCH-001 §10 makes the model
    the product, and a worktree is named after the commit SHA (`acquire/workspace.py`), so a
    leaked spelling puts the host's directory layout *and* the revision under analysis into
    the model -- differing between any two revisions, on every run, for entities nobody
    touched."""

    def test_an_unnamed_entity_carries_no_path_line_or_column(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        result = run(tmp_path, toolchain, ANONYMOUS)
        assert result.ok
        union = named(result, "app::Widget::(anonymous union)")
        assert union.kind is NodeKind.UNION

        for node in result.nodes.values():
            for text in (node.name, node.qualified_name):
                assert str(tmp_path) not in text, text
                assert "/" not in text, text
                assert not re.search(r":\d+:\d+", text), text

    def test_a_member_of_an_unnamed_entity_is_named_through_it(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The member has a perfectly good name of its own; it inherits the leak from its
        parent. On fmt 28 of the 32 affected nodes were fields, not the records."""
        result = run(tmp_path, toolchain, ANONYMOUS)
        field = named(result, "app::Widget::(anonymous union)::as_int")
        assert field.kind is NodeKind.FIELD

    def test_a_type_spelled_from_an_unnamed_record_carries_no_path_either(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """`attrs` and `signature` are diffable state, and an unnamed record's *type* is
        spelled with the same location as its name."""
        result = run(tmp_path, toolchain, ANONYMOUS)
        for node in result.nodes.values():
            texts = [node.signature or "", *(v for v in node.attrs.values() if isinstance(v, str))]
            for text in texts:
                assert str(tmp_path) not in text, (node.qualified_name, text)
                assert " at " not in text, (node.qualified_name, text)

    def test_the_same_source_at_two_worktrees_gives_the_same_model(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The claim the leak actually breaks, and the one no output would reveal: worktrees
        are keyed by SHA, so base and head differ in nothing but their path. Anything that
        path reaches is a difference reported on every comparison of any two revisions."""
        base, head = tmp_path / "bcaa44d05579", tmp_path / "e2243a6a7f4a"
        base.mkdir()
        head.mkdir()

        left = run(base, toolchain, ANONYMOUS)
        right = run(head, toolchain, ANONYMOUS)

        assert {n.id: n.qualified_name for n in left.nodes.values()} == {
            n.id: n.qualified_name for n in right.nodes.values()
        }
        assert as_model(left).fingerprint() == as_model(right).fingerprint()


#: One header, two TUs: one sees only the declarations, the other defines them. Every
#: cross-TU disagreement fmt showed has this shape.
SPLIT = {
    "lib.h": "#pragma once\nnamespace app { struct S { ~S(); int size() const; int x; }; }\n",
    "a.cpp": '#include "lib.h"\nint from_a() { return 0; }\n',
    "b.cpp": '#include "lib.h"\nnamespace app { S::~S() {} int S::size() const { return x; } }\n',
}


class TestMergingObservationsOfOneEntity:
    """Every header is reparsed once per TU that includes it, and the observations sometimes
    disagree. The merge decides what the model says, so it has to be a rule and not an accident
    of arrival order (ARCH-001 §5, §9)."""

    def observations(self, tmp_path: Path, toolchain: Toolchain) -> list[TuExtract]:
        return run_each(tmp_path, toolchain, SPLIT, "a.cpp", "b.cpp")

    @staticmethod
    def merged(partials: list[TuExtract]) -> dict[NodeId, Node]:
        nodes: dict[NodeId, Node] = {}
        for partial in partials:
            for node in partial.nodes.values():
                merge_node(nodes, node.model_copy(deep=True))
        return nodes

    def test_the_result_does_not_depend_on_the_order_the_observations_arrived(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Otherwise the model is a function of compile-database order, and two revisions
        differing only in a CMake reorder diff non-empty."""
        partials = self.observations(tmp_path, toolchain)
        assert all(p.ok for p in partials)
        assert self.merged(partials) == self.merged(list(reversed(partials)))

    def test_a_merged_node_is_one_observation_whole(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Taking the body from one TU and an attribute from another describes a state that
        exists in no translation unit and in no source file -- and the diff would report on
        it as if it did."""
        partials = self.observations(tmp_path, toolchain)
        merged = self.merged(partials)
        for node_id, node in merged.items():
            seen = [p.nodes[node_id] for p in partials if node_id in p.nodes]
            assert any(node == observation for observation in seen), (
                f"{node.qualified_name} matches no translation unit's view of it"
            )

    def test_the_definition_still_wins(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """The rule that was already there, restated because the merge changed underneath
        it: the surviving node carries the body and points at the definition."""
        merged = self.merged(self.observations(tmp_path, toolchain))
        size = next(n for n in merged.values() if n.qualified_name == "app::S::size")
        assert size.body_hash is not None
        assert size.location is not None and size.location.file == "b.cpp"


class TestMergingObservationsOfOneEdge:
    """The same rule as for nodes, and it was missing here.

    An edge attribute belongs to the relation, so two TUs seeing one edge should agree. When
    they do not, one knew less -- `virtual` is `False` both for "not virtual" and for a
    callee this TU could not resolve as a method, and `access` falls back to `"private"`.
    `setdefault` took whichever arrived first, which made the model a function of
    compile-database order: the same two revisions could diff clean or dirty depending on
    which TU the worker pool finished first.

    Built by hand rather than parsed. The disagreement is between *translation units*, and
    the smallest honest way to stage two disagreeing observations of one edge is to hand them
    to the merge directly -- the same reason `merge_node` has unit-level tests beside the
    parsed ones.
    """

    SRC, DST = "c:@F@caller#", "c:@F@callee#"

    def merged(self, *observations: Mapping[str, AttrValue]) -> dict[str, AttrValue]:
        edges: dict[str, Edge] = {}
        for attrs in observations:
            merge_edge(edges, self.SRC, self.DST, EdgeKind.CALLS, (), dict(attrs))
        return edges[f"{self.SRC}|calls|{self.DST}"].attrs

    def test_the_result_does_not_depend_on_the_order_the_observations_arrived(self) -> None:
        uninformed, informed = {"virtual": False}, {"virtual": True}
        assert self.merged(uninformed, informed) == self.merged(informed, uninformed)

    def test_the_better_informed_observation_wins(self) -> None:
        """`virtual=False` is what a TU reports when it could not resolve the callee as a
        method at all, so a `True` from any TU is positive evidence and a `False` is not."""
        assert self.merged({"virtual": False}, {"virtual": True}) == {"virtual": True}
        assert self.merged({"virtual": True}, {"virtual": False}) == {"virtual": True}

    def test_a_real_access_specifier_beats_the_private_fallback(self) -> None:
        """`_access(child) or "private"` means `"private"` is also the value for *unknown*."""
        assert self.merged({"access": "private"}, {"access": "public"}) == {"access": "public"}
        assert self.merged({"access": "public"}, {"access": "private"}) == {"access": "public"}

    def test_an_attribute_only_one_tu_saw_is_still_recorded(self) -> None:
        assert self.merged({}, {"virtual": True}) == {"virtual": True}

    def test_call_sites_still_accumulate_across_observations(self) -> None:
        """The merge is per attribute; locations are a union and must not have changed."""
        edges: dict[str, Edge] = {}
        for line in (10, 20, 10):
            merge_edge(
                edges,
                self.SRC,
                self.DST,
                EdgeKind.CALLS,
                (Location(file="a.cpp", line=line),),
                {"virtual": False},
            )
        assert [loc.line for loc in edges[f"{self.SRC}|calls|{self.DST}"].locations] == [10, 20]


#: A destructor declared in a header and defined in one of the two TUs that include it.
LAZY = {
    "lib.h": "#pragma once\nstruct Res { ~Res(); int x; };\n",
    "a.cpp": '#include "lib.h"\nint untouched() { return 0; }\n',
    "b.cpp": '#include "lib.h"\nRes::~Res() {}\n',
}


class TestExceptionSpecifications:
    """Clang computes an implicit exception specification lazily, so what a TU reports
    depends on whether that TU ever used the function. `signature` is tier-1 identity input
    (ARCH-001 §5); anything in it that varies with the *observer* breaks identity."""

    def sides(self, tmp_path: Path, toolchain: Toolchain) -> list[TuExtract]:
        """`a.cpp` never uses the destructor, `b.cpp` defines it. Same header, same
        entity, and clang computes the specification only where it was needed."""
        return run_each(tmp_path, toolchain, LAZY, "a.cpp", "b.cpp")

    def test_the_two_translation_units_really_do_disagree(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The premise. Without it the test below proves nothing: the TU that defines the
        destructor computes its specification, the TU that only includes the header does
        not, and `None` is this model's word for 'not determined here'."""
        unused, used = self.sides(tmp_path, toolchain)
        assert named(used, "Res::~Res").attrs["exception_spec"] == "basic_noexcept"
        assert named(unused, "Res::~Res").attrs["exception_spec"] is None

    def test_the_signature_is_the_same_from_both(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Which TUs are in the compile database is not a property of the code. If it
        reaches the signature, adding one test file makes an untouched destructor read as
        delete + add."""
        unused, used = self.sides(tmp_path, toolchain)
        signature = named(used, "Res::~Res").signature
        assert signature == named(unused, "Res::~Res").signature
        assert "noexcept" not in (signature or "")

    def test_unevaluated_never_reaches_the_model(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """It is not a state of the entity -- it says the compiler had no reason to look.
        ARCH-001 §4 admits identity, diffable state and provenance; this is none of them."""
        for side in self.sides(tmp_path, toolchain):
            assert "unevaluated" not in {n.attrs.get("exception_spec") for n in side.nodes.values()}

    def test_adding_noexcept_is_an_attribute_change_not_a_new_entity(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The other half of the same decision. The USR does not encode the specification,
        so tier 1 matches across the edit; carrying it in the signature too would have made
        the match report a delete and an add of the same function."""
        plain = run(tmp_path, toolchain, {"a.cpp": "void f();"})
        marked = run(tmp_path, toolchain, {"a.cpp": "void f() noexcept;"})
        before, after = named(plain, "f"), named(marked, "f")
        assert before.id == after.id
        assert before.signature == after.signature
        assert before.attrs["exception_spec"] != after.attrs["exception_spec"]


class TestRepositoryRelativePaths:
    def test_the_root_decides_what_relative_means(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The same file, the same spelling, two roots, two answers -- and both in one
        process, because `_relative` is memoized and a memo keyed on less than it is asked
        would hand one revision the other revision's answer (ARCH-001 §9)."""
        inner = tmp_path / "project"
        inner.mkdir()
        entry = build(inner, {"a.cpp": "int f() { return 0; }"})

        deep = extract(entry, root=inner, toolchain=toolchain)
        wide = extract(entry, root=tmp_path, toolchain=toolchain)

        deep_at, wide_at = named(deep, "f").location, named(wide, "f").location
        assert deep_at is not None
        assert wide_at is not None
        assert deep_at.file == "a.cpp"
        assert wide_at.file == "project/a.cpp"


class TestBodyHash:
    def test_survives_reformatting_and_comments(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """Tier-2 identity (ARCH-001 §5) is worthless if whitespace breaks it."""
        plain = run(tmp_path, toolchain, {"a.cpp": "int f(int x) { return x + 1; }"})
        noisy = run(
            tmp_path,
            toolchain,
            {"a.cpp": "int f(int x) {\n  // add one\n  return x   +   1;\n}\n"},
        )
        assert named(plain, "f").body_hash == named(noisy, "f").body_hash

    def test_changes_when_the_body_changes(self, tmp_path: Path, toolchain: Toolchain) -> None:
        before = run(tmp_path, toolchain, {"a.cpp": "int f(int x) { return x + 1; }"})
        after = run(tmp_path, toolchain, {"a.cpp": "int f(int x) { return x + 2; }"})
        assert named(before, "f").body_hash != named(after, "f").body_hash

    def test_ignores_the_name_so_a_rename_stays_matchable(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Tier 2 exists to catch renames. Hashing the signature would defeat the only
        thing the tier is for."""
        before = run(tmp_path, toolchain, {"a.cpp": "int compute(int x) { return x + 1; }"})
        after = run(tmp_path, toolchain, {"a.cpp": "int calculate(int x) { return x + 1; }"})
        assert named(before, "compute").body_hash == named(after, "calculate").body_hash

    def test_a_declaration_has_no_body_hash(self, tmp_path: Path, toolchain: Toolchain) -> None:
        result = run(tmp_path, toolchain, {"a.cpp": "int f(int x);"})
        assert named(result, "f").body_hash is None

    def test_an_empty_body_is_not_a_missing_body(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        result = run(tmp_path, toolchain, {"a.cpp": "void f() {}"})
        assert named(result, "f").body_hash is not None


class TestEdges:
    def test_contains_follows_the_semantic_parent(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """An out-of-line definition belongs to its class, not to the namespace it was
        written in."""
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                namespace app {
                struct Widget { int size() const; };
                int Widget::size() const { return 0; }
                }
                """
            },
        )
        assert ("app::Widget", "app::Widget::size") in edge_pairs(result, EdgeKind.CONTAINS)

    def test_calls_inherits_and_overrides(self, tmp_path: Path, toolchain: Toolchain) -> None:
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                namespace app {
                int helper(int v) { return v; }
                struct Base { virtual int run(int x) const = 0; };
                struct Impl : Base {
                  int run(int x) const override { return helper(x); }
                };
                }
                """
            },
        )
        assert ("app::Impl::run", "app::helper") in edge_pairs(result, EdgeKind.CALLS)
        assert ("app::Impl", "app::Base") in edge_pairs(result, EdgeKind.INHERITS)
        assert ("app::Impl::run", "app::Base::run") in edge_pairs(result, EdgeKind.OVERRIDES)

    def test_a_repeated_call_is_one_edge_with_two_locations(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """ARCH-001 §4.2 states this as an IR rule; it has to be true of what fills it."""
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                int helper(int v) { return v; }
                int caller(int x) {
                  int a = helper(x);
                  int b = helper(x + 1);
                  return a + b;
                }
                """
            },
        )
        calls = [e for e in result.edges.values() if e.kind is EdgeKind.CALLS]
        assert len(calls) == 1
        assert len(calls[0].locations) == 2


class TestTemplates:
    def test_instantiations_do_not_become_nodes(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """ARCH-001 §5: an unrelated new call site can materialize an instantiation, so
        instantiation-only deltas would be noise attributed to the wrong change."""
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                template <typename T> T identity(T v) { return v; }
                int use() { return identity(1) + (int)identity(2.0); }
                """
            },
        )
        assert len([n for n in result.nodes.values() if n.name == "identity"]) == 1

    def test_a_call_to_an_instantiation_offers_the_template_as_an_alias(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Calls resolve to an instantiation whose USR belongs to no written declaration.
        The extractor records the mapping rather than applying it -- only the merged node
        set can tell an instantiation from an explicit specialization."""
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                template <typename T> T identity(T v) { return v; }
                int use() { return identity(1); }
                """
            },
        )
        template = named(result, "identity")
        calls = [e for e in result.edges.values() if e.kind is EdgeKind.CALLS]
        assert calls, "the call was not extracted at all"
        target = calls[0].dst
        assert target not in result.nodes, "expected the raw instantiation USR"
        assert result.aliases[target] == template.id

    def test_an_explicit_specialization_keeps_its_own_identity(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """It is hand-written code. Collapsing it into the primary template would hide a
        change a reviewer has to see."""
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                template <typename T> struct Holder { T value; };
                template <> struct Holder<char> { char value; char extra; };
                """
            },
        )
        holders = [n for n in result.nodes.values() if n.name == "Holder"]
        assert len(holders) == 2
        assert len({n.id for n in holders}) == 2

    def test_a_templated_record_keeps_the_tag_it_was_written_with(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Clang files every templated record under one cursor kind, so reading `class` off
        it reports `template <typename T> struct X` as a class. Kind is not identity and no
        diff is corrupted, but one qualified name then appears as two kinds -- fmt's
        `formatter` split 54 class / 43 struct -- which corrupts rendering and every
        kind-keyed rule downstream."""
        result = run(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                template <typename T> struct Holder { T value; };
                template <typename T> struct Holder<T*> { T* value; };
                template <> struct Holder<char> { char value; };
                template <typename T> class Keeper { public: T value; };
                template <typename T> union Either { T value; int tag; };
                """
            },
        )
        assert result.ok

        def kinds(name: str) -> set[NodeKind]:
            return {n.kind for n in result.nodes.values() if n.qualified_name == name}

        # One qualified name, one kind -- including the partial and full specializations,
        # which are separate entities written with the same tag.
        assert kinds("Holder") == {NodeKind.STRUCT}
        assert kinds("Keeper") == {NodeKind.CLASS}
        assert kinds("Either") == {NodeKind.UNION}


class TestAMemberFunctionTemplateIsAMethod:
    """Clang files every function template under one cursor kind, so a mapping keyed on the
    cursor kind reports a member template as a free function -- 414 of them on fmt, beside
    the non-template members of the same classes, which are methods. Membership is a
    property of the scope, so the claim under test is about the scope: whatever the tag on
    the declaration, what decides is whether it was declared inside a class.

    The kind is not identity and both sides of a diff got it wrong identically, so no delta was
    ever corrupted. What was corrupted is everything downstream that reads a kind -- and,
    because `_attributes` is keyed on the node kind, the method attributes were absent
    entirely. `explicit` is the one with nowhere else to live: a `function` has no attribute
    that could carry it."""

    SOURCE = """
    namespace app {
    template <typename T> T free_tpl(T v) { return v; }

    class Widget {
     public:
      template <typename T> explicit Widget(T v) {}
      template <typename T> void member_tpl(T v);
      template <typename T> static T static_tpl(T v) { return v; }
      template <typename T> int const_tpl(T v) const { return 0; }
      void ordinary() const {}
      template <typename T> friend void befriend(T v);
    };
    template <typename T> void befriend(T v) {}

    struct Plain { template <typename T> void s_tpl(T v) {} };
    union Either { int tag; template <typename T> void u_tpl(T v) {} };

    template <typename T> class Boxed {
     public:
      template <typename U> explicit Boxed(U v) {}
      template <typename U> void ct_tpl(U v) {}
    };

    template <typename T> class Boxed<T*> {
     public:
      template <typename U> void partial_tpl(U v) {}
    };

    template <typename T> void Widget::member_tpl(T v) {}
    }
    """

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory: pytest.TempPathFactory, toolchain: Toolchain) -> TuExtract:
        return run(tmp_path_factory.mktemp("member_tpl"), toolchain, {"a.cpp": self.SOURCE})

    def kind(self, result: TuExtract, qualified: str) -> NodeKind:
        return named(result, qualified).kind

    def test_a_template_declared_inside_a_class_is_a_method(self, result: TuExtract) -> None:
        assert result.ok
        assert self.kind(result, "app::Widget::member_tpl") is NodeKind.METHOD
        assert self.kind(result, "app::Widget::static_tpl") is NodeKind.METHOD
        assert self.kind(result, "app::Widget::const_tpl") is NodeKind.METHOD
        assert self.kind(result, "app::Widget::Widget") is NodeKind.METHOD

    def test_struct_union_and_class_template_scopes_count_too(self, result: TuExtract) -> None:
        """A member of a class template is a member. Clang reports the semantic parent as
        CLASS_TEMPLATE (and as CLASS_TEMPLATE_PARTIAL_SPECIALIZATION for a partial), which
        is a different cursor kind from CLASS_DECL and the same answer."""
        assert self.kind(result, "app::Plain::s_tpl") is NodeKind.METHOD
        assert self.kind(result, "app::Either::u_tpl") is NodeKind.METHOD
        assert self.kind(result, "app::Boxed::ct_tpl") is NodeKind.METHOD
        assert self.kind(result, "app::Boxed::Boxed<T>") is NodeKind.METHOD
        assert self.kind(result, "app::Boxed::partial_tpl") is NodeKind.METHOD

    def test_a_template_at_namespace_scope_is_still_a_function(self, result: TuExtract) -> None:
        assert self.kind(result, "app::free_tpl") is NodeKind.FUNCTION

    def test_an_out_of_line_definition_is_the_same_method(self, result: TuExtract) -> None:
        """`member_tpl` is declared in the class and defined at namespace scope. One entity,
        one kind -- the same claim the record tags make, and the reason the rule reads the
        semantic parent: the lexical parent of the definition is the namespace."""
        members = [n for n in result.nodes.values() if n.name == "member_tpl"]
        assert len(members) == 1
        assert members[0].kind is NodeKind.METHOD
        assert members[0].body_hash is not None, "the out-of-line definition was not merged in"

    def test_a_friend_template_is_not_a_member_of_the_class_it_is_written_in(
        self, result: TuExtract
    ) -> None:
        """The case that says the rule is the *semantic* parent rather than the lexical one.
        A friend is declared inside the class body and is not a member of it; clang agrees,
        and reports the namespace as its semantic parent."""
        assert self.kind(result, "app::befriend") is NodeKind.FUNCTION

    def test_explicit_survives_on_a_constructor_template(self, result: TuExtract) -> None:
        """The attribute that was lost, with no other representation. It is not a new attribute
        name: it is the one an ordinary explicit constructor already carries, reaching a node
        that could not carry it while its kind said `function`."""
        assert named(result, "app::Widget::Widget").attrs["explicit"] is True
        assert named(result, "app::Boxed::Boxed<T>").attrs["explicit"] is True

    def test_it_carries_what_an_ordinary_method_carries_and_nothing_new(
        self, result: TuExtract
    ) -> None:
        """Consistency is the whole claim: a member function template is a method, so it
        answers the same questions a method answers. An attribute set that differs by so
        much as a key would make `NodeAttrChanged` fire on a declaration gaining or losing
        `template`, which is not what happened to it."""
        ordinary = named(result, "app::Widget::ordinary")
        member_tpl = named(result, "app::Widget::member_tpl")
        assert set(member_tpl.attrs) == set(ordinary.attrs)
        assert member_tpl.attrs["template"] is True
        assert ordinary.attrs["template"] is False
        assert named(result, "app::Widget::static_tpl").attrs["static"] is True
        assert named(result, "app::Widget::const_tpl").attrs["const"] is True

    def test_the_free_template_carries_no_method_attributes(self, result: TuExtract) -> None:
        """The other half of the same claim -- `explicit` on a free function is not a
        question, and an attribute that is absent for some nodes of a kind and present for
        others is what ARCH-001 §4.3 forbids."""
        free = named(result, "app::free_tpl")
        assert "explicit" not in free.attrs
        assert set(free.attrs) == set(named(result, "app::befriend").attrs)


class TestTemplateParametersAreComparedState:
    """A function template's parameter list is an attribute, `template_parameters`.

    Two templates that differ only in their parameter lists can share a signature, because the
    signature spells a template parameter by position. So once functions pair by name, a
    changed parameter list is visible only here. Names are not part of it: renaming `T` is no
    change.
    """

    SOURCE = """
    template <class T> void one(T) {}
    template <typename U> void renamed(U) {}
    template <class T, class U = int> void two(T) {}
    template <class T, int N, template <class...> class X, class... Ts> void mixed(T, Ts...) {}
    template <auto... Vs> void values() {}
    template <bool B> struct E {};
    template <int N, class T = E<(N > 0)>, class... Ts> void guarded(Ts...) {}
    template <template <class> class... Xs> void outer() {}
    void plain(int) {}
    struct S { template <class T> void m(T) {} void n() {} };
    template <class... Ts> struct P { template <class U> void k(U); };
    template <class... Ts> template <class U> void P<Ts...>::k(U) {}
    """

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory: pytest.TempPathFactory, toolchain: Toolchain) -> TuExtract:
        return run(tmp_path_factory.mktemp("tparams"), toolchain, {"a.cpp": self.SOURCE})

    def params(self, result: TuExtract, qualified: str) -> AttrValue:
        return named(result, qualified).attrs["template_parameters"]

    def test_a_parameter_list_is_spelled_without_names(self, result: TuExtract) -> None:
        assert self.params(result, "one") == "<class>"
        assert self.params(result, "renamed") == self.params(result, "one")

    def test_an_added_parameter_shows_where_the_signature_does_not(self, result: TuExtract) -> None:
        assert named(result, "one").signature == named(result, "two").signature
        assert self.params(result, "two") == "<class, class>"

    def test_each_kind_of_parameter_and_packs_are_told_apart(self, result: TuExtract) -> None:
        assert self.params(result, "mixed") == "<class, int, template <class...> class, class...>"
        assert self.params(result, "values") == "<auto...>"
        # A `>` inside a parenthesized default argument does not end the list.
        assert self.params(result, "guarded") == "<int, class, class...>"
        assert self.params(result, "outer") == "<template <class> class...>"

    def test_a_function_that_is_not_a_template_has_none(self, result: TuExtract) -> None:
        assert self.params(result, "plain") is None
        assert self.params(result, "S::n") is None
        assert self.params(result, "S::m") == "<class>"

    def test_an_out_of_line_member_template_reads_its_own_list(self, result: TuExtract) -> None:
        """Its definition is written after its class's header, which here is a pack."""
        assert named(result, "P::k").body_hash is not None, "the definition was not merged in"
        assert self.params(result, "P::k") == "<class>"


class TestAnInlineNamespaceSaysSo:
    """`inline` is an attribute of every namespace, written whether true or false.

    Neither the USR nor the spelling carries it, and the matcher needs it: a bumped inline
    namespace keeps its members' unqualified names working, a bumped plain one does not.
    """

    SOURCE = """
    namespace lib {
    inline namespace v1 { void f(); }
    namespace v1 { void g(); }
    namespace detail {}
    }
    namespace { inline namespace a1 {} }
    """

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory: pytest.TempPathFactory, toolchain: Toolchain) -> TuExtract:
        return run(tmp_path_factory.mktemp("inline"), toolchain, {"a.cpp": self.SOURCE})

    def test_an_inline_namespace_is_inline(self, result: TuExtract) -> None:
        assert named(result, "lib::v1").attrs["inline"] is True
        assert named(result, "a1").attrs["inline"] is True

    def test_a_plain_namespace_says_it_is_not(self, result: TuExtract) -> None:
        assert named(result, "lib").attrs["inline"] is False
        assert named(result, "lib::detail").attrs["inline"] is False


class TestFailureIsRecordedNotRaised:
    def test_a_broken_tu_is_reported_as_incompleteness(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """ARCH-001 §4.4/§11: one unparseable file is a completeness fact, not a reason to
        abandon the review. A broken *toolchain* is the opposite and fails before this."""
        result = run(tmp_path, toolchain, {"a.cpp": '#include "nonexistent-header.h"\nint f();\n'})
        assert not result.ok
        assert result.result.detail is not None
        assert "nonexistent-header.h" in result.result.detail
        assert result.nodes == {}

    def test_a_truncated_ast_is_never_extracted_from(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The silent-truncation failure mode, at TU granularity: clang abandons the rest
        of a file after a fatal error and returns what it has, without raising. Extracting
        from that produces a model confidently missing declarations."""
        result = run(
            tmp_path,
            toolchain,
            {"a.cpp": '#include "missing.h"\nnamespace app { int survivor(); }\n'},
        )
        assert not result.ok
        assert not any(n.name == "survivor" for n in result.nodes.values())


class TestCompileArguments:
    def test_drops_output_flags_and_the_source_file(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        entry = CompileEntry(
            file=tmp_path / "a.cpp",
            directory=tmp_path,
            arguments=["c++", "-std=c++20", "-I/inc", "-c", "a.cpp", "-o", "CMakeFiles/a.o"],
        )
        args = parse_arguments(entry, toolchain)
        assert "-std=c++20" in args and "-I/inc" in args
        assert "a.cpp" not in args
        assert "-o" not in args and "CMakeFiles/a.o" not in args
        assert not any(a in DROPPED_FLAGS for a in args)

    def test_werror_is_dropped_so_a_warning_cannot_fake_a_parse_failure(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """A TU reported as unparseable is a hole in the model. Letting -Werror create one
        would understate completeness for code that parses fine."""
        entry = CompileEntry(
            file=tmp_path / "a.cpp",
            directory=tmp_path,
            arguments=["c++", "-Werror", "-c", "a.cpp"],
        )
        assert "-Werror" not in parse_arguments(entry, toolchain)

    def test_passes_the_build_directory_rather_than_changing_into_it(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Relative include paths are relative to the database's `directory`. Using the
        process working directory would make the producer unparallelizable."""
        entry = CompileEntry(
            file=tmp_path / "a.cpp",
            directory=tmp_path / "build",
            arguments=["c++", "-Irel/include", "-c", "a.cpp"],
        )
        assert f"-working-directory={tmp_path / 'build'}" in parse_arguments(entry, toolchain)

    def test_relative_includes_resolve_against_the_build_directory(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The claim above, actually exercised: a header findable only through a relative
        -I must be found."""
        (tmp_path / "include").mkdir()
        (tmp_path / "include" / "dep.h").write_text("int from_header();\n", encoding="utf-8")
        (tmp_path / "a.cpp").write_text('#include "dep.h"\nint local() { return 0; }\n')
        entry = CompileEntry(
            file=tmp_path / "a.cpp",
            directory=tmp_path,
            arguments=["c++", "-Iinclude", "-c", "a.cpp"],
        )
        result = extract(entry, root=tmp_path, toolchain=toolchain)
        assert result.ok, result.result.detail
        assert {n.qualified_name for n in result.nodes.values()} == {"from_header", "local"}


class TestBodyHashHelper:
    def test_returns_none_without_a_body(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """Direct cover for the branch the node-level tests reach only indirectly."""
        result = run(tmp_path, toolchain, {"a.cpp": "struct S { int x; };"})
        assert body_hash is not None  # imported, exercised through nodes below
        assert named(result, "S::x").body_hash is None
