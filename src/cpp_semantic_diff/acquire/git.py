"""Git interrogation and revision resolution.

ARCH-001 §3.1. The one decision encoded here that is easy to get wrong: the base is
``merge-base(main, HEAD)``, not ``main``. Diffing against main's tip attributes every change
other people merged while the branch was open to the branch under review.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from cpp_semantic_diff.errors import (
    GitError,
    NoMergeBaseError,
    NotAGitRepositoryError,
    ShallowRepositoryError,
    UnknownRefError,
)

#: Tried in order when no base ref is given and origin/HEAD is unset.
DEFAULT_BRANCH_CANDIDATES = ("main", "master")


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc.stdout.strip()


def is_git_repository(repo: Path) -> bool:
    try:
        return run_git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    except (GitError, FileNotFoundError):
        return False


def is_shallow(repo: Path) -> bool:
    return run_git(repo, "rev-parse", "--is-shallow-repository") == "true"


def ref_exists(repo: Path, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
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
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", base_ref, head_ref],
        capture_output=True,
        text=True,
    )
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


def changed_files(repo: Path, revs: RevPair) -> list[str]:
    """Files the branch changed, relative to the merge base."""
    out = run_git(repo, "diff", "--name-only", f"{revs.base_sha}..{revs.head_sha}")
    return [line for line in out.splitlines() if line]


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
