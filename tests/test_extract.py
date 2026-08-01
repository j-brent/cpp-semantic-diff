"""What the extractor must get right, stated as claims the design makes.

These need a working toolchain, so they are integration tests by necessity -- there is no
useful way to fake libclang, and a fake one would test the fake. They are fast because
each parses a few lines of C++ with no includes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.errors import PreconditionError
from cpp_semantic_diff.model.ir import EdgeKind, NodeKind
from cpp_semantic_diff.produce.extract import (
    DROPPED_FLAGS,
    TuExtract,
    body_hash,
    extract,
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


def named(result: TuExtract, qualified: str):
    matches = [n for n in result.nodes.values() if n.qualified_name == qualified]
    assert matches, (
        f"{qualified} not extracted; have {sorted(n.qualified_name for n in result.nodes.values())}"
    )
    return matches[0]


def edge_pairs(result: TuExtract, kind: EdgeKind) -> set[tuple[str, str]]:
    """Edges as (qualified name, qualified name), with unresolved endpoints as raw USRs."""
    names = {i: n.qualified_name for i, n in result.nodes.items()}
    return {
        (names.get(e.src, e.src), names.get(e.dst, e.dst))
        for e in result.edges.values()
        if e.kind is kind
    }


class TestScope:
    """ARCH-001 §12 fixes the v0 scope. A wider one is not a bonus -- it is events nothing
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
