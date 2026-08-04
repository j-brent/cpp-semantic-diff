"""What the defining TU does and does not decide -- ARCH-001 §5, PLAN-001 §3.3.

§5 says identity must include the translation unit for entities whose USR is file-scoped.
`Node.tu` is declared for it and the producer does not populate it, so this module is mostly
**characterization**: it pins what the model does today, against the shapes that make the
question decidable. When `tu` is populated, the tests marked as such are expected to change,
and the diff between them is the change's effect -- stated in tests rather than argued from
a docstring.

The shapes live in `conftest.TU_IDENTITY_HAZARDS`, which explains why no real fixture can
stand in for them. Real libclang, a few lines of C++ each, no includes beyond one local
header.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from conftest import TU_IDENTITY_HAZARDS, TU_IDENTITY_TUS
from cpp_semantic_diff.acquire.workspace import Checkout, CompileDatabase
from cpp_semantic_diff.errors import PreconditionError
from cpp_semantic_diff.match import usr_file
from cpp_semantic_diff.model import Model
from cpp_semantic_diff.produce import build_model
from cpp_semantic_diff.produce.toolchain import Toolchain, load

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REV = "9f4d6c2e1b8a7d5c3e0f2a1b4c6d8e0f13579bdf"


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    try:
        return load()
    except PreconditionError as error:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable libclang: {error}")


def project(tmp_path: Path, sources: dict[str, str], tus: Sequence[str]) -> Checkout:
    """A synthetic checkout. Unlike `test_build.project`, this one creates directories --
    the collision under test needs two sources with one basename, which needs two of them."""
    for name, text in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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


@pytest.fixture(scope="module")
def hazards(tmp_path_factory: pytest.TempPathFactory, toolchain: Toolchain) -> Model:
    """Built once for the module: four TUs is ~1.5 s of real parsing, and every test here
    reads the same model. Nothing mutates it -- the one test that needs a changed model
    deep-copies first."""
    root = tmp_path_factory.mktemp("hazards")
    return build_model(project(root, TU_IDENTITY_HAZARDS, TU_IDENTITY_TUS), toolchain=toolchain)


def scoped(model: Model) -> dict[str, str]:
    """File-scoped USRs mapped to the file clang wrote into them."""
    return {i: f for i in model.nodes if (f := usr_file(i)) is not None}


class TestTheUsrCarriesANameNotAPath:
    """The fact everything else here follows from."""

    def test_a_file_scoped_usr_uses_the_basename(self, hazards: Model) -> None:
        files = set(scoped(hazards).values())
        assert files == {"util.cpp", "common.h"}, (
            "clang writes the file name, not the path -- which is why a/ and b/ collide "
            "and why a rename that changes the name breaks tier 1"
        )

    def test_external_linkage_carries_no_file_at_all(self, hazards: Model) -> None:
        """The control. Whatever `tu` comes to mean, it must not reach these."""
        entries = [i for i in hazards.nodes if i.endswith(("a_entry#", "b_entry#", "one_entry#"))]
        assert len(entries) == 3
        assert all(usr_file(i) is None for i in entries)


class TestTwoSourcesSharingABasename:
    """ARCH-001 §5's reason for requiring the TU, and the case no fixture repository has.

    `a/util.cpp` and `b/util.cpp` each declare `shared_helper` and `file_helper`. Four
    distinct functions, two USRs.
    """

    def test_the_usrs_collide(self, hazards: Model) -> None:
        util = sorted(i for i, f in scoped(hazards).items() if f == "util.cpp")
        assert util == ["c:util.cpp@F@file_helper#", "c:util.cpp@aN@F@shared_helper#"]

    def test_four_functions_collapse_into_two_nodes(self, hazards: Model) -> None:
        """**Characterization: this is the defect, not the design.**

        Half of these functions are absent from the model. A body change in whichever copy
        lost the merge produces *no event* -- under-reporting, which ARCH-001 §5 refuses
        more strongly than it refuses noise. Populating `Node.tu` is what separates them;
        when it does, this count becomes 4 and the test says so instead.
        """
        util = [i for i, f in scoped(hazards).items() if f == "util.cpp"]
        assert len(util) == 2, "two of the four survived; the other two are unreachable"

    def test_the_survivors_can_come_from_different_files(self, hazards: Model) -> None:
        """And which one survives is decided per node, so the model can hold one function
        from `a/` beside another from `b/` -- a combination present in no single TU."""
        util = [n for i, n in hazards.nodes.items() if scoped(hazards).get(i) == "util.cpp"]
        assert {n.location.file for n in util if n.location} <= {"a/util.cpp", "b/util.cpp"}


class TestAStaticDeclaredInAHeader:
    """The case that argues *against* setting `tu` unconditionally (PLAN-001 §3.3).

    `common.h` is included by two TUs, so its static and its anonymous-namespace function
    are one entity per includer. USR dedup collapses them to one node, and any TU recorded
    on it is whichever observation `_observation_rank` preferred -- a value that changes
    when an unrelated source file joins the build.
    """

    def test_it_is_one_node_however_many_tus_include_it(self, hazards: Model) -> None:
        header = sorted(i for i, f in scoped(hazards).items() if f == "common.h")
        assert header == ["c:common.h@F@header_static#", "c:common.h@aN@F@header_helper#"]

    def test_its_declaration_is_in_the_header_not_in_any_tu(self, hazards: Model) -> None:
        """The discriminator the narrow rule uses: the declaration's file is not the file
        of the translation unit that saw it, so no TU has a better claim than any other."""
        header = [n for i, n in hazards.nodes.items() if scoped(hazards).get(i) == "common.h"]
        assert header
        assert all(n.location and n.location.file == "common.h" for n in header)
        assert "common.h" not in TU_IDENTITY_TUS, "it is a header, never a translation unit"


class TestTheProducerDoesNotPopulateTu:
    """**Characterization.** `ir.py` declares `tu` as identity-relevant and nothing sets it.

    Pinned here rather than left implicit so that populating it is a visible change to a
    stated fact, not a silent one. The likeliest way that change goes wrong is that it
    quietly does nothing -- a path-normalization mismatch leaves `tu` None everywhere, every
    test still passes, and the model is unchanged. This test failing is the signal that it
    worked.
    """

    def test_nothing_carries_a_tu_today(self, hazards: Model) -> None:
        assert [n.id for n in hazards.nodes.values() if n.tu is not None] == []

    def test_the_fingerprint_does_not_see_the_tu_today(self, hazards: Model) -> None:
        """So a change that only moved an entity between TUs would fingerprint as identical.

        `Model.fingerprint()` claims two equal fingerprints produce an empty delta, and
        `diff/differ.py` relies on it. Once `tu` is identity that claim needs `tu` hashed
        under the same condition, or the claim becomes false.
        """
        before = hazards.fingerprint()
        moved = hazards.model_copy(deep=True)
        for node in moved.nodes.values():
            if usr_file(node.id) is not None:
                node.tu = "somewhere/else.cpp"
        assert moved.fingerprint() == before, (
            "characterization: the fingerprint is blind to `tu`. When `tu` becomes identity "
            "this must fail, and `fingerprint()` must hash it for file-scoped USRs."
        )
