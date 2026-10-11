"""Git interrogation and revision resolution.

ARCH-001 §3.1. The one decision encoded here that is easy to get wrong: the base is
``merge-base(main, HEAD)``, not ``main``. Diffing against main's tip attributes every change
other people merged while the branch was open to the branch under review.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cpp_semantic_diff.errors import (
    GitError,
    GitNotFoundError,
    InvalidSettingError,
    NoMergeBaseError,
    NotAGitRepositoryError,
    ShallowRepositoryError,
    UnknownRefError,
)

#: Tried in order when no base ref is given and origin/HEAD is unset.
DEFAULT_BRANCH_CANDIDATES = ("main", "master")

#: Git's own `-M` default, named rather than implied. ARCH-001 §14 asks for this knob to be
#: designed in early -- git's rename detection "has one knob and decades of tuning", and a
#: threshold the tool never states is one no repository can be calibrated against.
DEFAULT_RENAME_SIMILARITY = 50

#: Overrides it for a repository whose refactors are systematically noisier or cleaner.
#: Same shape as `CPPSD_WORKERS`: the knob is the parameter, the environment is how a caller
#: that owns no call site reaches it.
RENAME_SIMILARITY_ENV = "CPPSD_RENAME_SIMILARITY"


def _run(
    argv: Sequence[str],
    *,
    errors: str = "replace",
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, and report a missing git as exactly that.

    Without the translation a missing git surfaces as `FileNotFoundError`, and the caller
    that probes whether a directory is a repository cannot tell it from "not a repository".
    The user is then told their checkout is not a checkout, and goes looking for the wrong
    problem.
    """
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors=errors,
            env=None if env is None else dict(env),
        )
    except FileNotFoundError as exc:
        raise GitNotFoundError() from exc


def _git(repo: Path, args: Sequence[str], *, check: bool, errors: str) -> str:
    proc = _run(["git", "-C", str(repo), *args], errors=errors)
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc.stdout


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    return _git(repo, args, check=check, errors="replace").strip()


#: What git prints when `diff.renameLimit` is exceeded and it skips inexact rename detection.
#: Git offers no exit status, flag or plumbing output for this -- stderr is the only channel
#: -- so the match is deliberately loose. The wording has changed before ("inexact" became
#: "exhaustive"); both lines name either the skip or the variable, and the advice line is the
#: only one that carries the limit that would have sufficed. The call runs under `LC_ALL=C`
#: so a translated git cannot hide it.
_RENAME_SKIPPED = re.compile(r"rename detection was skipped|renamelimit", re.IGNORECASE)
_RENAME_LIMIT_NEEDED = re.compile(r"renamelimit\b.*?\bat least (\d+)", re.IGNORECASE)


def _c_locale_env() -> dict[str, str]:
    """The environment for a git call whose stderr is parsed: git's untranslated messages.

    `LC_ALL=C` outranks `LANG`, and gettext ignores `LANGUAGE` under the C locale; it is
    removed anyway, so the guarantee does not rest on that rule.
    """
    env = {k: v for k, v in os.environ.items() if k != "LANGUAGE"}
    env["LC_ALL"] = "C"
    return env


@dataclass(frozen=True)
class RenameLimitHit:
    """Git skipped inexact rename detection because `diff.renameLimit` was exceeded.

    `needed` is the limit git said would have sufficed, or None if it did not say.
    """

    needed: int | None = None


def rename_limit_hit(stderr: str) -> RenameLimitHit | None:
    """Whether a `git diff --find-renames` run reported truncated rename detection."""
    if not _RENAME_SKIPPED.search(stderr):
        return None
    needed = _RENAME_LIMIT_NEEDED.search(stderr)
    return RenameLimitHit(needed=int(needed.group(1)) if needed else None)


def run_git_z(repo: Path, *args: str) -> tuple[list[str], str]:
    """NUL-separated fields from a `-z` git command, decoded the way `os` decodes paths, and
    the stderr of the successful run.

    `run_git` cannot serve this, and the reason is not the `.strip()` -- NUL is not
    whitespace, so records survive it. It is `errors="replace"`, which maps two *different*
    non-UTF-8 paths onto the same string. For `changed_files` that is a mangled label; in a
    rename map it is a **fabricated identity**, which is the one thing ARCH-001 §5 refuses.
    `surrogateescape` is what `os` uses for filenames, so a path that round-trips through the
    filesystem round-trips through here.

    Stderr is returned because `run_git` discards it on success, and for `file_changes` a
    successful run can still carry the warning that the rename map is incomplete. The C
    locale keeps that warning in the words `rename_limit_hit` reads.
    """
    proc = _run(
        ["git", "-C", str(repo), *args],
        errors="surrogateescape",
        env=_c_locale_env(),
    )
    if proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return [field for field in proc.stdout.split("\0") if field], proc.stderr


