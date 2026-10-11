"""The third-party set, and the refusal when it changed -- ADR-025.

Every test builds a real repository: the set is read from git's own answers (`ls-tree`), and
a hand-built tree would test the parser against itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from conftest import commit, git, move
from cpp_semantic_diff.acquire import (
    ThirdPartyEntry,
    ThirdPartySet,
    ThirdPartySource,
    changed_third_party,
    file_changes,
    refuse_changed_third_party,
    resolve_revs,
    third_party_set,
)
from cpp_semantic_diff.acquire.third_party import (
    HINT_SEGMENTS,
    folder_hints,
    hint_message,
    normalize_declared,
)
from cpp_semantic_diff.errors import (
    ExitCode,
    InvalidSettingError,
    PreconditionError,
    ThirdPartyChangedError,
    ThirdPartyPathError,
)
from cpp_semantic_diff.rank import Exposure, classify_path

SUBMODULE = frozenset({ThirdPartySource.SUBMODULE})
DECLARED = frozenset({ThirdPartySource.DECLARED})


def add_submodule(repo: Path, inner: Path, path: str) -> None:
    """`git submodule add` from a local repository. The `file://` relaxation is the fixture's,
    as in `conftest.submodule_repo`, never the tool's."""
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), path)
    git(repo, "commit", "-q", "-m", f"add {path}")


def add_bare_gitlink(repo: Path, inner: Path, path: str) -> None:
    """A gitlink to `inner`'s head at `path`, with no `.gitmodules` entry."""
    sha = git(inner, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}")
    git(repo, "commit", "-q", "-m", f"pin {path}")


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

    def test_a_submodule_path_is_spelled_like_a_declared_one(
        self, bare_repo: Path, inner: Path
    ) -> None:
        """`path = ./v/` in `.gitmodules` changes nothing: the gitlink is at `v`, as declared."""
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        add_submodule(bare_repo, inner, "v")
        gitmodules = bare_repo / ".gitmodules"
        gitmodules.write_text(
            gitmodules.read_text(encoding="utf-8").replace("path = v", "path = ./v/"),
            encoding="utf-8",
        )
        git(bare_repo, "add", ".gitmodules")
        git(bare_repo, "commit", "-q", "-m", "respell")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        commit(bare_repo, "a.cpp", "int a(int);\n", "B")
        revs = resolve_revs(bare_repo)

        found = third_party_set(bare_repo, revs, ["v"])
        assert found.entries == (ThirdPartyEntry(path="v", sources=SUBMODULE | DECLARED),)

    def test_a_gitmodules_entry_with_no_gitlink_is_not_in_the_set(
        self, submodule_repo: Path
    ) -> None:
        """A stale `.gitmodules` entry pins no code, so there is nothing to exclude."""
        gitmodules = submodule_repo / ".gitmodules"
        gitmodules.write_text(
            gitmodules.read_text(encoding="utf-8")
            + '[submodule "ghost"]\n\tpath = ghost\n\turl = ../ghost\n',
            encoding="utf-8",
        )
        git(submodule_repo, "add", ".gitmodules")
        git(submodule_repo, "commit", "-q", "-m", "stale entry")
        git(submodule_repo, "checkout", "-q", "-b", "feature")
        commit(submodule_repo, "main.cpp", "int m() { return 0; }\n", "edit")
        revs = resolve_revs(submodule_repo)

        assert third_party_set(submodule_repo, revs).paths == ("dep",)

    def test_a_gitlink_gitmodules_does_not_name_is_in_the_set(
        self, bare_repo: Path, inner: Path
    ) -> None:
        """A gitlink is a submodule in git's model, with or without `.gitmodules`: either way
        it pins code the repository does not contain."""
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        add_bare_gitlink(bare_repo, inner, "lib")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        commit(bare_repo, "a.cpp", "int a(int);\n", "B")

        found = third_party_set(bare_repo, resolve_revs(bare_repo))

        assert not (bare_repo / ".gitmodules").exists()
        assert found.entries == (ThirdPartyEntry(path="lib", sources=SUBMODULE),)

    def test_no_gitlink_is_an_empty_set(self, copied_repo: Path) -> None:
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
        assert str(raised.value).splitlines()[0] == (
            "--third-party names a path that exists at neither revision: test/gtset. It is "
            "probably a typo, and a typo would leave the library in the model."
        )

    def test_a_path_inside_a_submodule_says_it_is_already_third_party(
        self, submodule_repo: Path
    ) -> None:
        """The superproject tracks nothing inside a submodule, so the path is absent to it."""
        git(submodule_repo, "checkout", "-q", "-b", "feature")
        commit(submodule_repo, "main.cpp", "int m() { return 0; }\n", "edit")

        with pytest.raises(ThirdPartyPathError, match="neither revision: dep/include") as raised:
            third_party_set(submodule_repo, resolve_revs(submodule_repo), ["dep/include"])

        assert "dep/include is inside submodule `dep`, which is already third-party." in str(
            raised.value
        )

    def test_an_absent_path_outside_any_submodule_does_not_mention_one(
        self, submodule_repo: Path
    ) -> None:
        git(submodule_repo, "checkout", "-q", "-b", "feature")
        commit(submodule_repo, "main.cpp", "int m() { return 0; }\n", "edit")

        with pytest.raises(ThirdPartyPathError) as raised:
            third_party_set(submodule_repo, resolve_revs(submodule_repo), ["depx/include"])

        assert "submodule" not in str(raised.value)

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

    def test_bundled_is_a_hint(self) -> None:
        """spdlog vendors fmt under `include/spdlog/fmt/bundled`."""
        assert "bundled" in HINT_SEGMENTS

    @pytest.mark.parametrize("segment", sorted(HINT_SEGMENTS))
    def test_a_hint_name_does_not_change_how_code_is_ranked(self, segment: str) -> None:
        """Folder names exclude nothing (ADR-025): code under one ranks as if it were not."""
        assert classify_path(f"{segment}/x.h") is Exposure.PUBLIC
        assert classify_path(f"src/{segment}/x.cc") is Exposure.INTERNAL

    def test_the_message_names_the_option_to_pass(self) -> None:
        assert hint_message("test/gtest") == (
            "`test/gtest` looks like third-party code; pass `--third-party test/gtest` if it is."
        )


