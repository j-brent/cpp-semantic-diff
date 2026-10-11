"""Error taxonomy.

ARCH-001 §11: conditions that eliminate the analysis are hard failures with actionable
messages, never partial output. `PreconditionError` and its subclasses are that category --
they are raised before any analysis runs and are never caught to produce a degraded result.
"""

from __future__ import annotations

import shlex
from enum import IntEnum


class ExitCode(IntEnum):
    """What `cppsd` exits with, one code per kind of failure, so a caller can branch on it.

    1 is left to Python: an uncaught exception is a defect in this tool, and its traceback is
    the report. 2 is what Click already uses for a malformed command line, so an invalid
    setting reached through the environment shares it -- both are input the caller can fix.
    """

    OK = 0
    INTERNAL = 1  #: an uncaught exception -- a bug in this tool
    USAGE = 2  #: a malformed command line, option value or environment setting
    REPOSITORY = 3  #: the repository or the revisions cannot be compared
    BUILD = 4  #: no usable compile database at a revision
    TOOLCHAIN = 5  #: no parser that can be trusted
    FAILED = 6  #: an operation failed while running: git, a worker pool, writing output


class SemanticDiffError(Exception):
    """Base for every error this package raises."""

    exit_code: ExitCode = ExitCode.FAILED


class PreconditionError(SemanticDiffError):
    """A condition that eliminates the analysis entirely (ARCH-001 §11).

    Never downgrade one of these into a warning. The whole point is that absence of
    evidence must not be presented as evidence of absence.
    """

    exit_code = ExitCode.REPOSITORY


class InvalidSettingError(PreconditionError):
    """An option or environment value that cannot be used as given.

    Refused rather than replaced with a default where the value decides the answer, because
    running with a setting other than the one asked for is a quiet disagreement (ARCH-001 §11).
    """

    exit_code = ExitCode.USAGE


class ThirdPartyPathError(InvalidSettingError):
    """A `--third-party` path that cannot name a directory in either revision.

    Refused rather than ignored: a mistyped path would silently leave the library it meant
    in the model (ADR-025).
    """


class GitNotFoundError(PreconditionError):
    def __init__(self) -> None:
        super().__init__("git is not installed, or not on PATH.")


class EmptyComparisonError(PreconditionError):
    """Head is the merge base, so there is no second point to analyze."""


class ShallowRepositoryError(PreconditionError):
    """No second point exists, so no two-point analysis is possible."""

    def __init__(self, repo: str) -> None:
        super().__init__(
            f"{repo} is a shallow clone, so there is no history to diff against.\n"
            f"Fix with:  git -C {shlex.quote(repo)} fetch --unshallow"
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

    exit_code = ExitCode.BUILD

    def __init__(self, rev: str, path: str, detail: str) -> None:
        super().__init__(
            f"No usable compile database for {rev} (looked in {path}).\n"
            f"{detail}\n"
            f"A compile database is required at both revisions; configure with\n"
            f"  cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
        )


class ConfigureError(PreconditionError):
    """The base did not configure. ARCH-001 §3.1 treats this as terminal by design."""

    exit_code = ExitCode.BUILD

    def __init__(self, rev: str, detail: str) -> None:
        super().__init__(
            f"Configure failed at {rev}.\n{detail}\n"
            f"The base revision is expected to build. This tool does not analyze a broken base."
        )


class ClangToolchainError(PreconditionError):
    """No libclang that can be trusted to parse completely (ARCH-001 §11, §13).

    Terminal by design. The alternative is a parser that returns truncated ASTs without
    raising, which yields two models that agree on the same missing declarations -- an
    analysis that is confidently, invisibly wrong.
    """

    exit_code = ExitCode.TOOLCHAIN

    def __init__(
        self,
        detail: str,
        *,
        searched: list[str] | None = None,
        hint: str | None = None,
    ) -> None:
        parts = [detail]
        if searched:
            parts.append("Searched:\n" + "\n".join(f"  {s}" for s in searched))
        parts.append(
            hint
            or "Install a system LLVM with its development headers. The package names "
            "differ by distribution:\n"
            "  Debian/Ubuntu  apt install libclang1-<v> libclang-common-<v>-dev\n"
            "  Fedora/RHEL    dnf install clang-libs clang-devel\n"
            "  Arch           pacman -S clang\n"
            "  macOS          brew install llvm\n"
            "Or set CPPSD_LIBCLANG to a libclang shared library directly."
        )
        super().__init__("\n".join(parts))


class ToolchainMismatchError(PreconditionError):
    """The two models were not produced by the same toolchain (ARCH-001 §12.2).

    A two-point analysis subtracts one model from another. If the two were produced by
    different parsers, different system headers or different target triples, the
    subtraction yields events that describe the *environment* rather than the change --
    `std::string` gaining a member because the base was parsed against libstdc++ 12 and the
    head against 14 is a `NodeAdded` like any other, rendered with full confidence and
    indistinguishable in the output from a real one.

    That is worse than anything in §11, which is why it is refused rather than flagged:
    §11's failures are missed changes, and this one is an invented change.
    """

    exit_code = ExitCode.TOOLCHAIN

    def __init__(self, base: str, head: str, *, differs: list[str]) -> None:
        self.base = base
        self.head = head
        self.differs = differs
        super().__init__(
            "The two models were produced by different toolchains, so their difference "
            "would describe the environment rather than the change (ARCH-001 §12.2).\n"
            f"  base  {base or '<no toolchain recorded>'}\n"
            f"  head  {head or '<no toolchain recorded>'}\n"
            f"  disagreeing record(s): {', '.join(differs)}\n"
            "Build both revisions in one run, or with one toolchain: an identity is the "
            "libclang release triple and the target it parses for, and both sides must "
            "agree before a delta between them means anything."
        )


class TranslationUnitError(SemanticDiffError):
    """One TU failed to parse. Recorded in `Completeness`, not fatal on its own."""

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"{path}: {detail}")


class GitError(SemanticDiffError):
    def __init__(self, argv: list[str], code: int, stderr: str) -> None:
        self.argv = argv
        self.code = code
        self.stderr = stderr
        super().__init__(f"git {' '.join(argv)} failed ({code}): {stderr.strip()}")


class OutputError(SemanticDiffError):
    """The document was produced but could not be written where it was asked for."""
