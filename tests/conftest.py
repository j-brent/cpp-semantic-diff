from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

#: Real C++ repositories used for measurement and end-to-end tests. Full history is
#: required -- a shallow clone has no second point and the tool rejects it by design.
FIXTURE_ROOT = Path(os.environ.get("CPPSD_FIXTURE_ROOT", Path.home() / "fixtures"))


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def commit(repo: Path, name: str, content: str, message: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def move(repo: Path, src: str, dst: str, message: str) -> str:
    """A real `git mv` plus commit -- the only honest way to test a rename map.

    A hand-built map tests the parser against itself. What has to hold is that *git's* own
    answer, at git's own threshold, reaches the matcher (ARCH-001 §5).
    """
    (repo / dst).parent.mkdir(parents=True, exist_ok=True)
    git(repo, "mv", src, dst)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """An initialized repo with identity configured and no commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


@pytest.fixture
def diverged_repo(bare_repo: Path) -> Path:
    """A branch forked from main, where **main then advanced independently**.

    This is the shape that makes merge-base semantics matter: `feature` changed one file,
    but main gained an unrelated commit afterwards. Diffing against main's tip would
    attribute that unrelated work to the branch under review.

        main:     A ── B ─────── D   (D is someone else's merged work)
                        \\
        feature:         └── C       (the only change under review)
    """
    repo = bare_repo
    commit(repo, "a.cpp", "int a() { return 1; }\n", "A")
    commit(repo, "b.cpp", "int b() { return 2; }\n", "B")

    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "c.cpp", "int c() { return 3; }\n", "C")

    git(repo, "checkout", "-q", "main")
    commit(repo, "d.cpp", "int d() { return 4; }\n", "D")

    git(repo, "checkout", "-q", "feature")
    return repo


@pytest.fixture
def renamed_repo(bare_repo: Path) -> Path:
    """A branch that renamed one file and moved another between directories.

    Two shapes, because `RenameMap` handles them with different halves: a basename change
    goes through `by_name` (which rewrites the file component of a file-scoped USR), a pure
    directory move through `by_path` (which rewrites `Node.tu`). A fixture with only one of
    them would leave half the machinery unexercised.

    An unrelated edit rides along so the renames are not the whole diff -- a change surface
    that is nothing but renames is the case least like a real review.

        main:     A ─ B
                       \\
        feature:        └─ rename ─ move ─ edit
    """
    repo = bare_repo
    commit(repo, "src/util.cpp", "static int helper() { return 1; }\n", "A")
    commit(repo, "src/tool.cpp", "int tool() { return 2; }\n", "B")

    git(repo, "checkout", "-q", "-b", "feature")
    move(repo, "src/util.cpp", "src/support.cpp", "rename util -> support")
    move(repo, "src/tool.cpp", "lib/tool.cpp", "move tool into lib")
    commit(repo, "README.md", "unrelated\n", "edit something that is not a rename")
    return repo


@pytest.fixture
def shallow_repo(tmp_path: Path, bare_repo: Path) -> Path:
    commit(bare_repo, "a.cpp", "int a();\n", "A")
    commit(bare_repo, "b.cpp", "int b();\n", "B")

    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", bare_repo.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )
    return clone


def require_fixture(name: str) -> Path:
    path = FIXTURE_ROOT / name
    if not path.is_dir():
        pytest.skip(f"fixture repo {name} not present at {path}")
    return path
