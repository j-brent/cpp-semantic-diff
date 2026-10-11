"""The command line's contract with whoever runs it (ADR-033).

Every failure reaches the user as a message and a distinct exit code, never a traceback: a
caller such as a CI job branches on the code, and a person reads the message and knows what
to fix. The one exception is a defect in `cppsd` itself, which exits 1 with its traceback,
because that traceback is the most useful report of the defect there is.

`diff --json` is a product (ARCH-001 §10): stdout carries exactly one JSON document, and
everything else -- progress, warnings -- goes to stderr, so the output can be piped.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner, Result

import cpp_semantic_diff.produce as produce
import cpp_semantic_diff.render as render_module
from conftest import commit, git
from cpp_semantic_diff import __version__, cli
from cpp_semantic_diff.errors import ExitCode, GitError
from cpp_semantic_diff.model import Model
from cpp_semantic_diff.produce.toolchain import Toolchain
from cpp_semantic_diff.render import DEFAULT_LIMIT

runner = CliRunner()


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may read or write the developer's real cache."""
    monkeypatch.setenv("CPPSD_CACHE_DIR", str(tmp_path / "cppsd-cache"))


def flat(text: str) -> str:
    """`text` with its whitespace collapsed, since rich wraps long lines at the console width."""
    return " ".join(text.split())


def assert_reported(result: Result, code: ExitCode, *fragments: str) -> None:
    """The command exited with `code`, said `fragments` on stderr, and raised nothing else.

    A `SystemExit` is how an exit code leaves Click; any other exception escaping is the
    traceback this contract forbids.
    """
    assert result.exit_code == code, (result.exit_code, result.stdout, result.stderr)
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert "Traceback" not in result.output
    for fragment in fragments:
        assert fragment in flat(result.stderr), result.stderr


