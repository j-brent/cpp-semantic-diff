"""Revision resolution -- ARCH-001 §3.1."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from conftest import commit, git, require_fixture
from cpp_semantic_diff.acquire import git as git_module
from cpp_semantic_diff.acquire.git import (
    RENAME_SIMILARITY_ENV,
    RenameLimitHit,
    RevPair,
    changed_files,
    detect_default_branch,
    diff_line_count,
    file_changes,
    is_git_repository,
    is_shallow,
    rename_limit_hit,
    resolve_rename_similarity,
    resolve_revs,
)
from cpp_semantic_diff.acquire.workspace import materialize_submodules
from cpp_semantic_diff.errors import (
    CompileDatabaseError,
    GitError,
    GitNotFoundError,
    InvalidSettingError,
    NoMergeBaseError,
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

    def test_unrelated_histories_have_no_merge_base(self, bare_repo: Path) -> None:
        """ADR-008's base is `merge-base(main, HEAD)`, which does not exist for two root
        commits with no shared ancestor -- an orphan branch, or a base ref picked by
        mistake. `git merge-base` exits 1 with empty stdout for this, and nothing downstream
        can turn that into a sensible answer, so it must surface as a precondition failure
        rather than, say, an empty sha silently treated as valid.
        """
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        git(bare_repo, "checkout", "-q", "--orphan", "feature")
        git(bare_repo, "rm", "-q", "-f", "a.cpp")
        commit(bare_repo, "c.cpp", "int c();\n", "C")

        with pytest.raises(NoMergeBaseError, match="share no history"):
            resolve_revs(bare_repo, base_ref="main")


class TestDefaultBranchDetection:
    def test_prefers_main(self, diverged_repo: Path) -> None:
        assert detect_default_branch(diverged_repo) == "main"

    def test_falls_back_to_master(self, bare_repo: Path) -> None:
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        git(bare_repo, "branch", "-m", "main", "master")
        assert detect_default_branch(bare_repo) == "master"

    def test_origin_head_outranks_the_local_candidate_list(self, bare_repo: Path) -> None:
        """The docstring's order is "origin/HEAD if the remote published one, else the first
        candidate" -- origin/HEAD is checked *first*. A clone's remote can disagree about
        which branch is the default with whatever `main`/`master` happens to exist locally --
        a stale local branch left over from a rename, say -- and the remote's own answer must
        win. `symbolic-ref` and `update-ref` reproduce exactly what `git clone` leaves behind,
        without a real remote.
        """
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        git(bare_repo, "checkout", "-q", "-b", "trunk")
        commit(bare_repo, "b.cpp", "int b();\n", "B")
        git(bare_repo, "checkout", "-q", "main")

        git(
            bare_repo, "update-ref", "refs/remotes/origin/main", git(bare_repo, "rev-parse", "main")
        )
        git(
            bare_repo,
            "update-ref",
            "refs/remotes/origin/trunk",
            git(bare_repo, "rev-parse", "trunk"),
        )
        git(bare_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

        assert detect_default_branch(bare_repo) == "origin/trunk"

    def test_falls_back_to_a_remote_branch_with_no_local_counterpart(self, bare_repo: Path) -> None:
        """A fresh clone with no `origin/HEAD` recorded (some hosts and shallow fetches omit
        it) and no local `main`/`master` -- only the remote-tracking ref exists. Reached by
        the `for ref in (candidate, f"origin/{candidate}")` fallback, which the other tests
        in this class never exercise because they always have a local candidate branch.
        """
        commit(bare_repo, "a.cpp", "int a();\n", "A")
        git(bare_repo, "branch", "-m", "main", "trunk-local")
        git(
            bare_repo,
            "update-ref",
            "refs/remotes/origin/main",
            git(bare_repo, "rev-parse", "trunk-local"),
        )

        assert detect_default_branch(bare_repo) == "origin/main"


class TestCompressionDenominator:
    def test_counts_added_and_deleted_lines(self, diverged_repo: Path) -> None:
        revs = resolve_revs(diverged_repo)
        # c.cpp is one added line, and nothing else is in scope.
        assert diff_line_count(diverged_repo, revs) == 1

    def test_empty_range_is_zero(self, diverged_repo: Path) -> None:
        revs = resolve_revs(diverged_repo, base_ref="HEAD")
        assert revs.is_empty
        assert diff_line_count(diverged_repo, revs) == 0

    def test_a_binary_file_does_not_count_as_lines(self, bare_repo: Path) -> None:
        """`git diff --numstat` prints `-` for both columns on a binary file, which the
        `added == "-" or deleted == "-"` guard skips. Getting this wrong inflates the
        compression denominator (§10) by however large the binary is, silently -- the
        number just looks a little different, never a crash or an empty result.
        """
        commit(bare_repo, "img.bin", "hello\x00world", "A")
        git(bare_repo, "checkout", "-q", "-b", "feature")
        commit(bare_repo, "img.bin", "hello\x00WORLD!!", "edit the binary")
        commit(bare_repo, "real.cpp", "int real();\n", "add a real line")

        revs = resolve_revs(bare_repo)
        assert diff_line_count(bare_repo, revs) == 1, "only real.cpp's one added line should count"


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
        monkeypatch.setattr(
            git_module, "run_git_z", lambda repo, *args: (["M", "a.cpp", "R100"], "")
        )
        with pytest.raises(GitError, match="mid-record"):
            file_changes(diverged_repo, revs)

    def test_an_unrecognised_status_letter_is_one_path_not_a_refusal(
        self, diverged_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`X` and friends -- git's own symbol for "I could not figure out what this is" --
        are explicitly not R or C, so the parser takes the one-path branch rather than
        raising. The comment in `file_changes` states the reasoning: a git that grows a new
        status letter should not stop a review it does not otherwise affect. Without this
        test a future change that narrows the one-path branch to a fixed set of letters
        would silently turn "unknown status" into "crashes every run against a newer git".
        """
        revs = resolve_revs(diverged_repo)
        monkeypatch.setattr(
            git_module, "run_git_z", lambda repo, *args: (["X", "puzzling.cpp"], "")
        )
        changes = file_changes(diverged_repo, revs)
        assert changes.paths == ["puzzling.cpp"]
        assert changes.renames == {}

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