def run_git_raw(repo: Path, *args: str) -> tuple[str, str]:
    """A git command's whole stdout, decoded the way `os` decodes paths, and its stderr, under
    the C locale.

    For output that carries file names and file content together -- a patch -- where
    `run_git`'s `errors="replace"` would mangle a non-UTF-8 name and its `.strip()` would cut
    a line that is part of the content. Stderr is kept for the reason `run_git_z` keeps it: a
    successful diff can still warn that its rename detection was cut short.
    """
    proc = _run(["git", "-C", str(repo), *args], errors="surrogateescape", env=_c_locale_env())
    if proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc.stdout, proc.stderr


def is_git_repository(repo: Path) -> bool:
    """Whether `repo` is inside a work tree. A missing git raises rather than answering no."""
    try:
        return run_git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    except GitError:
        return False


def is_shallow(repo: Path) -> bool:
    return run_git(repo, "rev-parse", "--is-shallow-repository") == "true"


def ref_exists(repo: Path, ref: str) -> bool:
    proc = _run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return proc.returncode == 0


def resolve(repo: Path, ref: str) -> str:
    if not ref_exists(repo, ref):
        raise UnknownRefError(ref, str(repo))
    return run_git(repo, "rev-parse", f"{ref}^{{commit}}")


def detect_default_branch(repo: Path) -> str:
    """origin/HEAD if the remote published one, else the first candidate that exists."""
    try:
        symbolic = run_git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if symbolic:
            return symbolic.removeprefix("refs/remotes/")
    except GitError:
        pass

    for candidate in DEFAULT_BRANCH_CANDIDATES:
        for ref in (candidate, f"origin/{candidate}"):
            if ref_exists(repo, ref):
                return ref

    raise UnknownRefError("/".join(DEFAULT_BRANCH_CANDIDATES), str(repo))


def merge_base(repo: Path, base_ref: str, head_ref: str) -> str:
    proc = _run(["git", "-C", str(repo), "merge-base", base_ref, head_ref])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise NoMergeBaseError(base_ref, head_ref)
    return proc.stdout.strip()


@dataclass(frozen=True)
class RevPair:
    """The two points of the two-point analysis.

    ``base_sha`` is the merge base, so the delta contains only what this branch did.
    """

    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str

    @property
    def is_empty(self) -> bool:
        return self.base_sha == self.head_sha

    def describe(self) -> str:
        return f"{self.base_ref}...{self.head_ref}  ({self.base_sha[:12]} -> {self.head_sha[:12]})"


