"""The IR -- ARCH-001 §4."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cpp_semantic_diff.acquire.workspace import CompileDatabase
from cpp_semantic_diff.model import (
    Completeness,
    Edge,
    EdgeKind,
    Location,
    MatchTier,
    Model,
    Node,
    NodeId,
    NodeKind,
    TuResult,
    TuStatus,
)


def node(id_: str, **kwargs: Any) -> Node:
    defaults = {
        "kind": NodeKind.FUNCTION,
        "name": id_,
        "qualified_name": id_,
    }
    return Node(id=NodeId(id_), **{**defaults, **kwargs})


class TestAttributesStayScalar:
    """ARCH-001 §4.3 -- an attribute diff must stay mechanical."""

    @pytest.mark.parametrize("value", ["public", 3, 3.5, True, None])
    def test_scalars_are_accepted(self, value: object) -> None:
        assert node("f", attrs={"k": value}).attrs["k"] == value

    @pytest.mark.parametrize("value", [{"nested": 1}, ["a"], (1, 2)])
    def test_structures_are_rejected(self, value: object) -> None:
        with pytest.raises(ValidationError, match="must be scalar"):
            node("f", attrs={"k": value})


class TestLocationIsProvenanceOnly:
    def test_separators_are_normalized(self) -> None:
        assert Location(file="src\\lib\\a.cpp", line=1).file == "src/lib/a.cpp"

    def test_location_does_not_affect_fingerprint(self) -> None:
        """An entity that moved is the same entity (ARCH-001 §4.1)."""
        a = Model(rev="a", nodes={NodeId("f"): node("f", location=Location(file="a.cpp", line=1))})
        b = Model(rev="b", nodes={NodeId("f"): node("f", location=Location(file="z.cpp", line=99))})
        assert a.fingerprint() == b.fingerprint()


class TestEdgeIdentity:
    def test_id_is_derived_from_endpoints_and_kind(self) -> None:
        edge = Edge(src=NodeId("a"), dst=NodeId("b"), kind=EdgeKind.CALLS)
        assert edge.id == "a|calls|b"

    def test_repeated_call_sites_are_one_edge_with_many_locations(self) -> None:
        """Dedup is a correctness requirement, not an optimization (ARCH-001 §5)."""
        edge = Edge(
            src=NodeId("a"),
            dst=NodeId("b"),
            kind=EdgeKind.CALLS,
            locations=[Location(file="a.cpp", line=3), Location(file="a.cpp", line=9)],
        )
        assert len(edge.locations) == 2
        assert edge.id == "a|calls|b"

    def test_different_kinds_are_different_edges(self) -> None:
        calls = Edge(src=NodeId("a"), dst=NodeId("b"), kind=EdgeKind.CALLS)
        uses = Edge(src=NodeId("a"), dst=NodeId("b"), kind=EdgeKind.USES_IN_IMPLEMENTATION)
        assert calls.id != uses.id


class TestMatchTierIsClosed:
    """ARCH-001 §5: identity matches in tiers, most-confident first, and the tier that
    produced a match is recorded on it and propagates into the confidence of every event
    derived from it -- so a tier the model does not know about cannot silently appear."""

    def test_tiers_are_exactly_the_ones_arch_names(self) -> None:
        """§5's table, most recently extended by ADR-036's `name` tier (a function or
        method whose signature changed, left over from tier 1's USR match). No test
        constructed this enum's whole set before: `match.py`'s tests exercise individual
        tiers but never check that the type has no more and no fewer than ARCH-001 names,
        so an accidental extra member -- or a dropped one -- would pass everywhere else."""
        assert {t.value for t in MatchTier} == {
            "declared",
            "name",
            "structural",
            "positional",
            "similarity",
        }


class TestFingerprint:
    def test_identical_models_agree(self) -> None:
        def build(rev: str) -> Model:
            m = Model(rev=rev)
            m.add_node(node("f", signature="void f()"))
            m.edges.append(Edge(src=NodeId("f"), dst=NodeId("g"), kind=EdgeKind.CALLS))
            return m

        assert build("a").fingerprint() == build("b").fingerprint()

    def test_signature_change_is_visible(self) -> None:
        a = Model(rev="a", nodes={NodeId("f"): node("f", signature="void f()")})
        b = Model(rev="b", nodes={NodeId("f"): node("f", signature="void f(int)")})
        assert a.fingerprint() != b.fingerprint()

    def test_attribute_change_is_visible(self) -> None:
        a = Model(rev="a", nodes={NodeId("f"): node("f", attrs={"virtual": False})})
        b = Model(rev="b", nodes={NodeId("f"): node("f", attrs={"virtual": True})})
        assert a.fingerprint() != b.fingerprint()

    def test_body_change_is_visible(self) -> None:
        a = Model(rev="a", nodes={NodeId("f"): node("f", body_hash="aaa")})
        b = Model(rev="b", nodes={NodeId("f"): node("f", body_hash="bbb")})
        assert a.fingerprint() != b.fingerprint()

    def test_node_order_does_not_matter(self) -> None:
        a = Model(rev="a")
        a.add_node(node("f"))
        a.add_node(node("g"))
        b = Model(rev="b")
        b.add_node(node("g"))
        b.add_node(node("f"))
        assert a.fingerprint() == b.fingerprint()


class TestTheTwoKindsOfIncompletenessAreDistinguished:
    """ARCH-001 §4.4 and §11. The claim: a model can be missing things in two ways that
    mean different things, and a record that reports one number for both is a record that
    either cries wolf on every run or hides a fifth of the graph.

    *Coverage* is a source the producer could not read: a real entity reads as absent, which
    is §11's failure exactly. *Boundary* is a relation to something the repository never
    declares -- `std::`, libc, implicit special members -- which is where the modelled world
    ends and is present on every real repository.
    """

    @staticmethod
    def _parsed(n: int = 1) -> Completeness:
        return Completeness(
            tus_total=n, tus=[TuResult(path=f"{i}.cpp", status=TuStatus.PARSED) for i in range(n)]
        )

    def test_dropped_edges_are_numbers_a_consumer_can_state(self) -> None:
        """**The regression this whole field exists for.** The count used to live only as
        prose in `notes`, so a consumer could report the fact only by parsing English --
        and ARCH-001 §10 makes the JSON the product."""
        c = self._parsed().model_copy(update={"edges_total": 26_071, "edges_dropped": 5_113})
        assert (c.edges_dropped, c.edges_total, c.edges_resolved) == (5_113, 26_071, 20_958)
        assert not c.edges_closed
        assert 0.19 < c.edge_residue < 0.20

    def test_a_lost_fifth_of_the_graph_is_not_a_coverage_failure(self) -> None:
        """The judgement this design turns on. Every TU parsed, so nothing the model set
        out to read is missing; the residue points outside the repository by construction.
        Folding it into `is_complete` would make that flag False on every real run and
        `Confidence.models_complete` False on every event, which turns the signal rank uses
        to promote uncertain events into a constant."""
        c = self._parsed().model_copy(update={"edges_total": 26_071, "edges_dropped": 5_113})
        assert c.is_complete
        assert not c.edges_closed

    def test_a_failed_tu_is_a_coverage_failure_even_with_a_closed_graph(self) -> None:
        """And the converse, so neither property is quietly standing in for the other."""
        c = Completeness(
            tus_total=2,
            tus=[
                TuResult(path="a.cpp", status=TuStatus.PARSED),
                TuResult(path="b.cpp", status=TuStatus.FAILED, detail="stddef.h not found"),
            ],
        )
        assert not c.is_complete
        assert c.edges_closed

    def test_a_closed_graph_is_reachable(self) -> None:
        """`edges_closed` has to be able to be True or the distinction is decoration: a
        consumer could not tell "20 % leads outside" from "models are simply like this"."""
        c = self._parsed().model_copy(update={"edges_total": 12, "edges_dropped": 0})
        assert c.edges_closed and c.edge_residue == 0.0

    def test_a_model_that_considered_no_edges_reports_no_residue(self) -> None:
        """Zero over zero is 0.0 and not a crash, because a header-only fixture with no
        calls is a legitimate model and the ratio is read on every run."""
        assert self._parsed().edge_residue == 0.0

    def test_the_summary_states_both_numerically(self) -> None:
        """§11's "state what was missing" means numbers. An adjective is not a measurement, and
        one dropped count is routine against a large graph and a catastrophe against a small
        one."""
        c = self._parsed().model_copy(update={"edges_total": 26_071, "edges_dropped": 5_113})
        summary = c.summary()
        assert "1/1 TUs parsed" in summary
        assert "5,113/26,071" in summary and "19.6%" in summary


class TestCompleteness:
    def test_reports_shortfall(self) -> None:
        c = Completeness(
            tus_total=4,
            tus=[
                TuResult(path="a.cpp", status=TuStatus.PARSED),
                TuResult(path="b.cpp", status=TuStatus.PARSED),
                TuResult(path="c.cpp", status=TuStatus.PARSED),
                TuResult(path="d.cpp", status=TuStatus.FAILED, detail="missing generated header"),
            ],
        )
        assert not c.is_complete
        assert c.parsed == 3
        assert c.failed == 1
        assert "25.0%" in c.summary()

    def test_empty_model_is_not_complete(self) -> None:
        assert not Completeness().is_complete


class TestCompileDatabase:
    def test_parses_command_form(self, tmp_path: Path) -> None:
        db_path = tmp_path / "compile_commands.json"
        db_path.write_text(
            json.dumps(
                [
                    {
                        "directory": "/build",
                        "file": "/src/a.cpp",
                        "command": "clang++ -std=c++20 -I/src/include -c /src/a.cpp",
                    }
                ]
            ),
            encoding="utf-8",
        )
        db = CompileDatabase.load(db_path)
        assert len(db) == 1
        assert "-std=c++20" in db.entries[0].arguments

    def test_parses_arguments_form_and_relative_paths(self, tmp_path: Path) -> None:
        db_path = tmp_path / "compile_commands.json"
        db_path.write_text(
            json.dumps(
                [
                    {
                        "directory": "/build",
                        "file": "a.cpp",
                        "arguments": ["clang++", "-c", "a.cpp"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        entry = CompileDatabase.load(db_path).entries[0]
        assert entry.file == Path("/build") / "a.cpp"
        assert entry.arguments == ["clang++", "-c", "a.cpp"]

    def test_rooted_paths_survive_a_foreign_host(self, tmp_path: Path) -> None:
        """A database is routinely read somewhere other than where it was written.

        Committed to a repo, copied off a build machine, produced in a container.
        `Path.is_absolute()` answers only for the running platform -- a POSIX-rooted entry
        read on Windows reads as relative and gets joined onto the build directory,
        silently producing a path to nothing. The absoluteness test must be
        flavour-agnostic even though the tool itself targets Linux.
        """
        db_path = tmp_path / "compile_commands.json"
        db_path.write_text(
            json.dumps(
                [{"directory": "/home/u/build", "file": "/home/u/src/a.cpp", "arguments": []}]
            ),
            encoding="utf-8",
        )
        entry = CompileDatabase.load(db_path).entries[0]
        assert entry.file == Path("/home/u/src/a.cpp")

    def test_distinguishes_cpp_from_headers_and_c(self, tmp_path: Path) -> None:
        db_path = tmp_path / "compile_commands.json"
        db_path.write_text(
            json.dumps(
                [
                    {"directory": "/b", "file": "/s/a.cpp", "arguments": []},
                    {"directory": "/b", "file": "/s/b.cc", "arguments": []},
                    {"directory": "/b", "file": "/s/c.c", "arguments": []},
                ]
            ),
            encoding="utf-8",
        )
        db = CompileDatabase.load(db_path)
        assert sum(e.is_cpp for e in db.entries) == 2


class TestGeneratorSelection:
    """ARCH-001 §3.1 -- configure must produce a compile database, not merely succeed.

    Only Ninja and the Makefile generators emit one. The others -- Visual Studio, Xcode --
    configure successfully and emit nothing, which turns a hard precondition into a silent
    one. Leaving the choice to CMake is the failure; the tool has to pick.
    """

    def test_picks_ninja_when_no_generator_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cpp_semantic_diff.acquire.workspace import select_generator

        # A generator named in the environment is a choice too, and is respected.
        monkeypatch.delenv("CMAKE_GENERATOR", raising=False)
        assert select_generator(None) == ["-G", "Ninja"]
        assert select_generator([]) == ["-G", "Ninja"]

    def test_respects_an_explicit_generator(self) -> None:
        from cpp_semantic_diff.acquire.workspace import select_generator

        assert select_generator(["-G", "Unix Makefiles"]) == []
        assert select_generator(["-GNinja Multi-Config"]) == []

    def test_reads_generator_from_an_existing_cache(self, tmp_path: Path) -> None:
        from cpp_semantic_diff.acquire.workspace import cached_generator

        assert cached_generator(tmp_path) is None
        (tmp_path / "CMakeCache.txt").write_text(
            "CMAKE_BUILD_TYPE:STRING=\nCMAKE_GENERATOR:INTERNAL=Visual Studio 17 2022\n",
            encoding="utf-8",
        )
        assert cached_generator(tmp_path) == "Visual Studio 17 2022"
