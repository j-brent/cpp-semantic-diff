"""Revision resolution -- ARCH-001 §3.1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import commit, git, require_fixture
from cpp_semantic_diff.acquire import git as git_module
from cpp_semantic_diff.acquire.git import (
    RENAME_SIMILARITY_ENV,
    RevPair,
    changed_files,
    detect_default_branch,
    diff_line_count,
    file_changes,
    is_git_repository,
    is_shallow,
    resolve_rename_similarity,
    resolve_revs,
)
from cpp_semantic_diff.acquire.workspace import materialize_submodules
from cpp_semantic_diff.errors import (
    CompileDatabaseError,
    GitError,
    GitNotFoundError,
    InvalidSettingError,
    NotAGitRepositoryError,
    ShallowRepositoryError,
    UnknownRefError,
)


class TestMergeBaseSemantics:
    """The base must be merge-base(main, HEAD), never main's tip.

    If this is wrong, every review shows the reviewer work that other people merged while
    the branch was open, and no amount of good analysis downstream recovers from it.
    """

    def test_base_is_the_fork_point_not_main_tip(self, diverged_repo: Path) -> None:
        revs = resolve_revs(diverged_repo)

        main_tip = git(diverged_repo, "rev-parse", "main")
        fork_point = git(diverged_repo, "merge-base", "main", "HEAD")

        assert revs.base_sha == fork_point
        assert revs.base_sha != main_tip

    def test_unrelated_main_work_is_excluded(self, diverged_repo: Path) -> None:
        revs = resolve_revs(diverged_repo)
        files = changed_files(diverged_repo, revs)

        assert files == ["c.cpp"], "only the branch's own change should appear"
        assert "d.cpp" not in files, "main's later commit must not be attributed to the branch"

    def test_head_resolves_to_the_branch_tip(self, diverged_repo: Path) -> None:
        revs = resolve_revs(diverged_repo)
        assert revs.head_sha == git(diverged_repo, "rev-parse", "feature")

    def test_explicit_base_ref_is_honoured(self, diverged_repo: Path) -> None:
        revs = resolve_revs(diverged_repo, base_ref="main")
        assert revs.base_ref == "main"
        assert revs.base_sha == git(diverged_repo, "merge-base", "main", "HEAD")


class TestPreconditions:
    """ARCH-001 §11: these are hard failures, never degraded output."""

    def test_shallow_clone_is_rejected(self, shallow_repo: Path) -> None:
        assert is_shallow(shallow_repo)
        with pytest.raises(ShallowRepositoryError, match="fetch --unshallow"):
            resolve_revs(shallow_repo)

    def test_non_repository_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(NotAGitRepositoryError):
            resolve_revs(tmp_path)

    def test_unknown_ref_is_rejected(self, diverged_repo: Path) -> None:
        with pytest.raises(UnknownRefError, match="no-such-branch"):
            resolve_revs(diverged_repo, base_ref="no-such-branch")

    def test_a_missing_git_is_not_reported_as_a_missing_repository(
        self, diverged_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Told "not a git repository", the user inspects a checkout that is fine."""
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        with pytest.raises(GitNotFoundError):
            resolve_revs(diverged_repo)
        with pytest.raises(GitNotFoundError):
            is_git_repository(diverged_repo)


class TestDefaultBranchDetection:
    def test_prefers_main(self, diverged_repo: Path) -> None:
        assert detect_default_branch(diverged_repo) == "main"

    def test_falls_back_to_master(self, bare_repo: Path) -> None:
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        git(bare_repo, "branch", "-m", "main", "master")
        assert detect_default_branch(bare_repo) == "master"


class TestCompressionDenominator:
    def test_counts_added_and_deleted_lines(self, diverged_repo: Path) -> None:
        revs = resolve_revs(diverged_repo)
        # c.cpp is one added line, and nothing else is in scope.
        assert diff_line_count(diverged_repo, revs) == 1

    def test_empty_range_is_zero(self, diverged_repo: Path) -> None:
        revs = resolve_revs(diverged_repo, base_ref="HEAD")
        assert revs.is_empty
        assert diff_line_count(diverged_repo, revs) == 0