def resolve_revs(
    repo: Path,
    base_ref: str | None = None,
    head_ref: str = "HEAD",
) -> RevPair:
    """Resolve the pair to analyze, failing hard on any condition that makes it impossible.

    Shallow clones are rejected outright: a two-point analysis with one point available is
    not a degraded analysis, it is no analysis (ARCH-001 §2).
    """
    # Checked first and by name, so the first thing a machine without git reports is that,
    # not a consequence of it.
    if shutil.which("git") is None:
        raise GitNotFoundError()
    repo = repo.resolve()
    if not is_git_repository(repo):
        raise NotAGitRepositoryError(str(repo))
    if is_shallow(repo):
        raise ShallowRepositoryError(str(repo))

    resolved_base_ref = base_ref or detect_default_branch(repo)
    head_sha = resolve(repo, head_ref)
    base_tip = resolve(repo, resolved_base_ref)
    base_sha = merge_base(repo, base_tip, head_sha)

    return RevPair(
        base_ref=resolved_base_ref,
        head_ref=head_ref,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def resolve_rename_similarity(requested: int | None = None) -> int:
    """The similarity threshold: explicit request, then environment, then git's default.

    Malformed input is fatal here, unlike `$CPPSD_WORKERS`, which is ignored on a typo. The
    worker count is a performance knob and running at the default is better than refusing to
    run; this threshold decides *which files are the same file*, so falling back silently to
    a different answer than the one asked for is the quiet disagreement ARCH-001 §11 refuses.
    """
    if requested is None and (override := os.environ.get(RENAME_SIMILARITY_ENV, "").strip()):
        if not override.isdigit():
            raise InvalidSettingError(
                f"{RENAME_SIMILARITY_ENV}={override!r} is not a percentage. It decides which "
                f"files count as renamed, so it is not guessed at."
            )
        requested = int(override)
    if requested is None:
        return DEFAULT_RENAME_SIMILARITY
    if not 1 <= requested <= 100:
        raise InvalidSettingError(
            f"rename similarity must be a percentage in 1..100, got {requested}"
        )
    return requested


@dataclass(frozen=True)
class FileChange:
    """One record of `git diff --name-status`.

    `path` is the head-side path, except for a delete, where there is no head side and it is
    the base-side one -- which is exactly what `--name-only` prints in each case.
    """

    status: str
    path: str
    old_path: str | None = None
    score: int | None = None

    @property
    def is_rename(self) -> bool:
        return self.old_path is not None


@dataclass(frozen=True)
class FileChanges:
    """What the branch did to the file tree, with renames resolved.

    The one place in this codebase that knows the shape of a git change record, for the same
    reason `usr_file` is the one place that knows the shape of a USR: a second parser is a
    second opinion about identity.
    """

    changes: tuple[FileChange, ...]
    rename_limit: RenameLimitHit | None = None
    """Set when git skipped inexact rename detection, so `renames` holds only exact renames
    and some renamed files appear as a delete plus an add. Not lifted with `-l0`: that
    makes detection potentially quadratic on exactly the large diffs that hit the limit.
    It is reported instead (ARCH-001 §11, rule 2)."""

    @property
    def missed_rename_files(self) -> frozenset[str]:
        """Files a truncated rename map may have failed to pair: every add and delete.

        Empty when the map is complete. The matcher marks entities located in these files
        as possibly renamed rather than plainly added or removed.
        """
        if self.rename_limit is None:
            return frozenset()
        return frozenset(c.path for c in self.changes if c.status in ("A", "D"))

    @property
    def paths(self) -> list[str]:
        """Exactly what `git diff --name-only` prints, in git's order."""
        return [c.path for c in self.changes]

    @property
    def renames(self) -> dict[str, str]:
        """Base path -> head path. ARCH-001 §5's input to matching."""
        return {c.old_path: c.path for c in self.changes if c.old_path is not None}


def file_changes(
    repo: Path,
    revs: RevPair,
    *,
    rename_similarity: int | None = None,
) -> FileChanges:
    """Files the branch changed, relative to the merge base, with renames resolved.

    ARCH-001 §5 requires renames to be resolved from git *before* matching, because clang
    writes the source file name into the USR of every internal-linkage entity: a renamed file
    breaks tier 1 for all of them at once.

    Four flags, each load-bearing:

    `--find-renames` is passed **explicitly even though detection is on by default**. A
    repository or reviewer with `diff.renames=false` in config would otherwise yield an empty
    rename map and a silently different answer -- and identity may not depend on whose
    `~/.gitconfig` the tool ran under (ARCH-001 §12).

    `-z` is not a formatting preference. Without it git applies `core.quotePath` and
    C-escapes any non-ASCII path, so the parser would need an unquoter to recover a name it
    is about to use as identity.

    `--ignore-submodules=none` is passed because `ignore = all` in `.gitmodules`,
    `diff.ignoreSubmodules` or `submodule.<name>.ignore` would otherwise drop a moved gitlink
    from the list, and a dependency bump the refusal must see would vanish depending on the
    repository's or the reviewer's config (ADR-025, ARCH-001 §12).

    `--find-copies` is deliberately **not** passed. A copy leaves the base file in place, so
    rewriting the base-side USR to the copy's name would claim the original moved -- a
    node-identity judgement from a file-level heuristic, which is the inversion ARCH-001 §5
    exists to prevent.
    """
    similarity = resolve_rename_similarity(rename_similarity)
    # Stderr is kept for one reason: when `diff.renameLimit` is exceeded git skips inexact
    # rename detection, exits 0, and says so only there. Discarding it would hand the matcher
    # a rename map that is silently short (ARCH-001 §11).
    fields, stderr = run_git_z(
        repo,
        "diff",
        "--name-status",
        f"--find-renames={similarity}%",
        "--ignore-submodules=none",
        "-z",
        f"{revs.base_sha}..{revs.head_sha}",
    )

    changes: list[FileChange] = []
    i = 0
    while i < len(fields):
        token = fields[i]
        status, digits = token[:1], token[1:]
        # R and C are the only statuses that carry a second path. Everything else -- A, D, M,
        # T, U and the X git emits when it is confused -- is one path, and an unknown letter
        # is treated as one path rather than refused, because a git that grew a new status
        # should not stop a review it does not otherwise affect.
        wants_two = status in ("R", "C")
        needed = 3 if wants_two else 2
        if i + needed > len(fields):
            # A truncated record understates what changed. Refusing beats reporting a
            # smaller diff than git did -- the same reasoning as declining to cache rather
            # than serving a weak key (ARCH-001 §9).
            # It is git's output that cannot be read, so this is a failed run rather than a
            # precondition the user could have met.
            raise GitError(
                ["diff", "--name-status", "-z"],
                0,
                f"output ended mid-record: {fields[i:]!r}. Expected {needed} fields for "
                f"status {token!r}, so the list of changed files cannot be trusted.",
            )
        if wants_two:
            changes.append(
                FileChange(
                    status=status,
                    path=fields[i + 2],
                    old_path=fields[i + 1] if status == "R" else None,
                    score=int(digits) if digits.isdigit() else None,
                )
            )
        else:
            changes.append(FileChange(status=status, path=fields[i + 1]))
        i += needed

    return FileChanges(changes=tuple(changes), rename_limit=rename_limit_hit(stderr))


def changed_files(repo: Path, revs: RevPair) -> list[str]:
    """Files the branch changed, relative to the merge base.

    A view over `file_changes` rather than its own `--name-only` call, so there is one parser
    of git change records and not two that can drift.
    """
    return file_changes(repo, revs).paths


def diff_line_count(repo: Path, revs: RevPair) -> int:
    """Total added + deleted lines -- the denominator of the compression metric (§10)."""
    out = run_git(repo, "diff", "--numstat", f"{revs.base_sha}..{revs.head_sha}")
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted = parts[0], parts[1]
        if added == "-" or deleted == "-":  # binary file
            continue
        total += int(added) + int(deleted)
    return total
