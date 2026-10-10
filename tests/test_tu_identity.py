"""What the defining TU does and does not decide -- ARCH-001 §5.

§5 says identity must include the translation unit for entities whose USR is file-scoped.
`Node.tu` now carries it, under a rule narrower than §5's text: only where the declaration is
in the translation unit's *own* source, because anywhere else the TU is whichever observation
won a merge and would flip when an unrelated file joined the build.

Most of this module began as characterization -- tests asserting the gap was open, written to
fail when it closed, because the likeliest failure of that change was silently doing nothing.
They failed on its first run and are now inverted. What remains characterization is
`TestTwoSourcesSharingABasename`, which pins a defect `Node.tu` does **not** fix and which
the plan for that work wrongly claimed it would.

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
    """Built once for the module: four TUs is real parsing, and every test here reads the same
    model. Nothing mutates it -- the one test that needs a changed model deep-copies first."""
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
        """**Characterization, and `Node.tu` does not fix it** -- which the plan got wrong.

        The claim was that populating `tu` would separate these. It does not: `merge_node`
        keys the model on `Node.id`, which *is* the USR, so two same-USR statics merge into
        one node whether or not that node then records a TU. Half of these functions remain
        absent from the model and a body change in either produces no event.

        What `tu` fixes is the *matching* half -- a base node from `a/` no longer pairs with a
        head node from `b/` (`test_match.py`). Separating them in the model needs the node
        dictionary keyed on `(usr, tu)`, which reaches every edge endpoint, every event and the
        JSON contract. Recorded as a standing decision (v0 tracker §6,
        reports/2026-08-01--v0-build/STATUS.md) rather than half-done here.
        """
        util = [i for i, f in scoped(hazards).items() if f == "util.cpp"]
        assert len(util) == 2, "two of the four survived; the other two are unreachable"

    def test_the_survivors_can_come_from_different_files(self, hazards: Model) -> None:
        """And which one survives is decided per node, so the model can hold one function
        from `a/` beside another from `b/` -- a combination present in no single TU."""
        util = [n for i, n in hazards.nodes.items() if scoped(hazards).get(i) == "util.cpp"]
        assert {n.location.file for n in util if n.location} <= {"a/util.cpp", "b/util.cpp"}


class TestAStaticDeclaredInAHeader:
    """The case that argues *against* setting `tu` unconditionally.

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


class TestTheProducerRecordsTheDefiningTu:
    """ARCH-001 §5's "identity must include the TU", under the narrow rule.

    These two were characterization tests asserting the gap was open, written to *fail* when
    it closed -- the guard against the change silently doing nothing, which was its likeliest
    failure mode: one path-normalization mismatch leaves `tu` None everywhere while every
    other test still passes. Both failed on the first run of the real change. Inverted here.
    """

    def test_a_static_in_its_own_source_carries_that_source(self, hazards: Model) -> None:
        util = {n.tu for i, n in hazards.nodes.items() if scoped(hazards).get(i) == "util.cpp"}
        assert util <= {"a/util.cpp", "b/util.cpp"}
        assert None not in util

    def test_a_header_declared_static_carries_none(self, hazards: Model) -> None:
        """The narrow rule's whole point. `common.h` is one entity per includer, collapsed to
        one node whose TU would be whichever observation ranked highest -- a value that flips
        when an unrelated source joins the build."""
        header = [n for i, n in hazards.nodes.items() if scoped(hazards).get(i) == "common.h"]
        assert header and all(n.tu is None for n in header)

    def test_external_linkage_never_carries_one(self, hazards: Model) -> None:
        """The same entity legitimately appears in every TU that includes its header, so a
        TU here would be recording which includer the merge happened to prefer."""
        external = [n for i, n in hazards.nodes.items() if usr_file(i) is None]
        assert external and all(n.tu is None for n in external)

    def test_the_tu_is_repository_relative(self, hazards: Model) -> None:
        """An absolute path would carry the SHA-named worktree directory into every partial, so
        base and head would never agree and every internal-linkage match would break on every
        comparison."""
        assert all(not n.tu.startswith("/") for n in hazards.nodes.values() if n.tu)

    def test_the_fingerprint_sees_the_tu_where_it_is_identity(self, hazards: Model) -> None:
        """`Model.fingerprint()` claims two equal fingerprints produce an empty delta, and
        `diff/differ.py` relies on it. With `tu` in the tier-1 key, a fingerprint blind to it
        would call two models identical that the matcher pairs differently."""
        before = hazards.fingerprint()
        moved = hazards.model_copy(deep=True)
        for node in moved.nodes.values():
            if usr_file(node.id) is not None:
                node.tu = "somewhere/else.cpp"
        assert moved.fingerprint() != before

    def test_the_fingerprint_ignores_it_where_it_is_not(self, hazards: Model) -> None:
        """Hashed under the *same* condition `DeclaredTier.key` applies, so there is one rule
        about when the TU counts. An external-linkage node's TU is not identity, and a
        fingerprint that saw it would report a difference the matcher does not."""
        before = hazards.fingerprint()
        moved = hazards.model_copy(deep=True)
        for node in moved.nodes.values():
            if usr_file(node.id) is None:
                node.tu = "somewhere/else.cpp"
        assert moved.fingerprint() == before
