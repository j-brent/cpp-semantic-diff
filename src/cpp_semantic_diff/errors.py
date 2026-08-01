"""Error taxonomy.

ARCH-001 §11: conditions that eliminate the analysis are hard failures with actionable
messages, never partial output. `PreconditionError` and its subclasses are that category --
they are raised before any analysis runs and are never caught to produce a degraded result.
"""

from __future__ import annotations


class SemanticDiffError(Exception):
    """Base for every error this package raises."""


class PreconditionError(SemanticDiffError):
    """A condition that eliminates the analysis entirely (ARCH-001 §11).

    Never downgrade one of these into a warning. The whole point is that absence of
    evidence must not be presented as evidence of absence.
    """


class ShallowRepositoryError(PreconditionError):
    """No second point exists, so no two-point analysis is possible."""

    def __init__(self, repo: str) -> None:
        super().__init__(
            f"{repo} is a shallow clone, so there is no history to diff against.\n"
            f"Fix with:  git -C {repo} fetch --unshallow"
        )


class NoMergeBaseError(PreconditionError):
    def __init__(self, base_ref: str, head_ref: str) -> None:
        super().__init__(
            f"No merge base between {base_ref!r} and {head_ref!r}: they share no history.\n"
            f"Check that {base_ref!r} is the branch this work actually forked from."
        )


class UnknownRefError(PreconditionError):
    def __init__(self, ref: str, repo: str) -> None:
        super().__init__(
            f"Ref {ref!r} does not resolve in {repo}.\n"
            f"Pass an explicit base with --base, or fetch the missing ref."
        )


class NotAGitRepositoryError(PreconditionError):
    def __init__(self, path: str) -> None:
        super().__init__(f"{path} is not a git repository.")


class CompileDatabaseError(PreconditionError):
    """ARCH-001 §3.1: a compile database is required at both revisions."""

    def __init__(self, rev: str, path: str, detail: str) -> None:
        super().__init__(
            f"No usable compile database for {rev} (looked in {path}).\n"
            f"{detail}\n"
            f"A compile database is required at both revisions; configure with\n"
            f"  cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
        )


class ConfigureError(PreconditionError):
    """The base did not configure. ARCH-001 §3.1 treats this as terminal by design."""

    def __init__(self, rev: str, detail: str) -> None:
        super().__init__(
            f"Configure failed at {rev}.\n{detail}\n"
            f"The base revision is expected to build. This tool does not analyze a broken base."
        )


class GitError(SemanticDiffError):
    def __init__(self, argv: list[str], code: int, stderr: str) -> None:
        self.argv = argv
        self.code = code
        self.stderr = stderr
        super().__init__(f"git {' '.join(argv)} failed ({code}): {stderr.strip()}")
