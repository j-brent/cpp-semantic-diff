"""Revision resolution -- ARCH-001 §3.1."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import git
from cpp_semantic_diff.acquire.git import (
    changed_files,
    detect_default_branch,
    diff_line_count,
    is_shallow,
    resolve_revs,
)
from cpp_semantic_diff.errors import (
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


class TestDefaultBranchDetection:
    def test_prefers_main(self, diverged_repo: Path) -> None:
        assert detect_default_branch(diverged_repo) == "main"

    def test_falls_back_to_master(self, bare_repo: Path) -> None:
        from conftest import commit

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
