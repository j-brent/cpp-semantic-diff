"""The parser must be provably complete before it is trusted.

These assert the claim ARCH-001 §11 makes about this stage: a libclang that cannot reach
the compiler's builtin headers is rejected, rather than being allowed to return a
truncated AST that no later stage can detect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cpp_semantic_diff.errors import ClangToolchainError, PreconditionError
from cpp_semantic_diff.produce import toolchain as tc


def test_release_drops_vendor_noise() -> None:
    """The cache key must not move when packaging changes but the compiler does not."""
    assert tc.parse_release("Ubuntu clang version 18.1.3 (1ubuntu1)") == "18.1.3"
    assert tc.parse_release("clang version 20.1.2") == "20.1.2"
    assert tc.parse_release("Debian clang version 16.0.6 (27+b1)") == "16.0.6"


def test_release_rejects_unparseable() -> None:
    assert tc.parse_release("no version here") is None


def test_producer_id_is_stable_across_repackaging() -> None:
    """Two libclangs of the same release produce interchangeable models (ARCH-001 §9)."""
    ubuntu = tc.Toolchain(
        library=Path("/a"), version="Ubuntu clang 18.1.3 (1ubuntu1)", release="18.1.3"
    )
    debian = tc.Toolchain(library=Path("/b"), version="Debian clang 18.1.3 (3)", release="18.1.3")
    assert ubuntu.id == debian.id == "libclang 18.1.3"


def test_producer_id_changes_with_release() -> None:
    old = tc.Toolchain(library=Path("/a"), version="clang 18.1.3", release="18.1.3")
    new = tc.Toolchain(library=Path("/a"), version="clang 19.1.0", release="19.1.0")
    assert old.id != new.id


def test_toolchain_error_is_a_precondition() -> None:
    """Never caught to produce degraded output (ARCH-001 §11)."""
    assert issubclass(ClangToolchainError, PreconditionError)


def test_toolchain_error_names_the_silent_failure() -> None:
    """The message has to explain *why* this is fatal, or a user will try to skip it."""
    error = ClangToolchainError("broken", hint="truncated AST without raising")
    assert "truncated" in str(error)


class _FakeDiagnostic:
    def __init__(self, severity: int, spelling: str) -> None:
        self.severity = severity
        self.spelling = spelling
        self.location = type("Loc", (), {"file": type("F", (), {"name": "x.cpp"})(), "line": 7})()


class _FakeTu:
    def __init__(self, diagnostics: list[_FakeDiagnostic]) -> None:
        self.diagnostics = diagnostics


def test_fatal_diagnostics_ignores_warnings() -> None:
    tu = _FakeTu([_FakeDiagnostic(2, "unused variable")])
    assert tc.fatal_diagnostics(tu) == []


def test_fatal_diagnostics_reports_errors_and_above() -> None:
    """Error and Fatal both mean the AST past that point is not to be trusted."""
    tu = _FakeTu(
        [
            _FakeDiagnostic(2, "unused variable"),
            _FakeDiagnostic(3, "no member named foo"),
            _FakeDiagnostic(4, "'stddef.h' file not found"),
        ]
    )
    found = tc.fatal_diagnostics(tu)
    assert len(found) == 2
    assert "stddef.h" in found[1]


def test_candidates_are_existing_and_deduplicated() -> None:
    for path in tc.candidate_libraries():
        assert path.exists()
    found = tc.candidate_libraries()
    assert len(found) == len(set(found))


def test_env_override_is_preferred(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "libclang.so.1"
    fake.write_bytes(b"")
    monkeypatch.setenv("CPPSD_LIBCLANG", str(fake))
    assert tc.candidate_libraries()[0] == fake


@pytest.mark.fixtures
def test_real_toolchain_parses_builtin_headers() -> None:
    """The end-to-end claim: whatever this host resolves to, it parses stddef.h clean.

    Skipped rather than failed where no system LLVM exists, since that is a host
    property; `cppsd doctor` is the command that reports it as an error.
    """
    if not tc.candidate_libraries():
        pytest.skip("no system libclang on this host")
    loaded = tc.load()
    assert loaded.major >= tc.MINIMUM_MAJOR
    assert loaded.id.startswith("libclang ")