class TestLabelFor:
    def test_files_are_labelled_with_the_innermost_entry_they_lie_in(self) -> None:
        """A submodule inside a declared vendored folder: the submodule is the closer name."""
        tp = ThirdPartySet(
            entries=(
                ThirdPartyEntry(path="vendor", sources=DECLARED),
                ThirdPartyEntry(path="vendor/zlib", sources=SUBMODULE),
            )
        )
        assert tp.label_for("vendor/a.h") == "vendor"
        assert tp.label_for("vendor") == "vendor"
        assert tp.label_for("vendor/zlib/zlib.h") == "vendor/zlib"
        assert tp.label_for("vendors/a.h") is None
        assert tp.label_for("src/a.cpp") is None


def bump_submodule(repo: Path, inner: Path, path: str) -> str:
    """Move the gitlink at `path` to a new commit of `inner`, as a dependency bump does.

    Written to the index directly: the bump is the gitlink's new SHA, and fetching it into
    the submodule's checkout would add a transport the test does not need.
    """
    sha = commit(inner, "lib.h", "int lib(int);\n", "lib moves")
    git(repo, "update-index", "--cacheinfo", f"160000,{sha},{path}")
    git(repo, "commit", "-q", "-m", f"bump {path}")
    return sha


def changed(repo: Path, declared: Sequence[str] = ()) -> tuple[str, ...]:
    """What the refusal would name for the branch checked out in `repo`."""
    revs = resolve_revs(repo)
    return changed_third_party(third_party_set(repo, revs, declared), file_changes(repo, revs))


