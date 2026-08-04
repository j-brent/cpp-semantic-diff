"""Materializing both revisions and obtaining a compile database for each.

ARCH-001 §3.1: a compile database is required at both revisions -- configure, not a full
build. Configure is seconds where a build is minutes, which is what makes the requirement
affordable. Repositories with generated sources are the known exception and report the
shortfall as incompleteness rather than parsing the remainder silently.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from cpp_semantic_diff.acquire.git import run_git
from cpp_semantic_diff.errors import CompileDatabaseError, ConfigureError

#: Where a compile database usually turns up, in search order.
COMPILE_DB_LOCATIONS = ("build/compile_commands.json", "compile_commands.json")

CONFIGURE_TIMEOUT_SECONDS = 600


#: Only these CMake generators emit compile_commands.json. The tool picks one rather than
#: letting CMake default, because a generator that cannot emit one still *configures
#: successfully* -- turning a hard precondition into a silent one. Visual Studio and Xcode
#: are the generators that behave this way.
COMPILE_DB_GENERATORS = ("Ninja", "Ninja Multi-Config", "Unix Makefiles", "MinGW Makefiles")


def select_generator(extra_args: list[str] | None) -> list[str]:
    """Choose a generator that can emit a compile database, unless the caller picked one."""
    if extra_args and any(a == "-G" or a.startswith("-G") for a in extra_args):
        return []
    if shutil.which("ninja"):
        return ["-G", "Ninja"]
    return []


def cached_generator(build_dir: Path) -> str | None:
    cache = build_dir / "CMakeCache.txt"
    if not cache.is_file():
        return None
    for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("CMAKE_GENERATOR:"):
            return line.split("=", 1)[1].strip() if "=" in line else None
    return None


def is_rooted(path: str) -> bool:
    """True if `path` is absolute under *either* path flavour.

    A compile database records whatever the generating host wrote, and it is routinely
    read somewhere else -- committed to a repo, copied off a build machine, produced in a
    container. `Path.is_absolute()` answers only for the running platform, so a
    POSIX-rooted entry read on Windows reads as relative and gets joined onto the build
    directory, silently producing a path to nothing. Flavour-agnostic is the correct test
    even though this tool now targets Linux only.
    """
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def cache_root() -> Path:
    """Where worktrees and cached models live.

    Honours `XDG_CACHE_HOME` before falling back to `~/.cache`, which is the same
    fallback the XDG spec itself defines. A user who has redirected their cache -- onto a
    bigger disk, or off a synced home directory -- means it, and this cache grows by a
    full worktree per revision.
    """
    if override := os.environ.get("CPPSD_CACHE_DIR"):
        return Path(override)
    if xdg := os.environ.get("XDG_CACHE_HOME"):
        return Path(xdg) / "cpp-semantic-diff"
    return Path.home() / ".cache" / "cpp-semantic-diff"


@dataclass(frozen=True)
class CompileEntry:
    file: Path
    directory: Path
    arguments: list[str]

    @property
    def is_cpp(self) -> bool:
        return self.file.suffix.lower() in {".cpp", ".cc", ".cxx", ".c++", ".cp", ".ixx"}


@dataclass
class CompileDatabase:
    path: Path
    entries: list[CompileEntry] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    @classmethod
    def load(cls, path: Path) -> CompileDatabase:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries: list[CompileEntry] = []
        for item in raw:
            directory = Path(item["directory"])
            raw_file = item["file"]
            file = Path(raw_file) if is_rooted(raw_file) else directory / raw_file
            if "arguments" in item:
                arguments = list(item["arguments"])
            else:
                # The spec defines `command` as shell-quoted, and CMake writes it with
                # POSIX quoting regardless of host.
                arguments = shlex.split(item.get("command", ""), posix=True)
            entries.append(CompileEntry(file=file, directory=directory, arguments=arguments))
        return cls(path=path, entries=entries)


def find_compile_database(worktree: Path) -> Path | None:
    for relative in COMPILE_DB_LOCATIONS:
        candidate = worktree / relative
        if candidate.is_file():
            return candidate
    return None


def configure(worktree: Path, rev: str, extra_args: list[str] | None = None) -> Path:
    """Run CMake configure to produce a compile database.

    Failure here is terminal by design (ARCH-001 §3.1): the base is main, and a main that
    does not configure is a problem this tool is not the right response to.
    """
    if not (worktree / "CMakeLists.txt").is_file():
        raise CompileDatabaseError(
            rev,
            str(worktree),
            "No CMakeLists.txt, and no compile database was present either. "
            "Non-CMake builds need one generated by Bear, compiledb, or the build system.",
        )

    build_dir = worktree / "build"
    generator = select_generator(extra_args)

    # A build tree cached with a generator that cannot emit a compile database would fail
    # to reconfigure, and re-running the tool would keep hitting the same wall.
    existing = cached_generator(build_dir)
    if existing is not None and existing not in COMPILE_DB_GENERATORS:
        shutil.rmtree(build_dir, ignore_errors=True)

    argv = [
        "cmake",
        "-B",
        str(build_dir),
        "-S",
        str(worktree),
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        *generator,
        *(extra_args or []),
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CONFIGURE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).splitlines()[-15:])
        raise ConfigureError(rev, tail)

    database = find_compile_database(worktree)
    if database is None:
        used = cached_generator(build_dir) or "the default generator"
        raise CompileDatabaseError(
            rev,
            str(worktree),
            f"CMake configured successfully with {used}, but emitted no "
            f"compile_commands.json. Only these generators support it: "
            f"{', '.join(COMPILE_DB_GENERATORS)}. Install Ninja, or pass an explicit "
            f"generator through --configure-arg.",
        )
    return database


@dataclass
class Checkout:
    """One revision, materialized on disk with a compile database."""

    rev: str
    path: Path
    compile_db: CompileDatabase

    @property
    def cpp_entries(self) -> list[CompileEntry]:
        return [e for e in self.compile_db.entries if e.is_cpp]


def materialize_submodules(worktree: Path, rev: str) -> None:
    """Check out the submodules `rev` pins, because `git worktree add` does not.

    A submodule is a gitlink -- mode 160000, a commit id and nothing else -- and
    `git worktree add` creates it as an *empty directory*. Every header the superproject
    expects to find there is missing, so CMake fails on a tree that builds perfectly in the
    original clone, and it fails saying the source directory has no `CMakeLists.txt`. That
    reaches the user as ARCH-001 §3.1's "the base is expected to build", which is both
    alarming and wrong: the base builds, the worktree was incomplete.

    Silent truncation is the alternative worth naming. Where a missing submodule does *not*
    break configure -- an optional dependency, a vendored library only some targets use --
    the compile database is simply smaller, both revisions lose the same entities, and the
    two models agree with each other. That is the failure this project exists to argue
    against (STATUS §5), reached by a different road than the libclang one.

    The pinned commit is the right version by construction: it is what this revision
    recorded, so a commit that bumps a submodule is a real difference between the two
    models rather than an artefact of when the tool ran.

    **This needs the network, and it is worth knowing why it cannot be made not to.** A
    worktree shares the superproject's object store but *not* its submodule module store:
    `git submodule update` clones into `.git/worktrees/<sha>/modules/...`, a fresh store per
    worktree, so the parent having the submodule checked out does not help. Measured, with
    the network blocked, on leveldb: the clone is attempted and fails.
    `submodule.alternateLocation=superproject` does not rescue it either, because the
    alternate is resolved against the worktree's gitdir, which is the empty one.

    So the cost is one submodule clone per revision analyzed, paid once because worktrees are
    cached by SHA (ARCH-001 §9), and a repository with submodules cannot be analyzed offline.
    That is a limit of `git worktree`, not a choice made here, and it is stated rather than
    discovered.
    """
    if not (worktree / ".gitmodules").is_file():
        return

    result = subprocess.run(
        ["git", "-C", str(worktree), "submodule", "update", "--init", "--recursive"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise CompileDatabaseError(
            rev,
            str(worktree),
            "This revision pins git submodules and they could not be checked out. "
            "`git worktree add` does not populate them, so the tree is incomplete and any "
            "model built from it would omit whatever they provide.\n"
            f"git said: {result.stderr.strip()}\n"
            "A worktree clones submodules into its own module store, so this needs network "
            "access to wherever they are hosted -- having them checked out in the repository "
            "does not help.",
        )


@contextmanager
def checkout(
    repo: Path,
    rev: str,
    *,
    configure_args: list[str] | None = None,
    keep: bool = True,
) -> Iterator[Checkout]:
    """Materialize `rev` as a detached git worktree and ensure it has a compile database.

    Worktrees are kept by default and keyed by SHA, so the base -- which is shared by every
    review against it -- is configured once rather than once per run (ARCH-001 §9).
    """
    worktree = cache_root() / "worktrees" / repo.name / rev
    created = not worktree.exists()

    if created:
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run_git(repo, "worktree", "add", "--detach", "--quiet", str(worktree), rev)
        materialize_submodules(worktree, rev)

    try:
        database_path = find_compile_database(worktree)
        database_path = database_path or configure(worktree, rev, configure_args)
        yield Checkout(
            rev=rev,
            path=worktree,
            compile_db=CompileDatabase.load(database_path),
        )
    finally:
        if not keep and created:
            run_git(repo, "worktree", "remove", "--force", str(worktree), check=False)


def prune_worktrees(repo: Path) -> None:
    run_git(repo, "worktree", "prune", check=False)
