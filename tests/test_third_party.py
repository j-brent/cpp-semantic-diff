"""The third-party set -- ADR-025.

Every test builds a real repository: the set is read from git's own answers (`.gitmodules`
blobs, `ls-tree`), and a hand-built tree would test the parser against itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import commit, git
from cpp_semantic_diff.acquire import (
    ThirdPartyEntry,
    ThirdPartySet,
    ThirdPartySource,
    resolve_revs,
    third_party_set,
)
from cpp_semantic_diff.acquire.third_party import (
    HINT_SEGMENTS,
    folder_hints,
    hint_message,
    normalize_declared,
    submodule_paths,
)
from cpp_semantic_diff.errors import ExitCode, InvalidSettingError, ThirdPartyPathError
from cpp_semantic_diff.rank.exposure import VENDOR_SEGMENTS

SUBMODULE = frozenset({ThirdPartySource.SUBMODULE})
DECLARED = frozenset({ThirdPartySource.DECLARED})


def add_submodule(repo: Path, inner: Path, path: str) -> None:
    """`git submodule add` from a local repository. The `file://` relaxation is the fixture's,
    as in `conftest.submodule_repo`, never the tool's."""
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), path)
    git(repo, "commit", "-q", "-m", f"add {path}")


@pytest.fixture
def inner(tmp_path: Path) -> Path:
    """A repository to pin as a submodule."""
    repo = tmp_path / "inner"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, "lib.h", "int lib();\n", "lib")
    return repo


@pytest.fixture
def copied_repo(bare_repo: Path) -> Path:
    """A repository with a copied library and a branch that touches only its own code.

        main:     A (src/, test/gtest/, include/x/fmt/bundled/, extern/zlib/)
                   \\
        feature:    └─ B (src/a.cpp)
    """
    repo = bare_repo
    commit(repo, "src/a.cpp", "int a() { return 1; }\n", "src")
    commit(repo, "test/gtest/gtest.h", "// gtest\n", "gtest")
    commit(repo, "test/gtest/gmock/gmock.h", "// gmock\n", "gmock")
    commit(repo, "include/x/fmt/bundled/base.h", "// fmt\n", "bundled fmt")
    commit(repo, "extern/zlib/zlib.h", "// zlib\n", "zlib")
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "src/a.cpp", "int a() { return 2; }\n", "change a")
    return repo


class TestSubmodules:
    """Submodules are third-party automatically, at the base or the head."""

    def test_a_submodule_is_in_the_set(self, submodule_repo: Path) -> None:
        git(submodule_repo, "checkout", "-q", "-b", "feature")
        commit(submodule_repo, "main.cpp", "int m() { return 0; }\n", "edit")
        revs = resolve_revs(submodule_repo)

        found = third_party_set(submodule_repo, revs)

        assert found.entries == (ThirdPartyEntry(path="dep", sources=SUBMODULE),)

    def test_a_submodule_only_the_head_has_is_in_the_set(
        self, bare_repo: Path, inner: Path
    ) -> None:
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        add_submodule(bare_repo, inner, "third_party/lib")
        revs = resolve_revs(bare_repo)

        assert submodule_paths(bare_repo, revs.base_sha) == set()
        assert third_party_set(bare_repo, revs).paths == ("third_party/lib",)

    def test_a_submodule_only_the_base_has_is_in_the_set(
        self, bare_repo: Path, inner: Path
    ) -> None:
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        add_submodule(bare_repo, inner, "third_party/lib")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        git(bare_repo, "rm", "-q", "third_party/lib")
        git(bare_repo, "commit", "-q", "-m", "drop the submodule")
        revs = resolve_revs(bare_repo)

        assert submodule_paths(bare_repo, revs.head_sha) == set()
        assert third_party_set(bare_repo, revs).paths == ("third_party/lib",)

    def test_the_working_trees_gitmodules_is_not_read(self, submodule_repo: Path) -> None:
        """The set is a function of the two revisions, not of what is checked out."""
        git(submodule_repo, "checkout", "-q", "-b", "feature")
        commit(submodule_repo, "main.cpp", "int m() { return 0; }\n", "edit")
        revs = resolve_revs(submodule_repo)
        (submodule_repo / ".gitmodules").write_text(
            '[submodule "x"]\n\tpath = not/a/submodule\n', encoding="utf-8"
        )

        assert third_party_set(submodule_repo, revs).paths == ("dep",)

    def test_no_gitmodules_is_an_empty_set(self, copied_repo: Path) -> None:
        assert third_party_set(copied_repo, resolve_revs(copied_repo)).entries == ()