class TestRefusal:
    """Third-party code must be identical at both points (ADR-025)."""

    def test_a_submodule_bump_is_refused(self, bare_repo: Path, inner: Path) -> None:
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        add_submodule(bare_repo, inner, "third_party/lib")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        bump_submodule(bare_repo, inner, "third_party/lib")

        assert changed(bare_repo) == ("third_party/lib",)

    def test_a_bump_of_a_gitlink_gitmodules_does_not_name_is_refused(
        self, bare_repo: Path, inner: Path
    ) -> None:
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        add_bare_gitlink(bare_repo, inner, "lib")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        bump_submodule(bare_repo, inner, "lib")

        assert changed(bare_repo) == ("lib",)

    @pytest.mark.parametrize("where", ["diff.ignoreSubmodules", ".gitmodules"])
    def test_a_bump_git_config_would_hide_is_refused(
        self, bare_repo: Path, inner: Path, monkeypatch: pytest.MonkeyPatch, where: str
    ) -> None:
        """`ignore = all` drops a moved gitlink from `git diff`, wherever it is set; the
        refusal may not depend on whose config it ran under (ARCH-001 §12)."""
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        add_submodule(bare_repo, inner, "dep")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        if where == ".gitmodules":
            git(bare_repo, "config", "-f", ".gitmodules", "submodule.dep.ignore", "all")
            git(bare_repo, "commit", "-q", "-am", "ignore dep")
            git(bare_repo, "checkout", "-q", "main")
            git(bare_repo, "merge", "-q", "--ff-only", "feature")
            git(bare_repo, "checkout", "-q", "-b", "bump")
        else:
            # The `git -c` form, through the environment, so the tool's own git calls see it.
            monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
            monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.ignoreSubmodules")
            monkeypatch.setenv("GIT_CONFIG_VALUE_0", "all")
        bump_submodule(bare_repo, inner, "dep")

        assert changed(bare_repo) == ("dep",)

    def test_a_submodule_added_on_the_branch_is_refused(self, bare_repo: Path, inner: Path) -> None:
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        add_submodule(bare_repo, inner, "dep")

        assert changed(bare_repo) == ("dep",)

    def test_a_submodule_removed_on_the_branch_is_refused(
        self, bare_repo: Path, inner: Path
    ) -> None:
        """The set holds the base's submodules too, so a removal is a change to one."""
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        add_submodule(bare_repo, inner, "dep")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        git(bare_repo, "rm", "-q", "dep")
        git(bare_repo, "commit", "-q", "-m", "drop dep")

        assert changed(bare_repo) == ("dep",)

    def test_a_change_inside_a_declared_path_is_refused(self, copied_repo: Path) -> None:
        commit(copied_repo, "test/gtest/gmock/gmock.h", "// gmock 2\n", "gmock moves")
        assert changed(copied_repo, ["test/gtest"]) == ("test/gtest",)

    def test_a_change_next_to_a_declared_path_is_not_refused(self, copied_repo: Path) -> None:
        """A sibling whose name shares the prefix is the repository's own code."""
        commit(copied_repo, "test/gtest_main.cc", "int main() {}\n", "beside gtest")
        commit(copied_repo, "test/a_test.cc", "// test\n", "a test")
        assert changed(copied_repo, ["test/gtest"]) == ()

    def test_a_change_inside_an_undeclared_lookalike_is_not_refused(
        self, copied_repo: Path
    ) -> None:
        """A folder name is a hint, never a rule, so it refuses nothing either."""
        commit(copied_repo, "test/gtest/gtest.h", "// gtest 2\n", "gtest moves")
        assert changed(copied_repo) == ()

    @pytest.mark.parametrize(
        ("source", "target"),
        [("test/gtest/gtest.h", "src/gtest.h"), ("extern/zlib/zlib.h", "test/gtest/zlib.h")],
        ids=["out-of", "into"],
    )
    def test_a_rename_across_the_boundary_is_refused(
        self, copied_repo: Path, source: str, target: str
    ) -> None:
        """Checked at both ends: either way, the third-party directory's contents changed."""
        move(copied_repo, source, target, "move")
        changes = file_changes(copied_repo, resolve_revs(copied_repo))
        assert changes.renames, "the fixture must produce a rename"

        assert changed(copied_repo, ["test/gtest"]) == ("test/gtest",)

    def test_every_changed_entry_is_named_once(self, copied_repo: Path, inner: Path) -> None:
        """A declared folder and a submodule change on one branch; one refusal names both."""
        git(copied_repo, "checkout", "-q", "main")
        add_submodule(copied_repo, inner, "third_party/lib")
        git(copied_repo, "checkout", "-q", "-b", "both")
        commit(copied_repo, "extern/zlib/zlib.h", "// zlib 2\n", "zlib moves")
        commit(copied_repo, "extern/zlib/zconf.h", "// zconf\n", "zlib grows")
        bump_submodule(copied_repo, inner, "third_party/lib")

        revs = resolve_revs(copied_repo)
        tp = third_party_set(copied_repo, revs, ["extern/zlib", "test/gtest"])
        with pytest.raises(ThirdPartyChangedError) as caught:
            refuse_changed_third_party(tp, file_changes(copied_repo, revs))

        assert caught.value.paths == ("extern/zlib", "third_party/lib")
        message = str(caught.value)
        assert "extern/zlib, third_party/lib." in message
        assert "does not analyze changes to third-party code" in message

    def test_an_unchanged_set_passes(self, copied_repo: Path) -> None:
        revs = resolve_revs(copied_repo)
        tp = third_party_set(copied_repo, revs, ["test/gtest", "extern/zlib"])
        refuse_changed_third_party(tp, file_changes(copied_repo, revs))

    def test_the_refusal_is_a_repository_precondition(self) -> None:
        """Nothing on the command line fixes it: these revisions cannot be compared, exit 3."""
        assert issubclass(ThirdPartyChangedError, PreconditionError)
        assert not issubclass(ThirdPartyChangedError, InvalidSettingError)
        assert ThirdPartyChangedError.exit_code is ExitCode.REPOSITORY
