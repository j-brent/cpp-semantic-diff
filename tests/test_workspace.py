"""Materializing a revision -- ARCH-001 §3.1, and the worktree cache of §9."""

from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import commit, git
from cpp_semantic_diff.acquire import workspace
from cpp_semantic_diff.acquire.workspace import (
    CompileDatabase,
    CompileEntry,
    cache_root,
    checkout,
    configure,
    is_rooted,
    require_this_checkout,
    select_generator,
)
from cpp_semantic_diff.errors import (
    CompileDatabaseError,
    ConfigureError,
    GitNotFoundError,
)


@pytest.fixture(autouse=True)
def _cache_root_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may create worktrees in the developer's real cache."""
    monkeypatch.setenv("CPPSD_CACHE_DIR", str(tmp_path / "cppsd-cache"))


@pytest.fixture
def one_file_repo(bare_repo: Path) -> Path:
    commit(bare_repo, "a.cpp", "int a() { return 1; }\n", "A")
    return bare_repo


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def fake_configure(worktree: Path, rev: str, extra_args: list[str] | None = None) -> Path:
    """What a successful CMake configure leaves behind, without needing CMake.

    The tests that use it are about the cache's bookkeeping, not about CMake, and would
    otherwise be skipped wherever CMake or a compiler is missing.
    """
    database = worktree / "build" / "compile_commands.json"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_text(
        json.dumps(
            [
                {
                    "directory": str(worktree / "build"),
                    "file": str(worktree / "a.cpp"),
                    "arguments": ["c++", "-c", str(worktree / "a.cpp")],
                }
            ]
        ),
        encoding="utf-8",
    )
    return database