class TestAWorktreeCarriesWhatTheRevisionPins:
    """A submodule is a gitlink, and `git worktree add` materializes it as an empty directory.

    Found by widening the fixture set: fmt and spdlog vendor by copying, so nothing pinned a
    submodule until leveldb. The visible symptom was CMake reporting no `CMakeLists.txt`,
    surfaced as ARCH-001 §3.1's "the base is expected to build" -- alarming and wrong, since
    the base builds fine and it was the worktree that was incomplete.

    The quieter case is the one that matters more: where a missing submodule does *not* break
    configure, both models lose the same entities and agree with each other. That is the
    silent-truncation failure -- two models wrong in the same way -- by a different road.
    """

    def test_a_worktree_starts_without_the_submodule(
        self, submodule_repo: Path, tmp_path: Path
    ) -> None:
        """The behaviour being compensated for. If git ever populates these itself, this
        test fails and `materialize_submodules` can go."""
        worktree = tmp_path / "wt"
        git(submodule_repo, "worktree", "add", "--detach", "--quiet", str(worktree), "HEAD")
        assert (worktree / "dep").is_dir(), "the gitlink becomes a directory"
        assert not (worktree / "dep" / "vendor.h").exists(), "but an empty one"

    def test_materializing_fills_it_in(
        self, submodule_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `GIT_CONFIG_*` is the only way to pass this that git honours, short of putting it
        # in the tool. A local submodule URL is a fixture artefact; relaxing the transport
        # guard belongs to the test that created one, not to production code.
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        worktree = tmp_path / "wt"
        git(submodule_repo, "worktree", "add", "--detach", "--quiet", str(worktree), "HEAD")
        materialize_submodules(worktree, "HEAD")
        assert (worktree / "dep" / "vendor.h").read_text(encoding="utf-8") == "int vendored();\n"

    def test_a_submodule_that_cannot_be_fetched_is_refused_not_ignored(
        self, submodule_repo: Path, tmp_path: Path
    ) -> None:
        """ARCH-001 §11. An incomplete tree that still configures is the dangerous case:
        both models lose the same entities and agree. Without the transport relaxation above
        the clone fails, which stands in for the offline case."""
        worktree = tmp_path / "wt"
        git(submodule_repo, "worktree", "add", "--detach", "--quiet", str(worktree), "HEAD")
        with pytest.raises(CompileDatabaseError, match="submodules"):
            materialize_submodules(worktree, "HEAD")

    def test_a_repository_without_submodules_is_untouched(self, diverged_repo: Path) -> None:
        """No `.gitmodules`, no subprocess -- the common case pays nothing."""
        materialize_submodules(diverged_repo, "HEAD")


class TestRenamesAreResolvedFromGit:
    """ARCH-001 §5: renames are an *input* to matching, resolved from git before it runs.

    Clang writes the source file name into the USR of every internal-linkage entity, so a
    renamed file breaks tier-1 identity for all of them at once. The matcher may not infer
    the rename itself -- that would derive identity from the content whose identity is in
    question -- so this is the stage that has to know.
    """

    def test_a_rename_is_reported_with_both_paths(self, renamed_repo: Path) -> None:
        revs = resolve_revs(renamed_repo)
        assert file_changes(renamed_repo, revs).renames == {
            "src/util.cpp": "src/support.cpp",
            "src/tool.cpp": "lib/tool.cpp",
        }

    def test_a_directory_move_is_a_rename_too(self, renamed_repo: Path) -> None:
        """`by_path` exists for this shape and is unreachable without it."""
        revs = resolve_revs(renamed_repo)
        renames = file_changes(renamed_repo, revs).renames
        assert renames["src/tool.cpp"] == "lib/tool.cpp"

    def test_the_change_surface_is_unchanged(self, diverged_repo: Path) -> None:
        """`changed_files` now reads through the new parser, and must still agree with git.

        The equivalence is what lets one parser serve both callers; without this test a
        later edit to the record walker could quietly change what every review analyzes.
        """
        revs = resolve_revs(diverged_repo)
        expected = git(diverged_repo, "diff", "--name-only", f"{revs.base_sha}..{revs.head_sha}")
        assert changed_files(diverged_repo, revs) == expected.splitlines()

    def test_a_rename_reports_only_the_destination_as_changed(self, renamed_repo: Path) -> None:
        """What `--name-only` prints, so the compression denominator does not move."""
        revs = resolve_revs(renamed_repo)
        paths = file_changes(renamed_repo, revs).paths
        assert "src/support.cpp" in paths
        assert "src/util.cpp" not in paths

    def test_rename_detection_does_not_depend_on_git_config(self, renamed_repo: Path) -> None:
        """ARCH-001 §12: identity may not depend on whose `~/.gitconfig` the tool ran under.

        Detection is on by default, so relying on the default would pass everywhere except
        on the one reviewer's machine that has turned it off -- where the tool would report
        every internal-linkage entity in a renamed file as deleted and re-added.
        """
        git(renamed_repo, "config", "diff.renames", "false")
        revs = resolve_revs(renamed_repo)
        assert file_changes(renamed_repo, revs).renames == {
            "src/util.cpp": "src/support.cpp",
            "src/tool.cpp": "lib/tool.cpp",
        }

    def test_a_copy_is_not_a_rename(self, bare_repo: Path) -> None:
        """`--find-copies` is deliberately not passed.

        A copy leaves the base file in place. Rewriting the base-side USR to the copy's name
        would claim the original moved -- a node-identity judgement made from a file-level
        heuristic, which is the inversion ARCH-001 §5 exists to prevent.
        """
        body = "static int helper() { return 41 + 1; }\n" * 8
        commit(bare_repo, "src/original.cpp", body, "A")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        commit(bare_repo, "src/duplicate.cpp", body, "copy it")

        revs = resolve_revs(bare_repo)
        changes = file_changes(bare_repo, revs)
        assert changes.renames == {}
        assert changes.paths == ["src/duplicate.cpp"]

    def test_the_similarity_threshold_is_a_knob(self, bare_repo: Path) -> None:
        """ARCH-001 §14 asks for the calibration surface to exist before it is needed.

        A file renamed *and* substantially edited is a rename at git's default and not one
        at a strict threshold. Both answers are defensible, which is the point: the tool has
        to be able to state which it asked for.
        """
        commit(
            bare_repo,
            "src/util.cpp",
            "".join(f"int line{i}() {{ return {i}; }}\n" for i in range(20)),
            "A",
        )
        git(bare_repo, "checkout", "-q", "-b", "feature")
        git(bare_repo, "mv", "src/util.cpp", "src/support.cpp")
        (bare_repo / "src/support.cpp").write_text(
            "".join(f"int line{i}() {{ return {i}; }}\n" for i in range(12))
            + "".join(f"int fresh{i}() {{ return {i}; }}\n" for i in range(8)),
            encoding="utf-8",
        )
        git(bare_repo, "add", "-A")
        git(bare_repo, "commit", "-q", "-m", "rename and rework")

        revs = resolve_revs(bare_repo)
        assert file_changes(bare_repo, revs, rename_similarity=50).renames == {
            "src/util.cpp": "src/support.cpp"
        }
        assert file_changes(bare_repo, revs, rename_similarity=100).renames == {}

    def test_a_malformed_threshold_is_refused_not_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike `$CPPSD_WORKERS`, this one changes the answer rather than the speed."""
        monkeypatch.setenv(RENAME_SIMILARITY_ENV, "half")
        with pytest.raises(InvalidSettingError, match="not a percentage"):
            resolve_rename_similarity()

        monkeypatch.setenv(RENAME_SIMILARITY_ENV, "0")
        with pytest.raises(InvalidSettingError, match="percentage in 1"):
            resolve_rename_similarity()
        with pytest.raises(InvalidSettingError, match="percentage in 1"):
            resolve_rename_similarity(101)

        monkeypatch.setenv(RENAME_SIMILARITY_ENV, "50")
        assert resolve_rename_similarity() == 50
        assert resolve_rename_similarity(90) == 90, "an explicit request outranks the environment"

    def test_a_truncated_record_is_refused_not_shortened(
        self, diverged_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rename record missing its destination would understate what changed."""
        revs = resolve_revs(diverged_repo)
        monkeypatch.setattr(git_module, "run_git_z", lambda repo, *args: ["M", "a.cpp", "R100"])
        with pytest.raises(GitError, match="mid-record"):
            file_changes(diverged_repo, revs)

    def test_two_undecodable_paths_do_not_collapse_into_one(self, bare_repo: Path) -> None:
        """Why the rename query needs its own runner rather than `run_git`.

        `run_git` decodes with `errors="replace"`, which maps every undecodable byte to the
        same replacement character -- so `a\\xe9.cpp` and `a\\xff.cpp` become one string. In
        `changed_files` that is a mangled label. In a rename map it is a fabricated identity:
        two renames collapse to one entry, one of them is lost, and the survivor asserts that
        two different files are the same file. That is the confidently-wrong output ARCH-001
        §11 refuses, so the map is decoded with `surrogateescape` instead.

        Linux-only, like the rest of the project -- these are legal filenames here and not
        encodable on Windows at all.
        """
        src = bare_repo / "src"
        src.mkdir()
        for raw in (b"a\xe9.cpp", b"a\xff.cpp"):
            (src / os.fsdecode(raw)).write_bytes(b"int x();\n")
        git(bare_repo, "add", "-A")
        git(bare_repo, "commit", "-q", "-m", "A")

        git(bare_repo, "checkout", "-q", "-b", "feature")
        for old, new in ((b"a\xe9.cpp", b"b\xe9.cpp"), (b"a\xff.cpp", b"b\xff.cpp")):
            (src / os.fsdecode(old)).rename(src / os.fsdecode(new))
        git(bare_repo, "add", "-A")
        git(bare_repo, "commit", "-q", "-m", "rename both")

        revs = resolve_revs(bare_repo)
        renames = file_changes(bare_repo, revs).renames

        assert len(renames) == 2, "one entry means two different files were merged into one"
        assert renames[os.fsdecode(b"src/a\xe9.cpp")] == os.fsdecode(b"src/b\xe9.cpp")
        assert renames[os.fsdecode(b"src/a\xff.cpp")] == os.fsdecode(b"src/b\xff.cpp")

    @pytest.mark.fixtures
    def test_fmt_core_h_to_base_h(self) -> None:
        """The commit the fix will be measured against.

        `include/fmt/core.h` -> `include/fmt/base.h` is R100 -- byte-identical -- which is
        what makes it a clean experiment: the unnamed records in that header have USRs of
        the form `c:core.h@<byte-offset>@...`, and an R100 rename leaves the offsets alone,
        isolating the basename as the only variable.

        Git only, so this costs milliseconds: no worktree, no configure, no libclang.
        """
        repo = require_fixture("fmt")
        revs = RevPair(
            base_ref="79f1506f~1",
            head_ref="79f1506f",
            base_sha=git(repo, "rev-parse", "79f1506f~1"),
            head_sha=git(repo, "rev-parse", "79f1506f"),
        )
        changes = file_changes(repo, revs)

        assert changes.renames == {"include/fmt/core.h": "include/fmt/base.h"}
        assert len(changes.paths) == 11

    @pytest.mark.fixtures
    def test_fmt_posix_cc_to_os_cc(self) -> None:
        """The commit that actually demonstrates the defect.

        Unlike the two below, this one renames a file that *contains* internal-linkage
        entities -- `convert_rwcount` in an anonymous namespace. Before the map was fed, it
        produced 8 events for one unchanged function. The measurement depends on this
        rename being detected, so it is pinned here where it costs milliseconds rather than
        only in a full-pipeline run.
        """
        repo = require_fixture("fmt")
        revs = RevPair(
            base_ref="da256982~1",
            head_ref="da256982",
            base_sha=git(repo, "rev-parse", "da256982~1"),
            head_sha=git(repo, "rev-parse", "da256982"),
        )
        assert file_changes(repo, revs).renames == {"src/posix.cc": "src/os.cc"}

    @pytest.mark.fixtures
    def test_fmt_core_test_to_base_test(self) -> None:
        repo = require_fixture("fmt")
        revs = RevPair(
            base_ref="f924d20d~1",
            head_ref="f924d20d",
            base_sha=git(repo, "rev-parse", "f924d20d~1"),
            head_sha=git(repo, "rev-parse", "f924d20d"),
        )
        changes = file_changes(repo, revs)

        assert changes.renames == {"test/core-test.cc": "test/base-test.cc"}
        assert changes.paths == ["test/CMakeLists.txt", "test/base-test.cc"]
