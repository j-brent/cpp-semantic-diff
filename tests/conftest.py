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
def submodule_repo(tmp_path: Path, bare_repo: Path) -> Path:
    """A superproject pinning a submodule -- the shape `git worktree add` does not carry.

    leveldb is the first fixture with one; fmt and spdlog vendor by copying, so nothing
    exercised this until the fixture set widened.
    """
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    git(inner, "config", "user.email", "test@example.com")
    git(inner, "config", "user.name", "Test")
    git(inner, "config", "commit.gpgsign", "false")
    commit(inner, "vendor.h", "int vendored();\n", "vendor")

    # Git blocks the `file://` transport for submodules (CVE-2022-39253) and ignores repo
    # config for it, so it has to come from the command line. Real submodule URLs are
    # remote; this is a property of the fixture, never of the tool -- which is why the
    # relaxation is applied here and via the environment in the test, and never in
    # `materialize_submodules`.
    git(bare_repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "dep")
    commit(bare_repo, "main.cpp", '#include "dep/vendor.h"\nint m() { return vendored(); }\n', "A")
    return bare_repo


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


#: The TU-identity hazards, in the smallest C++ that produces them (STATUS §6).
#:
#: These shapes exist in no fixture repository. fmt has 250 file-scoped USRs at `da256982`
#: and *one* of them is header-declared, so the narrow-vs-broad `Node.tu` rule would be
#: decided there on a sample of one; its four repeated compile-database basenames are all
#: the same file recompiled, so the collision below has no instance at all. A rule cannot be
#: chosen against a repository that does not contain the case it is about.
#:
#: Two hazards, and they pull in opposite directions:
#:
#: `a/util.cpp` and `b/util.cpp` -- clang writes the file *name* into a file-scoped USR, not
#: the path, so four distinct functions here share two USRs. Today they merge and half of
#: them vanish, which is **under-reporting**: a body change in `b`'s copy produces no event
#: at all. This is the case ARCH-001 §5's "identity must include the TU" exists for.
#:
#: `common.h` included by `one.cpp` and `two.cpp` -- one entity per includer, collapsed by
#: USR dedup to a single node whose TU is whichever observation won. Populating `tu` here
#: would make identity depend on which *other* files happen to be in the build, so an
#: unrelated new source could break the match for an entity nobody touched.
TU_IDENTITY_HAZARDS: dict[str, str] = {
    "a/util.cpp": (
        "namespace { int shared_helper() { return 1; } }\n"
        "static int file_helper() { return 2; }\n"
        "int a_entry() { return shared_helper() + file_helper(); }\n"
    ),
    "b/util.cpp": (
        "namespace { int shared_helper() { return 10; } }\n"
        "static int file_helper() { return 20; }\n"
        "int b_entry() { return shared_helper() + file_helper(); }\n"
    ),
    "common.h": (
        "namespace { int header_helper() { return 3; } }\n"
        "static int header_static() { return 4; }\n"
    ),
    "one.cpp": (
        '#include "common.h"\nint one_entry() { return header_helper() + header_static(); }\n'
    ),
    "two.cpp": (
        '#include "common.h"\nint two_entry() { return header_helper() + header_static(); }\n'
    ),
}

#: Every translation unit in `TU_IDENTITY_HAZARDS`, in compile-database order.
TU_IDENTITY_TUS: tuple[str, ...] = ("a/util.cpp", "b/util.cpp", "one.cpp", "two.cpp")


def require_fixture(name: str) -> Path:
    path = FIXTURE_ROOT / name
    if not path.is_dir():
        pytest.skip(f"fixture repo {name} not present at {path}")
    return path