def run_module(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """`cppsd` in a fresh interpreter, for what one process can only do once."""
    return subprocess.run(
        [sys.executable, "-m", "cpp_semantic_diff.cli", *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=120,
    )


@functools.cache
def _libclang_problem() -> str | None:
    """Why this host cannot parse, or None. Asked once, in a subprocess, because loading
    libclang is once per process and a failed load must not poison this one."""
    proc = subprocess.run(
        [sys.executable, "-c", "from cpp_semantic_diff.produce import load; load()"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return None if proc.returncode == 0 else (proc.stderr.strip().splitlines() or ["?"])[-1]


def require_libclang() -> None:
    """`diff` verifies the parser before anything else, so without one it never gets far."""
    if (problem := _libclang_problem()) is not None:
        pytest.skip(f"no usable libclang on this host: {problem}")


def require_cmake() -> None:
    if shutil.which("cmake") is None or shutil.which("c++") is None:
        pytest.skip("needs cmake and a C++ compiler")


@pytest.fixture
def branch_without_build(bare_repo: Path) -> Path:
    """A branch with a C++ change and nothing that says how to compile it."""
    commit(bare_repo, "a.cpp", "int a() { return 1; }\n", "A")
    git(bare_repo, "checkout", "-q", "-b", "feature")
    commit(bare_repo, "a.cpp", "int a() { return 2; }\n", "change a")
    return bare_repo


@pytest.fixture
def repo_with_vendor_dir(bare_repo: Path) -> Path:
    """A `vendor/dep` directory, present and unchanged on both sides of a branch."""
    commit(bare_repo, "vendor/dep/dep.h", "// vendored\n", "add vendor/dep")
    commit(bare_repo, "a.cpp", "int a() { return 1; }\n", "A")
    git(bare_repo, "checkout", "-q", "-b", "feature")
    commit(bare_repo, "a.cpp", "int a() { return 2; }\n", "change a")
    return bare_repo


def mock_pipeline(
    monkeypatch: pytest.MonkeyPatch, base_model: Model, head_model: Model
) -> list[dict[str, object]]:
    """Replace the two stages that need a system libclang and a compile database --
    `produce.load` and `produce.build_model` -- with the two models given, and `checkout`
    with a bare stand-in that does no real `git worktree add`.

    Everything else `diff` does is real: resolving the revisions, computing the
    third-party set, matching, diffing, deriving, classifying, ranking and building the
    document. That is what makes this useful for a wiring test -- it runs the actual CLI
    command end to end against a plain git repository with no cmake and no libclang, and
    the gap it can see is exactly "does the flag the person typed reach the stage it is
    documented to reach", not "does that stage do the right thing with it" (covered where
    each stage has its own test file).

    Returns the keyword arguments `build_model` was called with, once per side, in call
    order (base, then head) -- the only way to see what the CLI resolved `--workers` and
    `--third-party` to, since neither name is in the JSON document.
    """
    monkeypatch.setattr(
        produce,
        "load",
        lambda: Toolchain(library=Path("/fake/libclang.so"), version="18.1.3", release="18.1.3"),
    )

    @contextmanager
    def fake_checkout(repo: Path, rev: str) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(rev=rev, path=repo, cpp_entries=[])

    monkeypatch.setattr(cli, "checkout", fake_checkout)

    models = iter([base_model, head_model])
    calls: list[dict[str, object]] = []

    def fake_build_model(checkout: object, **kwargs: object) -> Model:
        calls.append(kwargs)
        return next(models)

    monkeypatch.setattr(produce, "build_model", fake_build_model)
    return calls


# -- 0: success -------------------------------------------------------------------------------


def test_version_prints_the_package_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert_reported(result, ExitCode.OK)
    assert result.stdout.strip() == f"cppsd {__version__}"


def test_plan_reports_only_the_branchs_own_change(diverged_repo: Path) -> None:
    """main advanced after the fork; `plan` must still count only the branch's file."""
    result = runner.invoke(cli.app, ["plan", str(diverged_repo)])
    assert_reported(result, ExitCode.OK)
    assert re.search(r"files changed\s+1\b", result.stdout), result.stdout
    assert re.search(r"C\+\+ files\s+1\b", result.stdout), result.stdout


def test_plan_warns_rather_than_refuses_when_head_is_the_merge_base(bare_repo: Path) -> None:
    """`diff` refuses this exact condition with exit 3 (`EmptyComparisonError`, see
    `test_an_empty_comparison_is_refused_by_diff_with_exit_3` below); `plan` answers "what
    would be analyzed" even when the answer is nothing, so it must stay at exit 0 and only
    say so. The two commands disagreeing about whether an empty comparison is a failure
    would be a design regression that no other test here would catch, since each
    command's tests live in their own place."""
    commit(bare_repo, "a.cpp", "int a() { return 1; }\n", "A")
    result = runner.invoke(cli.app, ["plan", str(bare_repo)])
    assert_reported(result, ExitCode.OK)
    assert "head is the merge base" in flat(result.stdout)
    assert "nothing to review" in flat(result.stdout)


def test_cache_status_succeeds_on_an_empty_cache() -> None:
    result = runner.invoke(cli.app, ["cache", "status"])
    assert_reported(result, ExitCode.OK)
    assert "TU models" in result.stdout


def test_cache_gc_dry_run_succeeds_and_says_it_would_collect() -> None:
    result = runner.invoke(cli.app, ["cache", "gc", "--dry-run"])
    assert_reported(result, ExitCode.OK)
    assert "would collect" in result.stdout


def test_cache_gc_without_dry_run_says_collected_not_would_collect() -> None:
    """The one branch `--dry-run`'s own test cannot exercise: nothing to collect from an
    empty cache either way, so this is the only thing that tells the two modes apart."""
    result = runner.invoke(cli.app, ["cache", "gc"])
    assert_reported(result, ExitCode.OK)
    assert "collected" in result.stdout
    assert "would collect" not in result.stdout


@pytest.mark.usefixtures("warm_cache")
def test_acquire_reports_each_revisions_compile_database(cmake_branch: Path) -> None:
    """`acquire` is the only command whose own report nothing else here exercises: `doctor`
    and `plan` never touch a compile database, and `diff` folds this exact step into its
    own pipeline rather than running it standalone. Only a test that runs `acquire` by
    itself can tell whether its two-revision report is still right."""
    result = runner.invoke(cli.app, ["acquire", str(cmake_branch)])
    assert_reported(result, ExitCode.OK)
    assert result.stdout.count("C++ TUs of") == 2, "one line per revision, base and head"
    assert re.search(r"base\s+\w{12}", result.stdout)
    assert re.search(r"head\s+\w{12}", result.stdout)


# -- 2: input the caller can fix --------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        (["cache", "gc", "--older-than", "-5"], "--older-than"),
        (["diff", ".", "--limit", "-1"], "--limit"),
        (["diff", ".", "--workers", "0"], "--workers"),
    ],
    ids=["negative-age", "negative-limit", "zero-workers"],
)
def test_an_option_out_of_range_is_a_usage_error_with_exit_2(argv: list[str], option: str) -> None:
    result = runner.invoke(cli.app, argv)
    assert_reported(result, ExitCode.USAGE, option)


def test_a_malformed_environment_setting_is_a_usage_error_with_exit_2(
    diverged_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reached through the environment rather than the command line, but just as fixable."""
    monkeypatch.setenv("CPPSD_RENAME_SIMILARITY", "abc")
    result = runner.invoke(cli.app, ["plan", str(diverged_repo)])
    assert_reported(result, ExitCode.USAGE, "cannot analyze", "CPPSD_RENAME_SIMILARITY='abc'")


def test_a_third_party_path_neither_revision_has_is_a_usage_error_with_exit_2(
    diverged_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo would leave the library in the model, so it is refused -- before any worktree,
    so the refusal costs no checkout (ADR-025). Every missing path is named."""
    require_libclang()

    def no_checkout(*args: object, **kwargs: object) -> None:
        raise AssertionError("a worktree was requested before the refusal")

    monkeypatch.setattr(cli, "checkout", no_checkout)
    result = runner.invoke(
        cli.app,
        ["diff", str(diverged_repo), "--third-party", "vendr", "--third-party", "libs/x/"],
    )
    assert_reported(result, ExitCode.USAGE, "cannot analyze", "neither revision", "vendr, libs/x")


# -- 3: the repository or revisions cannot be compared ----------------------------------------


def test_a_directory_that_is_not_a_repository_is_refused_with_exit_3(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = runner.invoke(cli.app, ["plan", str(plain)])
    assert_reported(result, ExitCode.REPOSITORY, "cannot analyze", "is not a git repository")


def test_an_unknown_base_is_refused_with_exit_3_and_named_verbatim(diverged_repo: Path) -> None:
    """Brackets survive: rich would otherwise read `[bar]` as markup and silently drop it."""
    result = runner.invoke(cli.app, ["plan", str(diverged_repo), "--base", "foo[bar]"])
    assert_reported(result, ExitCode.REPOSITORY, "cannot analyze", "'foo[bar]'", "--base")


def test_shallow_clone_is_refused_with_exit_3(shallow_repo: Path) -> None:
    result = runner.invoke(cli.app, ["plan", str(shallow_repo)])
    assert_reported(result, ExitCode.REPOSITORY, "shallow clone", "fetch --unshallow")


def test_a_changed_third_party_dependency_is_refused_with_exit_3(
    submodule_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submodule bump and a change in a declared path, both named, before any worktree, so
    the refusal costs no checkout and no parse (ADR-025)."""
    require_libclang()
    git(submodule_repo, "checkout", "-q", "-b", "feature")
    sha = commit(tmp_path / "inner", "vendor.h", "int vendored(int);\n", "vendor moves")
    git(submodule_repo, "update-index", "--cacheinfo", f"160000,{sha},dep")
    git(submodule_repo, "commit", "-q", "-m", "bump dep")
    commit(submodule_repo, "libs/z/z.h", "// z\n", "copy z")

    def no_checkout(*args: object, **kwargs: object) -> None:
        raise AssertionError("a worktree was requested before the refusal")

    monkeypatch.setattr(cli, "checkout", no_checkout)
    result = runner.invoke(cli.app, ["diff", str(submodule_repo), "--third-party", "libs/z"])
    assert_reported(
        result, ExitCode.REPOSITORY, "cannot analyze", "Third-party code changed", "dep, libs/z."
    )


def test_a_missing_git_is_named_with_exit_3(
    diverged_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = runner.invoke(cli.app, ["plan", str(diverged_repo)])
    assert_reported(result, ExitCode.REPOSITORY, "git is not installed, or not on PATH")


def test_an_empty_comparison_is_refused_by_diff_with_exit_3(bare_repo: Path) -> None:
    """Head is the merge base: an empty document here would be no analysis dressed as one."""
    require_libclang()
    commit(bare_repo, "a.cpp", "int a() { return 1; }\n", "A")
    result = runner.invoke(cli.app, ["diff", str(bare_repo), "-j", "1"])
    assert_reported(result, ExitCode.REPOSITORY, "cannot analyze", "no second point")
    assert result.stdout == ""


# -- 4: no usable compile database ------------------------------------------------------------


def test_a_repository_with_no_build_description_is_refused_with_exit_4(
    branch_without_build: Path,
) -> None:
    require_libclang()
    result = runner.invoke(cli.app, ["diff", str(branch_without_build), "-j", "1"])
    assert_reported(
        result,
        ExitCode.BUILD,
        "cannot analyze",
        "No usable compile database",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    )


def test_a_malformed_compile_database_is_refused_with_exit_4(bare_repo: Path) -> None:
    require_libclang()
    commit(bare_repo, "a.cpp", "int a() { return 1; }\n", "A")
    commit(bare_repo, "compile_commands.json", "{ not json", "a broken database")
    git(bare_repo, "checkout", "-q", "-b", "feature")
    commit(bare_repo, "a.cpp", "int a() { return 2; }\n", "change a")
    result = runner.invoke(cli.app, ["diff", str(bare_repo), "-j", "1"])
    assert_reported(result, ExitCode.BUILD, "cannot analyze", "could not be read as JSON")


# -- 5: no parser that can be trusted ---------------------------------------------------------


def test_a_libclang_override_that_names_nothing_is_refused_with_exit_5(tmp_path: Path) -> None:
    """In a subprocess: libclang loads once per process, and a test of a failed load must not
    leave this one holding nothing -- or holding the real library and passing for it."""
    proc = run_module(
        "doctor",
        env={"CPPSD_LIBCLANG": "/nonexistent/libclang.so", "CPPSD_CACHE_DIR": str(tmp_path)},
    )
    assert proc.returncode == ExitCode.TOOLCHAIN, (proc.returncode, proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr
    assert "cannot analyze" in proc.stderr
    assert "CPPSD_LIBCLANG=/nonexistent/libclang.so" in flat(proc.stderr)


# -- 6: an operation failed while running -----------------------------------------------------


def test_git_failing_mid_command_exits_6_as_a_failure_not_a_refusal(
    diverged_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repository was fine and the command started; something broke while it ran. A
    caller may retry that, which it should not do for a refused precondition."""

    def git_fails(*args: object, **kwargs: object) -> int:
        raise GitError(["diff", "--numstat"], 128, "fatal: simulated failure")

    monkeypatch.setattr(cli, "diff_line_count", git_fails)
    result = runner.invoke(cli.app, ["plan", str(diverged_repo)])
    assert_reported(result, ExitCode.FAILED, "failed", "fatal: simulated failure")
    assert "cannot analyze" not in result.stderr


# -- 1: a defect in cppsd ---------------------------------------------------------------------


def test_an_unexpected_exception_is_not_swallowed_and_exits_1(
    diverged_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only this package's own errors become messages; anything else is a bug, and catching
    it would trade its traceback for a message that explains nothing."""

    def defect(*args: object, **kwargs: object) -> None:
        raise RuntimeError("a defect in cppsd")

    monkeypatch.setattr(cli, "file_changes", defect)
    result = runner.invoke(cli.app, ["plan", str(diverged_repo)])
    assert result.exit_code == ExitCode.INTERNAL
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "a defect in cppsd"
    assert "cannot analyze" not in result.stderr
    assert "failed" not in result.stderr


# -- wiring: a flag typed on the command line reaches the stage documented for it -------------
#
# `diff` "holds no presentation and no analysis" (this module's own docstring): every flag
# below is a value the command only has to carry, unchanged, to the one place that gives it
# meaning. That is exactly the kind of bug that is invisible in a design review and silent in
# the output -- `--expand` quietly inverted still produces a document, still renders, and
# still passes every test that checks the *document's* contract (`test_render.py` already
# does, thoroughly). `mock_pipeline` stands in for a system libclang and a compile database so
# these can run as plain, fast, deterministic tests of the CLI's own glue, against a git
# repository with neither.


def test_diff_threads_declared_third_party_paths_into_the_document(
    repo_with_vendor_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--third-party` reaching `third_party_set` is the whole of the CLI's job for the flag
    (ADR-025). The error-path tests above (a path neither revision has; a path that
    changed) monkeypatch `checkout` away specifically so the worktree is never touched --
    which means neither of them can see whether a *valid* declared path actually reaches
    the document once `diff` keeps going."""
    mock_pipeline(monkeypatch, Model(rev="a"), Model(rev="b"))
    result = runner.invoke(
        cli.app,
        ["diff", str(repo_with_vendor_dir), "--third-party", "vendor/dep", "--json"],
    )
    assert_reported(result, ExitCode.OK)
    document = json.loads(result.stdout)
    assert document["third_party"] == [{"path": "vendor/dep", "sources": ["declared"]}]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], {"collapse_cascades": True, "limit": DEFAULT_LIMIT}),
        (["--expand", "--limit", "0"], {"collapse_cascades": False, "limit": 0}),
    ],
    ids=["documented-defaults", "expand-and-unlimited"],
)
def test_expand_and_limit_are_forwarded_to_the_view_unaltered(
    diverged_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected: dict[str, object],
) -> None:
    """`--expand`'s and `--limit`'s own collapse/top-level semantics belong to `render` and
    are `test_render.py`'s claim to defend; what only a CLI-level test can see is whether
    the value the person typed is still the value `render` receives, two option parses and
    a document build later."""
    mock_pipeline(monkeypatch, Model(rev="a"), Model(rev="b"))
    calls: list[dict[str, object]] = []

    def fake_render(document: object, console: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(render_module, "render", fake_render)

    result = runner.invoke(cli.app, ["diff", str(diverged_repo), *argv])
    assert_reported(result, ExitCode.OK)
    assert calls == [expected]


@pytest.mark.parametrize(
    ("argv", "expected_workers"),
    [([], None), (["--workers", "3"], 3)],
    ids=["unset-stays-none", "explicit-value-passes-through"],
)
def test_the_workers_flag_reaches_build_model_unfilled_by_the_cli(
    diverged_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_workers: int | None,
) -> None:
    """`build_model`'s own precedence is flag > `$CPPSD_WORKERS` > what the machine can run
    (`produce.parallel.worker_count`) -- a promise this module's `diff` docstring says the
    CLI must not quietly break by filling in a default of its own before this call, which
    would be two mechanisms resolving one knob and disagreeing silently. `None` has to
    reach `build_model` unchanged when the flag is absent, not some number computed here."""
    calls = mock_pipeline(monkeypatch, Model(rev="a"), Model(rev="b"))
    result = runner.invoke(cli.app, ["diff", str(diverged_repo), *argv])
    assert_reported(result, ExitCode.OK)
    assert [call["workers"] for call in calls] == [expected_workers, expected_workers]


# -- end to end: a real diff of a tiny CMake project ------------------------------------------


@pytest.fixture(scope="module")
def cmake_branch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A CMake project whose `feature` branch changes one function body."""
    require_cmake()
    require_libclang()
    repo = tmp_path_factory.mktemp("e2e") / "tiny"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, "a.cpp", "int f() { return 1; }\n", "A")
    commit(repo, "orphan.h", "int limit = 1;\n", "a header no translation unit includes")
    commit(
        repo,
        "CMakeLists.txt",
        "cmake_minimum_required(VERSION 3.10)\nproject(tiny CXX)\nadd_library(tiny a.cpp)\n",
        "cmake",
    )
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "a.cpp", "int f() { return 2; }\n", "change f")
    commit(repo, "orphan.h", "int limit = 2;\n", "change what no model can see")
    return repo


@pytest.fixture
def warm_cache(
    cmake_branch: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One cache for every end-to-end test, so only the first pays for configure and parse."""
    shared = tmp_path_factory.getbasetemp() / "e2e-cache"
    monkeypatch.setenv("CPPSD_CACHE_DIR", str(shared))


@pytest.mark.usefixtures("warm_cache")
def test_diff_json_puts_one_document_on_stdout_and_names_the_repo_by_its_directory(
    cmake_branch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run as `cppsd diff .`: the repository's name is still its directory's, not ''.

    `--out` in a directory that does not exist yet gets the same document, whole, with no
    temporary file left beside it.
    """
    out = tmp_path / "reports" / "doc.json"
    monkeypatch.chdir(cmake_branch)
    result = runner.invoke(cli.app, ["diff", ".", "--json", "--out", str(out), "-j", "1"])
    assert_reported(result, ExitCode.OK)

    document = json.loads(result.stdout)  # one document, nothing before or after it
    assert document["meta"]["repo"] == "tiny"
    assert "comparing" in result.stderr, "progress belongs on stderr"
    assert "comparing" not in result.stdout

    assert json.loads(out.read_text(encoding="utf-8")) == document
    assert [p.name for p in out.parent.iterdir()] == ["doc.json"]


@pytest.mark.usefixtures("warm_cache")
def test_diff_json_carries_the_third_party_set_and_hints(cmake_branch: Path) -> None:
    """Present on every document, empty when the repository has neither (ADR-025)."""
    result = runner.invoke(cli.app, ["diff", str(cmake_branch), "--json", "-j", "1"])
    assert_reported(result, ExitCode.OK)
    document = json.loads(result.stdout)
    assert document["third_party"] == []
    assert document["third_party_hints"] == []


@pytest.mark.usefixtures("warm_cache")
def test_a_change_no_model_can_see_is_reported_as_uncovered(cmake_branch: Path) -> None:
    """`f`'s change is on its change event's lines; the header nothing includes is in no
    model, so its change is uncovered rather than silently absent (ADR-042)."""
    result = runner.invoke(cli.app, ["diff", str(cmake_branch), "--json", "-j", "1"])
    assert_reported(result, ExitCode.OK)
    document = json.loads(result.stdout)
    summary = document["summary"]
    assert (summary["changed_cpp_lines"], summary["uncovered_lines"]) == (4, 2)
    assert {(f["side"], f["path"]) for f in document["completeness"]["uncovered"]["files"]} == {
        ("base", "orphan.h"),
        ("head", "orphan.h"),
    }
    assert "2 of 4 changed C++ lines explained by no change event" in summary["compression"]
    assert document["completeness"]["uncovered"]["lines_in_unmodelled_files"] == 2


@pytest.mark.usefixtures("warm_cache")
def test_a_reader_that_stops_early_is_not_an_error(cmake_branch: Path) -> None:
    """`cppsd diff --json | head -c 1`: the reader closing the pipe is its choice, so the
    exit is 0 and stderr carries no BrokenPipeError.

    The read end is closed before the document is written rather than after one byte of
    it: a document smaller than the pipe's buffer would otherwise be written whole before
    `head` exits, and the test would pass without the pipe ever breaking.
    """
    read, write = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-m", "cpp_semantic_diff.cli", "diff", str(cmake_branch), "--json"],
        stdout=write,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(write)
    os.close(read)
    _, stderr = proc.communicate(timeout=120)
    assert proc.returncode == ExitCode.OK, stderr
    assert "BrokenPipeError" not in stderr
    assert "Traceback" not in stderr
    assert "Exception ignored" not in stderr


@pytest.mark.usefixtures("warm_cache")
def test_an_out_path_that_is_a_directory_exits_6_and_names_the_path(
    cmake_branch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The analysis succeeded and its delivery failed: a failure while running, not a refusal."""
    (tmp_path / "taken").mkdir()
    monkeypatch.chdir(tmp_path)  # a short relative path, which rich cannot wrap mid-name
    result = runner.invoke(cli.app, ["diff", str(cmake_branch), "--out", "taken", "-j", "1"])
    assert_reported(result, ExitCode.FAILED, "failed", "Could not write taken")
    assert "cannot analyze" not in result.stderr
    assert result.stdout == "", "no view is rendered for a document that was not delivered"