class TestDeclaredPaths:
    """`--third-party PATH`: repository-relative directories, refused when neither side has one."""

    def test_a_declared_directory_is_in_the_set(self, copied_repo: Path) -> None:
        found = third_party_set(copied_repo, resolve_revs(copied_repo), ["test/gtest"])
        assert found.entries == (ThirdPartyEntry(path="test/gtest", sources=DECLARED),)

    def test_a_directory_only_the_base_has_is_accepted(self, copied_repo: Path) -> None:
        git(copied_repo, "rm", "-q", "-r", "extern/zlib")
        git(copied_repo, "commit", "-q", "-m", "drop zlib")

        found = third_party_set(copied_repo, resolve_revs(copied_repo), ["extern/zlib"])

        assert found.paths == ("extern/zlib",)

    def test_a_declared_submodule_has_both_sources(self, submodule_repo: Path) -> None:
        git(submodule_repo, "checkout", "-q", "-b", "feature")
        commit(submodule_repo, "main.cpp", "int m() { return 0; }\n", "edit")

        found = third_party_set(submodule_repo, resolve_revs(submodule_repo), ["dep"])

        assert found.entries == (ThirdPartyEntry(path="dep", sources=SUBMODULE | DECLARED),)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("test/gtest", "test/gtest"),
            ("test/gtest/", "test/gtest"),
            ("./test/gtest", "test/gtest"),
            ("test//gtest", "test/gtest"),
        ],
    )
    def test_spelling_is_normalized(self, raw: str, expected: str) -> None:
        assert normalize_declared(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "says"),
        [
            ("/abs/path", "absolute"),
            ("../elsewhere", "leaves the repository"),
            ("test/../../x", "leaves the repository"),
            (".", "names the repository itself"),
            ("", "names the repository itself"),
        ],
    )
    def test_a_path_outside_the_repository_is_refused(self, raw: str, says: str) -> None:
        with pytest.raises(ThirdPartyPathError, match=says):
            normalize_declared(raw)

    def test_a_path_neither_revision_has_is_refused_by_name(self, copied_repo: Path) -> None:
        with pytest.raises(ThirdPartyPathError, match="test/gtset") as raised:
            third_party_set(copied_repo, resolve_revs(copied_repo), ["test/gtest", "test/gtset"])
        assert "neither revision" in str(raised.value)
        assert "test/gtest," not in str(raised.value), "only the missing path is named"

    def test_a_file_is_refused_as_not_a_directory(self, copied_repo: Path) -> None:
        with pytest.raises(ThirdPartyPathError, match="not a directory"):
            third_party_set(copied_repo, resolve_revs(copied_repo), ["test/gtest/gtest.h"])

    def test_the_refusal_is_a_usage_error(self) -> None:
        """A path the caller can fix on the command line: exit 2, like any bad option."""
        assert issubclass(ThirdPartyPathError, InvalidSettingError)
        assert ThirdPartyPathError.exit_code is ExitCode.USAGE


class TestFolderHints:
    """Folder names suggest; they never exclude (ADR-025)."""

    def test_named_directories_outside_the_set_are_hinted(self, copied_repo: Path) -> None:
        found = third_party_set(copied_repo, resolve_revs(copied_repo))
        assert found.hints == ("extern", "include/x/fmt/bundled", "test/gtest")
        assert found.entries == (), "a hint excludes nothing"

    def test_a_declared_directory_is_not_hinted(self, copied_repo: Path) -> None:
        found = third_party_set(
            copied_repo, resolve_revs(copied_repo), ["test/gtest", "include/x/fmt/bundled"]
        )
        assert found.hints == ("extern",)

    def test_a_directory_holding_a_submodule_is_not_hinted(
        self, bare_repo: Path, inner: Path
    ) -> None:
        """leveldb's `third_party` holds its two submodules and nothing to declare."""
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        add_submodule(bare_repo, inner, "third_party/lib")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        commit(bare_repo, "a.cpp", "int a(int);\n", "B")

        found = third_party_set(bare_repo, resolve_revs(bare_repo))

        assert found.paths == ("third_party/lib",)
        assert found.hints == ()

    @pytest.mark.parametrize(
        ("directories", "third_party", "expected"),
        [
            # Topmost only: one note covers what it contains.
            (["test", "test/gtest", "test/gtest/gmock"], [], ["test/gtest"]),
            # Inside a set entry.
            (["vendor", "vendor/gtest"], ["vendor"], []),
            # Containing a set entry; a sibling match inside it is still hinted.
            (
                ["third_party", "third_party/dep", "third_party/gtest"],
                ["third_party/dep"],
                ["third_party/gtest"],
            ),
            # The directory's own name, case-insensitively, not any segment of its path.
            (["src/Vendor", "third_party_tools"], [], ["src/Vendor"]),
            # A prefix that is not a path component does not count as containment.
            (["deps", "deps2"], ["deps2"], ["deps"]),
        ],
    )
    def test_the_hint_rule(
        self, directories: list[str], third_party: list[str], expected: list[str]
    ) -> None:
        assert folder_hints(directories, third_party) == tuple(expected)

    def test_bundled_is_a_hint_but_not_yet_a_ranking_rule(self) -> None:
        """Ranking reads `VENDOR_SEGMENTS` until vendored ranking goes; `bundled` is a hint."""
        assert "bundled" in HINT_SEGMENTS
        assert "bundled" not in VENDOR_SEGMENTS
        assert VENDOR_SEGMENTS <= HINT_SEGMENTS

    def test_the_message_names_the_option_to_pass(self) -> None:
        assert hint_message("test/gtest") == (
            "`test/gtest` looks like third-party code; pass `--third-party test/gtest` if it is."
        )


class TestLabelFor:
    def test_files_are_labelled_with_the_entry_they_lie_in(self) -> None:
        tp = ThirdPartySet(
            entries=(
                ThirdPartyEntry(path="third_party/lib", sources=SUBMODULE),
                ThirdPartyEntry(path="third_party/lib/vendored", sources=DECLARED),
            )
        )
        assert tp.label_for("third_party/lib/a.h") == "third_party/lib"
        assert tp.label_for("third_party/lib") == "third_party/lib"
        assert tp.label_for("third_party/lib/vendored/b.h") == "third_party/lib/vendored"
        assert tp.label_for("third_party/library/a.h") is None
        assert tp.label_for("src/a.cpp") is None
