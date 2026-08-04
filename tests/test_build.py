"""What the builder must get right, stated as claims the design makes.

The claims worth testing here are the ones whose failure is invisible in the output. A
model with three copies of the same class looks exactly like a model with one until
something counts them; a model whose call edges point at instantiation USRs looks exactly
like a model with no call graph at all. Both are ARCH-001 §5 correctness requirements, not
tidiness.

Like `test_extract.py`, these are integration tests by necessity -- there is no useful way
to fake libclang -- and fast because each one compiles a handful of lines with no includes
beyond the project itself.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from cpp_semantic_diff.acquire.workspace import Checkout, CompileDatabase
from cpp_semantic_diff.errors import PreconditionError
from cpp_semantic_diff.model.ir import EdgeKind, Model, Node, TuStatus
from cpp_semantic_diff.produce.build import assemble, build_model, tu_extracts
from cpp_semantic_diff.produce.toolchain import Toolchain, load

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: A resolved commit SHA. `Model.rev` is a commit, never a ref (ARCH-001 §4), and a test
#: that passed "HEAD" here would not notice the difference.
REV = "9f4d6c2e1b8a7d5c3e0f2a1b4c6d8e0f13579bdf"


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    try:
        return load()
    except PreconditionError as error:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable libclang: {error}")


def project(tmp_path: Path, sources: dict[str, str], tus: Sequence[str]) -> Checkout:
    """A synthetic checkout: some files, and a compile database naming the TUs."""
    for name, text in sources.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    database = tmp_path / "compile_commands.json"
    database.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": tu,
                    "arguments": ["clang++", "-std=c++17", "-c", tu, "-o", f"{tu}.o"],
                }
                for tu in tus
            ]
        ),
        encoding="utf-8",
    )
    return Checkout(rev=REV, path=tmp_path, compile_db=CompileDatabase.load(database))


def build(tmp_path: Path, toolchain: Toolchain, sources: dict[str, str], *tus: str) -> Model:
    return build_model(project(tmp_path, sources, tus), toolchain=toolchain)


class TestTheLanguageModeIsRecorded:
    """A determinant of the AST that nothing else in `Completeness` captures.

    Raising `CMAKE_CXX_STANDARD` is one line of CMake and changes which declarations exist.
    Measured on leveldb `HEAD~9..~1` (11 → 17): 117 entities appear in vendored googletest
    that no source change explains, and in the delta they are indistinguishable from new
    code. Reported so the reviewer can reach the right conclusion (STATUS §5).
    """

    SOURCES: ClassVar[dict[str, str]] = {"a.cpp": "int a() { return 1; }\n"}

    def test_the_standard_the_build_asked_for_is_recorded(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        model = build(tmp_path, toolchain, self.SOURCES, "a.cpp")
        assert model.completeness.language_modes == ["c++17"]

    def test_a_build_that_states_no_standard_records_none(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """fmt's compile database carries no `-std` at all, so it is parsed at libclang's
        default rather than the project's. Empty is the honest answer, not a guess."""
        (tmp_path / "a.cpp").write_text(self.SOURCES["a.cpp"], encoding="utf-8")
        database = tmp_path / "compile_commands.json"
        database.write_text(
            json.dumps(
                [
                    {
                        "directory": str(tmp_path),
                        "file": "a.cpp",
                        "arguments": ["clang++", "-c", "a.cpp", "-o", "a.o"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        checkout = Checkout(rev=REV, path=tmp_path, compile_db=CompileDatabase.load(database))
        model = build_model(checkout, toolchain=toolchain, cache=False)
        assert model.completeness.language_modes == []

    def test_modes_are_deduplicated_and_sorted(self, toolchain: Toolchain) -> None:
        """One entry per distinct standard, not per TU -- a 900-TU build states one fact."""
        model = assemble(REV, [], toolchain=toolchain, language_modes=["c++17", "c++11", "c++17"])
        assert model.completeness.language_modes == ["c++11", "c++17"]


def named(model: Model, qualified: str) -> list[Node]:
    return [n for n in model.nodes.values() if n.qualified_name == qualified]


def one(model: Model, qualified: str) -> Node:
    matches = named(model, qualified)
    assert len(matches) == 1, (
        f"expected exactly one {qualified}, have {[n.id for n in matches]} "
        f"out of {sorted(n.qualified_name for n in model.nodes.values())}"
    )
    return matches[0]


def edge_pairs(model: Model, kind: EdgeKind) -> set[tuple[str, str]]:
    names = {i: n.qualified_name for i, n in model.nodes.items()}
    return {(names[e.src], names[e.dst]) for e in model.edges if e.kind is kind}


#: A header seen by every TU, and one source file that defines what it declares. This is
#: the shape the whole stage exists to handle: `shared.h` is parsed three times.
SHARED = {
    "shared.h": """
    #pragma once
    namespace app {
    struct Widget {
      int size() const;
      int width_;
    };
    int helper(int v);
    inline int twice(int v) { return helper(v) + helper(v); }
    }
    """,
    "a.cpp": '#include "shared.h"\nint from_a() { return app::twice(1); }\n',
    "b.cpp": """
    #include "shared.h"
    namespace app {
    int helper(int v) { return v + 1; }
    int Widget::size() const { return width_; }
    }
    """,
    "c.cpp": '#include "shared.h"\nint from_c() { return app::twice(2); }\n',
}


class TestDedupAcrossTranslationUnits:
    """ARCH-001 §5. Every header is reparsed once per TU that includes it, so the same
    entity arrives once per includer -- 15,656 method cursors for 161 classes in FTXUI.
    Getting this wrong inflates node counts by two orders of magnitude and changes no
    other observable thing."""

    def test_a_header_entity_is_one_node_however_many_tus_include_it(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        model = build(tmp_path, toolchain, SHARED, "a.cpp", "b.cpp", "c.cpp")
        assert model.completeness.tus_total == 3
        for qualified in ("app", "app::Widget", "app::Widget::size", "app::helper", "app::twice"):
            assert len(named(model, qualified)) == 1, qualified
        # The dictionary is keyed by id, so a duplicate can only show up as two entries
        # with the same qualified name; assert the totals agree so a future kind cannot
        # start duplicating unnoticed.
        assert len(model.nodes) == len({n.qualified_name for n in model.nodes.values()})

    def test_the_definition_wins_over_declarations_from_other_tus(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Two TUs see only the declaration, one sees the definition. The surviving node
        must be the definition -- it carries the body, and its location is where a reviewer
        gets sent. A declaration winning would produce a node that exists, looks right, and
        can never report a body change."""
        model = build(tmp_path, toolchain, SHARED, "a.cpp", "b.cpp", "c.cpp")
        helper = one(model, "app::helper")
        assert helper.body_hash is not None
        assert helper.location is not None and helper.location.file == "b.cpp"

    def test_the_model_does_not_depend_on_the_order_the_tus_were_parsed(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The merge rule has to be commutative or the model depends on compile-database
        order -- and on whatever order a cache or a worker pool returns partials in later
        (ARCH-001 §9). Two revisions differing only in that order would diff non-empty."""
        forward = build(tmp_path, toolchain, SHARED, "a.cpp", "b.cpp", "c.cpp")
        reverse = build(tmp_path, toolchain, SHARED, "c.cpp", "b.cpp", "a.cpp")
        assert forward.fingerprint() == reverse.fingerprint()
        assert [e.id for e in forward.edges] == [e.id for e in reverse.edges]
        # The fingerprint is provenance-free by design, so location -- the thing
        # "definitions win" is really about -- has to be checked separately.
        assert one(forward, "app::helper").location == one(reverse, "app::helper").location


class TestEdges:
    def test_a_call_seen_from_two_tus_is_one_edge_with_the_union_of_its_sites(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """`app::twice` is an inline header function calling `helper` twice, and every TU
        that includes the header extracts both call sites. One edge, two locations
        (ARCH-001 §4.2) -- not six, and not one with six."""
        model = build(tmp_path, toolchain, SHARED, "a.cpp", "b.cpp", "c.cpp")
        twice = one(model, "app::twice")
        calls = [e for e in model.edges if e.kind is EdgeKind.CALLS and e.src == twice.id]
        assert len(calls) == 1
        assert len(calls[0].locations) == 2
        assert len(set(calls[0].locations)) == 2

    def test_every_edge_endpoint_is_a_node_in_the_model(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The invariant the builder exists to establish. A partial model has dangling
        edges by construction; the assembled one must not, or every consumer downstream
        needs its own opinion about what a missing endpoint means."""
        model = build(tmp_path, toolchain, SHARED, "a.cpp", "b.cpp", "c.cpp")
        assert model.edges
        assert all(e.src in model.nodes and e.dst in model.nodes for e in model.edges)
        assert ("app::Widget", "app::Widget::size") in edge_pairs(model, EdgeKind.CONTAINS)

    def test_an_edge_resolved_by_one_tu_survives_from_the_tu_that_could_not(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """`a.cpp` calls into the header without ever seeing `helper`'s definition. The
        edge is only resolvable once `b.cpp`'s nodes are in, which is the reason resolution
        is the builder's job and not the extractor's."""
        model = build(tmp_path, toolchain, SHARED, "a.cpp")
        assert ("app::twice", "app::helper") in edge_pairs(model, EdgeKind.CALLS)


class TestTemplates:
    def test_a_call_to_an_instantiation_lands_on_the_template(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Nothing calls a template. Calls resolve to an instantiation whose USR belongs to
        no declaration anyone wrote -- 10,158 of fmt's call edges, a third of the graph.
        Left unaliased they all drop, and the model reports a call graph with a hole
        exactly where the generic code is."""
        model = build(
            tmp_path,
            toolchain,
            {
                "lib.h": "#pragma once\ntemplate <typename T> T identity(T v) { return v; }\n",
                "a.cpp": '#include "lib.h"\nint use() { return identity(1) + identity(2); }\n',
            },
            "a.cpp",
        )
        assert ("use", "identity") in edge_pairs(model, EdgeKind.CALLS)
        assert model.completeness.notes == []

    def test_an_explicit_specialization_keeps_the_calls_that_are_its_own(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The alias is applied only where the instantiation is not itself a node. An
        explicit specialization is hand-written code with its own identity (fmt has 173);
        rewriting calls to it onto the primary template would attribute a change in one
        body to the other."""
        model = build(
            tmp_path,
            toolchain,
            {
                "a.cpp": """
                template <typename T> int size(T v) { return 1; }
                template <> int size(char v) { return 2; }
                int use() { return size('a'); }
                """
            },
            "a.cpp",
        )
        sizes = named(model, "size")
        assert len(sizes) == 2
        primary = next(n for n in sizes if n.attrs.get("template"))
        special = next(n for n in sizes if not n.attrs.get("template"))
        calls = [e for e in model.edges if e.kind is EdgeKind.CALLS]
        assert [e.dst for e in calls] == [special.id]
        assert primary.id != special.id


class TestUnresolvableEndpoints:
    def test_an_endpoint_outside_the_repository_is_dropped_and_counted(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """A model is of *this* repository, so `printf` can never be a node. Dropping is
        right and inventing a node for it would be worse -- but a silent drop turns "we do
        not model libc" into "nothing here calls libc", which is ARCH-001 §11's failure
        mode. The count is the difference.

        **The premise changed and the assertion with it.** The count used to arrive as an
        English sentence in `Completeness.notes`, which meant a consumer could only get at
        it by matching on prose, and ARCH-001 §10 makes the JSON the product. It is now two
        integers, so the test asks for the numbers rather than for the word "dropped".
        """
        model = build(
            tmp_path,
            toolchain,
            {
                "a.cpp": "#include <stdio.h>\n"
                "namespace app {\n"
                "int helper() { return 1; }\n"
                'int use() { return printf("hi") + helper(); }\n'
                "}\n"
            },
            "a.cpp",
        )
        c = model.completeness
        assert all(e.src in model.nodes and e.dst in model.nodes for e in model.edges)
        assert c.edges_dropped > 0 and not c.edges_closed
        # The denominator is the population the numerator came out of: kept plus dropped.
        # A ratio over the kept edges alone would report >100 % on a model that dropped
        # more than it kept, which is exactly the case a reader would most need to trust.
        assert c.edges_total == c.edges_dropped + len(model.edges)
        assert 0.0 < c.edge_residue < 1.0

    def test_the_count_is_of_distinct_edges_on_both_sides_of_the_ratio(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Two calls to `printf` from two places are two distinct edges; two calls from one
        function are one edge with two locations (ARCH-001 §4.2). Counting arrivals instead
        would report a loss that no denominator in the model matches."""
        model = build(
            tmp_path,
            toolchain,
            {
                "a.cpp": "#include <stdio.h>\n"
                'int a() { return printf("hi") + printf("again"); }\n'
                'int b() { return printf("hi"); }\n'
            },
            "a.cpp",
        )
        assert model.completeness.edges_dropped == 2  # a -> printf, b -> printf

    def test_a_repository_that_calls_nothing_outside_itself_reports_a_closed_graph(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The other end of the scale, and the reason the residue is a *number* rather than
        a flag: `edges_closed` must be able to be True, or a consumer cannot distinguish
        "20 % of the graph leads outside" from "this is simply what models look like"."""
        model = build(
            tmp_path,
            toolchain,
            {
                "a.cpp": "namespace app {\n"
                "int helper() { return 1; }\n"
                "int use() { return helper(); }\n"
                "}\n"
            },
            "a.cpp",
        )
        assert model.completeness.edges_closed
        assert model.completeness.edges_dropped == 0
        assert model.completeness.edge_residue == 0.0


class TestCompleteness:
    def test_the_producer_that_built_the_model_is_recorded(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """A model built by a different parser is a different model (ARCH-001 §9, §13), so
        this is a cache-key component before it is a report field. Unset, a cached model
        from LLVM 16 gets compared against a fresh one from LLVM 20 and the version
        difference reads as source changes."""
        model = build(tmp_path, toolchain, SHARED, "a.cpp")
        assert model.completeness.producer_version == toolchain.id
        assert toolchain.id in model.completeness.producers
        assert toolchain.release in model.completeness.producer_version

    def test_the_revision_is_the_resolved_sha(self, tmp_path: Path, toolchain: Toolchain) -> None:
        model = build(tmp_path, toolchain, SHARED, "a.cpp")
        assert model.rev == REV

    def test_a_build_was_not_available_and_the_model_says_so(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """ARCH-001 §3.1 configures and never builds. The evidence layer joins on binary
        artifacts that therefore do not exist, and it has to be told rather than find out."""
        model = build(tmp_path, toolchain, SHARED, "a.cpp")
        assert model.completeness.build_available is False

    def test_a_tu_that_cannot_be_parsed_is_reported_not_raised(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """One unparseable file is incompleteness to report, not a reason to abandon a
        review (ARCH-001 §4.4, §11). The other TUs still contribute, and the model states
        that it is partial rather than looking complete and being short a file."""
        sources = {**SHARED, "bad.cpp": '#include "nonexistent-header.h"\nint gone();\n'}
        model = build(tmp_path, toolchain, sources, "a.cpp", "b.cpp", "bad.cpp")

        assert model.completeness.tus_total == 3
        assert model.completeness.parsed == 2
        assert model.completeness.failed == 1
        assert not model.completeness.is_complete

        failure = next(t for t in model.completeness.tus if t.status is TuStatus.FAILED)
        assert failure.path == "bad.cpp"
        assert failure.detail is not None and "nonexistent-header.h" in failure.detail
        assert named(model, "app::helper"), "a healthy TU stopped contributing"

    def test_every_translation_unit_appears_exactly_once(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        model = build(tmp_path, toolchain, SHARED, "a.cpp", "b.cpp", "c.cpp")
        assert [t.path for t in model.completeness.tus] == ["a.cpp", "b.cpp", "c.cpp"]
        assert model.completeness.tus_total == len(model.completeness.tus)


class TestAssemblyIsSeparableFromParsing:
    """ARCH-001 §9 caches per TU and reuses partials across runs and across revisions.
    Assembly therefore has to be a pure function of the partials it is given -- these are
    the properties that let a cache or a worker pool be dropped in front of it."""

    def test_assembling_does_not_modify_the_partials_it_was_given(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """A partial mutated during assembly is a cache entry that no longer describes its
        own TU. The next build reuses it and is confidently wrong -- the worst failure this
        system can have, and one no output would show."""
        checkout = project(tmp_path, SHARED, ("a.cpp", "b.cpp", "c.cpp"))
        partials = list(tu_extracts(checkout.cpp_entries, root=tmp_path, toolchain=toolchain))
        before = [
            ([n.model_dump() for n in p.nodes.values()], [e.model_dump() for e in p.edges.values()])
            for p in partials
        ]

        assemble(REV, partials, toolchain=toolchain)

        after = [
            ([n.model_dump() for n in p.nodes.values()], [e.model_dump() for e in p.edges.values()])
            for p in partials
        ]
        assert after == before

    def test_assembling_the_same_partials_twice_gives_the_same_model(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        checkout = project(tmp_path, SHARED, ("a.cpp", "b.cpp", "c.cpp"))
        partials = list(tu_extracts(checkout.cpp_entries, root=tmp_path, toolchain=toolchain))
        first = assemble(REV, partials, toolchain=toolchain)
        second = assemble(REV, partials, toolchain=toolchain)
        assert first.model_dump() == second.model_dump()