class TestAnUnfinishedWorktreeIsNotReused:
    """A worktree that was created but never finished must be rebuilt, never trusted.

    The dangerous case is `git worktree add` succeeding and submodule checkout failing --
    offline, or interrupted. The directory exists, so a check for existence alone would skip
    the submodules on the next run and configure an incomplete tree: two models that agree on
    the same missing entities, which is the silent truncation ARCH-001 §11 refuses.
    """

    def test_a_worktree_without_its_completion_marker_is_rebuilt(
        self, one_file_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(workspace, "configure", fake_configure)
        rev = head(one_file_repo)
        worktree = cache_root() / "worktrees" / one_file_repo.name / rev

        # What an interrupted run leaves: the tree, and nothing saying it was finished.
        worktree.parent.mkdir(parents=True)
        git(one_file_repo, "worktree", "add", "--detach", "--quiet", str(worktree), rev)
        (worktree / "left-by-the-interrupted-run").write_text("", encoding="utf-8")

        with checkout(one_file_repo, rev) as co:
            assert not (co.path / "left-by-the-interrupted-run").exists(), "it was reused"
            assert (co.path / "a.cpp").is_file()
        assert worktree.with_name(f"{rev}.complete").is_file()

    def test_a_failure_part_way_leaves_nothing_the_next_run_trusts(
        self, one_file_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(workspace, "configure", fake_configure)
        rev = head(one_file_repo)
        calls: list[Path] = []

        def offline(worktree: Path, rev: str) -> None:
            calls.append(worktree)
            if len(calls) == 1:
                raise CompileDatabaseError(rev, str(worktree), "submodules: offline")

        monkeypatch.setattr(workspace, "materialize_submodules", offline)

        with pytest.raises(CompileDatabaseError), checkout(one_file_repo, rev):
            pass
        with checkout(one_file_repo, rev):
            pass

        assert len(calls) == 2, "the second run skipped the step the first one never finished"

    def test_a_finished_worktree_is_reused(
        self, one_file_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cache still does its job (ARCH-001 §9): the base is built once."""
        monkeypatch.setattr(workspace, "configure", fake_configure)
        rev = head(one_file_repo)
        with checkout(one_file_repo, rev) as co:
            (co.path / "kept").write_text("", encoding="utf-8")
        with checkout(one_file_repo, rev) as co:
            assert (co.path / "kept").exists()

    def test_the_marker_is_outside_the_checkout(
        self, one_file_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inside, it would be an untracked file in the tree being analyzed."""
        monkeypatch.setattr(workspace, "configure", fake_configure)
        rev = head(one_file_repo)
        with checkout(one_file_repo, rev) as co:
            untracked = git(co.path, "status", "--porcelain", "--untracked-files=all")
            assert "complete" not in untracked
            assert co.path.with_name(f"{rev}.complete").is_file()


class TestConcurrentRunsTakeTurns:
    def test_the_lock_is_held_while_building_and_released_for_analysis(
        self, one_file_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Held around create-and-configure, so two runs cannot both create the worktree or
        one read a database the other is writing; released before the checkout is handed
        out, so reviews of the same base are not serialized for their whole length."""
        rev = head(one_file_repo)
        lock = cache_root() / "worktrees" / one_file_repo.name / f"{rev}.lock"
        held_during_configure: list[bool] = []

        def probing_configure(
            worktree: Path, rev: str, extra_args: list[str] | None = None
        ) -> Path:
            held_during_configure.append(not _can_lock(lock))
            return fake_configure(worktree, rev, extra_args)

        monkeypatch.setattr(workspace, "configure", probing_configure)
        with checkout(one_file_repo, rev):
            assert _can_lock(lock), "the lock is still held while the checkout is in use"
        assert held_during_configure == [True]


def _can_lock(path: Path) -> bool:
    """Whether a fresh open of `path` can take the lock -- `flock` conflicts per open file."""
    with path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


class TestTheCacheRootIsAbsolute:
    """Python and `git -C <repo>` resolve a relative path against different directories."""

    def test_a_relative_xdg_cache_home_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CPPSD_CACHE_DIR")
        monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
        assert cache_root() == Path.home() / ".cache" / "cpp-semantic-diff"

    def test_an_absolute_xdg_cache_home_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CPPSD_CACHE_DIR")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert cache_root() == tmp_path / "cpp-semantic-diff"

    def test_a_relative_override_is_made_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CPPSD_CACHE_DIR", "here")
        assert cache_root() == tmp_path.resolve() / "here"

    def test_the_current_directory_is_keyed_by_its_name(
        self, one_file_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`Path('.').name` is empty; `cppsd diff .` must not share one folder across repos."""
        monkeypatch.setattr(workspace, "configure", fake_configure)
        monkeypatch.chdir(one_file_repo)
        with checkout(Path("."), head(one_file_repo)) as co:
            assert co.path.parent.name == one_file_repo.name


class TestAMalformedCompileDatabaseIsRefused:
    """A hand-made or non-CMake database is input the user can fix, so it is reported as a
    compile-database problem rather than as a traceback from the JSON parser."""

    @pytest.mark.parametrize(
        "content",
        [
            "{ not json",
            json.dumps({"directory": "/b", "file": "/s/a.cpp"}),
            json.dumps([{"file": "/s/a.cpp", "arguments": []}]),
            json.dumps([{"directory": "/b", "arguments": []}]),
            json.dumps([{"directory": "/b", "file": "/s/a.cpp"}]),
            json.dumps(["not an object"]),
        ],
        ids=["invalid-json", "not-a-list", "no-directory", "no-file", "no-command", "not-object"],
    )
    def test_refused(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "compile_commands.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(CompileDatabaseError, match="abc123"):
            CompileDatabase.load(path, rev="abc123")


class TestIsRooted:
    """`is_rooted` is the one thing standing between a cross-platform path and a path to
    nothing: `Path.is_absolute()` answers only for the platform running the code, so a
    Windows-generated database read on this (Linux) host would otherwise look relative and
    get silently joined onto the build directory."""

    def test_recognises_either_path_flavour_as_absolute(self) -> None:
        assert is_rooted("/posix/absolute/path.cpp")
        assert is_rooted("C:\\windows\\absolute\\path.cpp")
        assert is_rooted("C:/windows/absolute/path.cpp")

    def test_a_relative_path_is_neither(self) -> None:
        assert not is_rooted("relative/path.cpp")
        assert not is_rooted("just-a-file.cpp")

    def test_a_windows_absolute_file_is_not_joined_onto_the_directory(self, tmp_path: Path) -> None:
        """What `is_rooted` exists to prevent: on POSIX, a Windows path is not absolute by
        `Path.is_absolute()`'s own reckoning, so without the flavour-agnostic check this
        entry would be read as `directory / file` -- a nonsense path built out of two
        absolute paths glued together -- rather than kept as the absolute path it names."""
        path = tmp_path / "compile_commands.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "directory": "C:\\build",
                        "file": "C:\\project\\a.cpp",
                        "arguments": ["cl", "/c", "C:\\project\\a.cpp"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        db = CompileDatabase.load(path, rev="abc123")
        assert str(db.entries[0].file) == "C:\\project\\a.cpp"


class TestADatabaseFromAnotherCheckoutIsRefused:
    def test_a_committed_database_with_foreign_paths(self, bare_repo: Path) -> None:
        """Every TU outside the worktree means every declaration is pruned while every TU
        reports that it parsed: an empty model that claims to be complete. Two of them agree
        that nothing changed."""
        commit(bare_repo, "a.cpp", "int a() { return 1; }\n", "A")
        foreign = [
            {
                "directory": "/home/someone-else/project/build",
                "file": "/home/someone-else/project/a.cpp",
                "arguments": ["c++", "-c", "/home/someone-else/project/a.cpp"],
            }
        ]
        commit(bare_repo, "compile_commands.json", json.dumps(foreign), "commit the database")
        rev = head(bare_repo)

        with (
            pytest.raises(CompileDatabaseError, match="different checkout"),
            checkout(bare_repo, rev),
        ):
            pass
        marker = cache_root() / "worktrees" / bare_repo.name / f"{rev}.complete"
        assert not marker.exists(), "a refused checkout must not be marked complete"

    def test_some_entries_outside_the_worktree_is_ordinary(self, tmp_path: Path) -> None:
        """Only "none inside" is refused -- a build that compiles one file from a sibling
        checkout is unremarkable and must stay visible in the output. That asymmetry is the
        whole point of `require_this_checkout`; until now only the "all outside" half of it
        had a test, so a change that tightened the check to "any outside" would pass the
        suite while silently refusing every ordinary partial build.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        inside = worktree / "a.cpp"
        inside.write_text("int a();\n", encoding="utf-8")
        db = CompileDatabase(
            path=tmp_path / "compile_commands.json",
            entries=[
                CompileEntry(file=inside, directory=worktree, arguments=["c++", "-c", str(inside)]),
                CompileEntry(
                    file=Path("/elsewhere/b.cpp"),
                    directory=Path("/elsewhere"),
                    arguments=["c++", "-c", "/elsewhere/b.cpp"],
                ),
            ],
        )
        require_this_checkout(db, worktree, "abc123")  # must not raise


class TestConfigureFailsAsABuildProblem:
    def test_a_missing_cmake_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "CMakeLists.txt").write_text("project(x CXX)\n", encoding="utf-8")
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        with pytest.raises(CompileDatabaseError, match="CMake is not installed"):
            configure(tmp_path, "abc123")

    def test_a_timeout_is_a_configure_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "CMakeLists.txt").write_text("project(x CXX)\n", encoding="utf-8")
        monkeypatch.setattr(
            "cpp_semantic_diff.acquire.workspace.shutil.which", lambda name: f"/usr/bin/{name}"
        )

        def too_slow(argv: list[str], **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(argv, workspace.CONFIGURE_TIMEOUT_SECONDS)

        monkeypatch.setattr("cpp_semantic_diff.acquire.workspace.subprocess.run", too_slow)
        with pytest.raises(ConfigureError, match="did not finish"):
            configure(tmp_path, "abc123")

    def test_cmake_generator_in_the_environment_is_respected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CMake reads it natively; an explicit `-G Ninja` would silently override it."""
        monkeypatch.setenv("CMAKE_GENERATOR", "Unix Makefiles")
        assert select_generator(None) == []

    def test_an_explicit_generator_flag_is_also_respected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command-line twin of the environment-variable case above: `--configure-arg
        -G Xcode` must win over the tool's own default just as plainly. Forcing ninja to
        appear available proves the `-G` check is what stops it, not an absent ninja."""
        monkeypatch.setattr(
            "cpp_semantic_diff.acquire.workspace.shutil.which", lambda name: f"/usr/bin/{name}"
        )
        assert select_generator(["-G", "Xcode"]) == []
        assert select_generator(["-GXcode"]) == []

    def test_a_build_failure_is_reported_not_raised_as_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-010: a base that does not build is a hard failure, with no partial mode.
        This is the exit code CMake actually uses to say so -- the sibling tests in this
        class cover cmake being absent or hanging, never cmake running and refusing."""
        (tmp_path / "CMakeLists.txt").write_text("project(x CXX)\n", encoding="utf-8")
        monkeypatch.setattr(
            "cpp_semantic_diff.acquire.workspace.shutil.which", lambda name: f"/usr/bin/{name}"
        )

        def broken(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr='CMake Error: Unknown CMake command "nonsense".\n'
            )

        monkeypatch.setattr("cpp_semantic_diff.acquire.workspace.subprocess.run", broken)
        with pytest.raises(ConfigureError, match="Unknown CMake command"):
            configure(tmp_path, "abc123")

    def test_a_generator_that_cannot_emit_a_database_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ARCH-001 §3.1's central claim about this stage: Visual Studio and Xcode configure
        *successfully* and emit nothing, turning a hard precondition into a silent one
        unless something checks for the database's absence after a clean configure. Neither
        test file exercised this path before -- every other configure test fails for a
        reason CMake itself reports.
        """
        (tmp_path / "CMakeLists.txt").write_text("project(x CXX)\n", encoding="utf-8")
        monkeypatch.setattr(
            "cpp_semantic_diff.acquire.workspace.shutil.which",
            lambda name: "/usr/bin/cmake" if name == "cmake" else None,
        )
        build_dir = tmp_path / "build"

        def configures_but_emits_nothing(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "CMakeCache.txt").write_text(
                "CMAKE_GENERATOR:INTERNAL=Xcode\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(
            "cpp_semantic_diff.acquire.workspace.subprocess.run", configures_but_emits_nothing
        )
        with pytest.raises(CompileDatabaseError, match="Ninja"):
            configure(tmp_path, "abc123")

    def test_a_stale_incompatible_generator_is_discarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ARCH-001 §3.1: configure "discards a cached build tree whose generator cannot
        emit one." CMake refuses to switch generators on an existing cache, so a build
        directory left over from a Visual Studio or Xcode configure would otherwise fail to
        reconfigure in place, and every later run would hit the same wall forever.
        """
        (tmp_path / "CMakeLists.txt").write_text("project(x CXX)\n", encoding="utf-8")
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / "CMakeCache.txt").write_text(
            "CMAKE_GENERATOR:INTERNAL=Xcode\n", encoding="utf-8"
        )
        (build_dir / "stale-from-xcode").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "cpp_semantic_diff.acquire.workspace.shutil.which", lambda name: f"/usr/bin/{name}"
        )

        def reconfigures_cleanly(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            assert not (build_dir / "stale-from-xcode").exists(), (
                "the stale build tree was reconfigured in place instead of discarded first"
            )
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "compile_commands.json").write_text("[]", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(
            "cpp_semantic_diff.acquire.workspace.subprocess.run", reconfigures_cleanly
        )
        database = configure(tmp_path, "abc123")
        assert database == build_dir / "compile_commands.json"


class TestSubmodulesWithoutGit:
    def test_a_missing_git_is_named(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".gitmodules").write_text("", encoding="utf-8")
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        with pytest.raises(GitNotFoundError):
            workspace.materialize_submodules(tmp_path, "abc123")


@pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("c++") is None,
    reason="needs cmake and a C++ compiler",
)
def test_a_real_configure_produces_a_checkout_inside_the_worktree(bare_repo: Path) -> None:
    """The whole path with nothing faked: the database CMake writes passes the
    belongs-to-this-checkout test, and the worktree is marked finished."""
    commit(bare_repo, "a.cpp", "int a() { return 1; }\n", "A")
    commit(
        bare_repo,
        "CMakeLists.txt",
        "cmake_minimum_required(VERSION 3.10)\nproject(x CXX)\nadd_library(x a.cpp)\n",
        "cmake",
    )
    rev = head(bare_repo)
    with checkout(bare_repo, rev) as co:
        assert [e.file.name for e in co.cpp_entries] == ["a.cpp"]
    assert (cache_root() / "worktrees" / bare_repo.name / f"{rev}.complete").is_file()
