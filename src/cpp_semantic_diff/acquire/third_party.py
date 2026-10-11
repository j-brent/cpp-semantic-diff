"""The third-party set: code inside the repository that the repository does not own.

ADR-025. v0 analyzes only code the repository owns and contains, so a
comparison first has to know which directories are someone else's. Two sources, and only two:

- **Submodules**, automatically: every gitlink at the base or the head.
- **Declared paths**, from `cppsd diff --third-party PATH`, for libraries copied into the tree.

A declared path that exists in neither revision is refused. It is almost certainly a typo, and
a typo would silently leave a library in the model.

**Folder names are a hint, never a rule.** A tracked directory named like a dependency
(`third_party`, `gtest`, `bundled`) that is not in the set produces a note suggesting
`--third-party`, and excludes nothing. Recognition by name is a guess, and it misses a library
copied under any other name (ADR-025, "Alternatives rejected").

The set is computed once per comparison, after the revisions are resolved and before any
worktree exists, so a refusal costs no checkout. So is the check that none of it changed
between the two points: v0 refuses a comparison across a dependency change.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from cpp_semantic_diff.acquire.git import FileChanges, RevPair, run_git, run_git_z
from cpp_semantic_diff.errors import ThirdPartyChangedError, ThirdPartyPathError

#: Directory names that suggest a dependency. They produce a hint and nothing else.
HINT_SEGMENTS: frozenset[str] = frozenset(
    {
        "3rdparty",
        "_deps",
        "bundled",  # spdlog vendors fmt under `include/spdlog/fmt/bundled`.
        "contrib",
        "deps",
        "extern",
        "external",
        "third-party",
        "third_party",
        "thirdparty",
        "vendor",
        "vendored",
        # Named libraries. A copy is often dropped straight into `test/` with no
        # `third_party` above it, which is what fmt does with googletest.
        "catch",
        "catch2",
        "doctest",
        "gmock",
        "googlemock",
        "googletest",
        "gtest",
    }
)


class ThirdPartySource(StrEnum):
    """Where a third-party path came from."""

    SUBMODULE = "submodule"
    DECLARED = "declared"


@dataclass(frozen=True)
class ThirdPartyEntry:
    """One third-party directory. A path can be both a submodule and declared."""

    path: str
    sources: frozenset[ThirdPartySource]


@dataclass(frozen=True)
class ThirdPartySet:
    """The third-party directories of one comparison, and the folder-name hints.

    Paths are repository-relative, POSIX-separated directories with no trailing slash -- the
    spelling `Location.file` uses -- sorted, so two runs over the same input are equal.
    """

    entries: tuple[ThirdPartyEntry, ...] = ()
    hints: tuple[str, ...] = field(default=())
    """Tracked directories that look like third-party code and are not in the set."""

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries)

    def label_for(self, file: str) -> str | None:
        """The entry `file` lies in, or None if it is not third-party.

        `file` is repository-relative. Where entries nest -- a submodule inside a declared
        vendored folder -- the innermost wins, because it is the more specific name.
        """
        best: str | None = None
        for path in self.paths:
            if _within(file, path) and (best is None or len(path) > len(best)):
                best = path
        return best


def hint_message(path: str) -> str:
    """The note for one folder-name hint (ADR-025)."""
    return f"`{path}` looks like third-party code; pass `--third-party {path}` if it is."


def _within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + "/")


def normalize_declared(raw: str) -> str:
    """A `--third-party` value as a repository-relative directory, or a refusal.

    A trailing slash and a leading `./` are spelling, not meaning, and are dropped. An
    absolute path, one that climbs out with `..`, or one that names the repository itself
    cannot be a directory inside the repository, and is refused.
    """
    text = raw.strip().replace("\\", "/")
    if text.startswith("/"):
        raise ThirdPartyPathError(
            f"--third-party {raw!r} is absolute. Give a path relative to the repository root."
        )
    parts = [p for p in PurePosixPath(text).parts if p != "."]
    if not parts:
        raise ThirdPartyPathError(
            f"--third-party {raw!r} names the repository itself, which cannot be third-party."
        )
    if ".." in parts:
        raise ThirdPartyPathError(
            f"--third-party {raw!r} leaves the repository. Give a path relative to the "
            f"repository root."
        )
    return "/".join(parts)


@dataclass(frozen=True)
class _Tree:
    """The directories and gitlinks git tracks at one revision."""

    directories: frozenset[str]
    gitlinks: frozenset[str]


def _tracked_tree(repo: Path, rev: str) -> _Tree:
    """`git ls-tree -r -d`: every tracked directory, and every gitlink, at `rev`."""
    fields, _ = run_git_z(repo, "ls-tree", "-r", "-d", "-z", "--full-tree", rev)
    directories: set[str] = set()
    gitlinks: set[str] = set()
    for record in fields:
        meta, _, path = record.partition("\t")
        kind = meta.split(" ")[1] if meta.count(" ") >= 2 else ""
        if kind == "tree":
            directories.add(path)
        elif kind == "commit":
            gitlinks.add(path)
    return _Tree(frozenset(directories), frozenset(gitlinks))


def _owner(path: str, directories: Iterable[str]) -> str | None:
    """The directory among `directories` that strictly contains `path`, if any."""
    return next((d for d in sorted(directories) if path.startswith(d + "/")), None)


def _is_file(repo: Path, rev: str, path: str) -> bool:
    out = run_git(repo, "ls-tree", "--full-tree", rev, "--", path)
    return " blob " in out


def folder_hints(directories: Iterable[str], third_party: Sequence[str]) -> tuple[str, ...]:
    """Tracked directories named like a dependency that the set does not cover.

    A directory is hinted when its own name is in `HINT_SEGMENTS` and it is not a set entry,
    not inside one, and does not contain one -- leveldb's `third_party` holds its two
    submodules and nothing to declare. Only the topmost of nested matches is hinted: fmt's
    `test/gtest/gmock` is part of `test/gtest`, and one note covers both.
    """
    hinted: list[str] = []
    for directory in sorted(directories):
        name = PurePosixPath(directory).name.lower()
        if name not in HINT_SEGMENTS:
            continue
        if any(_within(directory, entry) or _within(entry, directory) for entry in third_party):
            continue
        if any(_within(directory, done) for done in hinted):
            continue
        hinted.append(directory)
    return tuple(hinted)


def third_party_set(repo: Path, revs: RevPair, declared: Sequence[str] = ()) -> ThirdPartySet:
    """The third-party set for comparing `revs`, refusing a declared path neither side has.

    The union of both revisions' submodules and the declared paths. A submodule is a gitlink,
    read from the tree, not from `.gitmodules`: a gitlink the file does not name still pins
    code the repository does not contain, and a stale entry with no gitlink pins none.
    `.gitmodules` is configuration for cloning, so it is not read at all.

    A declared path exists if it is a tracked directory or a gitlink at either revision; a
    file is refused as not a directory, and anything else as absent.
    """
    repo = repo.resolve()
    revisions = dict.fromkeys((revs.base_sha, revs.head_sha))
    trees = {rev: _tracked_tree(repo, rev) for rev in revisions}
    existing = set().union(*(t.directories | t.gitlinks for t in trees.values()))

    submodules = set().union(*(t.gitlinks for t in trees.values()))
    wanted = dict.fromkeys(normalize_declared(raw) for raw in declared)
    missing = [path for path in wanted if path not in existing]
    if missing:
        files = [p for p in missing if any(_is_file(repo, rev, p) for rev in revisions)]
        absent = [p for p in missing if p not in files]
        lines = []
        if absent:
            lines.append(
                "--third-party names a path that exists at neither revision: "
                + ", ".join(absent)
                + ". It is probably a typo, and a typo would leave the library in the model."
            )
            # The superproject tracks nothing inside a submodule, so a path there is absent
            # to it -- and already excluded, which is the more useful thing to say.
            lines.extend(
                f"{path} is inside submodule `{owner}`, which is already third-party."
                for path in absent
                if (owner := _owner(path, submodules)) is not None
            )
        if files:
            lines.append("--third-party names a file, not a directory: " + ", ".join(files) + ".")
        raise ThirdPartyPathError("\n".join(lines))

    sources: dict[str, set[ThirdPartySource]] = {}
    for path in submodules:
        sources.setdefault(path, set()).add(ThirdPartySource.SUBMODULE)
    for path in wanted:
        sources.setdefault(path, set()).add(ThirdPartySource.DECLARED)

    entries = tuple(
        ThirdPartyEntry(path=path, sources=frozenset(kinds))
        for path, kinds in sorted(sources.items())
    )
    directories = set().union(*(t.directories for t in trees.values()))
    return ThirdPartySet(
        entries=entries,
        hints=folder_hints(directories, [e.path for e in entries]),
    )


def changed_third_party(third_party: ThirdPartySet, changes: FileChanges) -> tuple[str, ...]:
    """The set entries that `changes` touches, sorted.

    A copied library shows its own changed files. A submodule bump is a modified gitlink at
    the submodule's path, which `label_for` matches like a file inside it; adding or removing
    a submodule is the same, as the set holds submodules from both revisions. A rename is
    checked at both ends, so a file moved into or out of a third-party directory counts.
    """
    hit: set[str] = set()
    for change in changes.changes:
        for path in (change.path, change.old_path):
            if path is not None and (label := third_party.label_for(path)) is not None:
                hit.add(label)
    return tuple(sorted(hit))


def refuse_changed_third_party(third_party: ThirdPartySet, changes: FileChanges) -> None:
    """Refuse the comparison if any third-party code changed between the two points.

    ADR-025: third-party code must be identical at both points. Every changed entry is
    named, so one run tells the user the whole of what blocks it.
    """
    if changed := changed_third_party(third_party, changes):
        raise ThirdPartyChangedError(changed)