class TestATruncatedRenameMapIsReported:
    """ARCH-001 §11 rule 2. Past `diff.renameLimit`, git skips inexact rename
    detection, exits 0 and says so only on stderr. A rename map that is silently short turns
    every file-scoped entity in a renamed-and-edited file into a removal plus an addition, and
    nothing in the output would say why. The cap is not lifted (`-l0` is potentially quadratic
    on the diffs that hit it); the truncation is reported."""

    @staticmethod
    def _two_inexact_renames(repo: Path) -> RevPair:
        for name in ("a", "b"):
            body = "".join(f"int {name}{i}() {{ return {i}; }}\n" for i in range(30))
            commit(repo, f"src/{name}.cpp", body, f"add {name}")
        git(repo, "checkout", "-q", "-b", "feature")
        for name in ("a", "b"):
            git(repo, "mv", f"src/{name}.cpp", f"src/{name}2.cpp")
            with (repo / f"src/{name}2.cpp").open("a", encoding="utf-8") as fh:
                fh.write("int extra();\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "rename and edit both")
        return resolve_revs(repo)

    def test_within_the_limit_nothing_is_reported(self, bare_repo: Path) -> None:
        revs = self._two_inexact_renames(bare_repo)
        changes = file_changes(bare_repo, revs)
        assert changes.renames == {"src/a.cpp": "src/a2.cpp", "src/b.cpp": "src/b2.cpp"}
        assert changes.rename_limit is None
        assert changes.missed_rename_files == frozenset()

    def test_past_the_limit_the_truncation_is_reported(self, bare_repo: Path) -> None:
        revs = self._two_inexact_renames(bare_repo)
        git(bare_repo, "config", "diff.renameLimit", "1")
        changes = file_changes(bare_repo, revs)
        assert changes.renames == {}, "the gap being reported must actually exist"
        assert changes.rename_limit == RenameLimitHit(needed=2)
        assert changes.missed_rename_files == {
            "src/a.cpp",
            "src/a2.cpp",
            "src/b.cpp",
            "src/b2.cpp",
        }

    def test_an_exact_rename_survives_the_limit_that_truncates_an_inexact_one(
        self, bare_repo: Path
    ) -> None:
        """The realistic case: one branch hits the cap and loses *some* of its renames, not
        all of them. Git finds an exact (byte-identical, R100) rename through its cheap
        content-hash pass regardless of `diff.renameLimit` -- only the expensive
        similarity-scoring pass that catches a renamed-and-edited file is what gets skipped.
        A map that reports zero renames whenever the limit is hit (as both tests above do,
        for simplicity) would hide that the two failure modes coexist in one diff: some
        renames still resolve correctly, and only the genuinely-lost ones should end up in
        `missed_rename_files`.
        """
        commit(bare_repo, "src/exact.cpp", "int exact() { return 0; }\n", "add exact")
        self._two_inexact_renames(bare_repo)  # branches to "feature" and leaves it checked out
        git(bare_repo, "mv", "src/exact.cpp", "src/exact2.cpp")
        git(bare_repo, "commit", "-q", "-m", "rename exact unchanged")
        revs = resolve_revs(bare_repo)

        git(bare_repo, "config", "diff.renameLimit", "1")
        changes = file_changes(bare_repo, revs)

        assert changes.rename_limit is not None, "the inexact pass must actually be skipped"
        assert changes.renames == {"src/exact.cpp": "src/exact2.cpp"}, (
            "the exact rename is not inexact detection, so the cap does not touch it"
        )
        assert changes.missed_rename_files == {"src/a.cpp", "src/a2.cpp", "src/b.cpp", "src/b2.cpp"}
        assert "src/exact.cpp" not in changes.missed_rename_files
        assert "src/exact2.cpp" not in changes.missed_rename_files

    def test_the_query_runs_in_the_c_locale(
        self, bare_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning is read from stderr, so a translated git would hide it. The call is
        made in the C locale whatever the reviewer's is, with `LANGUAGE` removed."""
        seen: list[Mapping[str, str] | None] = []
        real = git_module._run

        def spy(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            seen.append(kw.get("env"))
            return real(argv, **kw)

        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        monkeypatch.setenv("LANGUAGE", "de")
        monkeypatch.setattr(git_module, "_run", spy)
        git_module.run_git_z(bare_repo, "status", "-z")

        (env,) = seen
        assert env is not None
        assert env["LC_ALL"] == "C"
        assert "LANGUAGE" not in env

    def test_the_message_git_prints_is_the_one_read(self, bare_repo: Path) -> None:
        """Pins git's own wording, in the C locale, so a git that rewords it fails here
        rather than going quiet in production."""
        revs = self._two_inexact_renames(bare_repo)
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(bare_repo),
                "-c",
                "diff.renameLimit=1",
                "diff",
                "--name-status",
                "--find-renames=50%",
                "-z",
                f"{revs.base_sha}..{revs.head_sha}",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        assert "rename detection was skipped" in proc.stderr
        assert "diff.renameLimit" in proc.stderr
        assert rename_limit_hit(proc.stderr) == RenameLimitHit(needed=2)

    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            ("", None),
            ("warning: something unrelated\n", None),
            # git 2.43's wording
            (
                "warning: exhaustive rename detection was skipped due to too many files.\n"
                "warning: you may want to set your diff.renameLimit variable to at least 1234 "
                "and retry the command.\n",
                RenameLimitHit(needed=1234),
            ),
            # older git's wording
            (
                "warning: inexact rename detection was skipped due to too many files.\n"
                "warning: you may want to set your diff.renamelimit variable to at least 9 "
                "and retry the command.\n",
                RenameLimitHit(needed=9),
            ),
            # the skip without the advice line still counts
            ("warning: inexact rename detection was skipped\n", RenameLimitHit(needed=None)),
        ],
    )
    def test_the_match_is_tolerant(self, stderr: str, expected: RenameLimitHit | None) -> None:
        assert rename_limit_hit(stderr) == expected
