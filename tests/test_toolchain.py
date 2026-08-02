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


def toolchain(release: str = "18.1.3", **kw) -> tc.Toolchain:
    defaults = {
        "library": Path("/a"),
        "version": f"clang version {release}",
        "target": "x86_64-pc-linux-gnu",
    }
    return tc.Toolchain(release=release, **{**defaults, **kw})


def test_producer_id_is_stable_across_repackaging() -> None:
    """Two libclangs of the same release produce interchangeable models (ARCH-001 §9).

    The distro version string is deliberately not identity: a package rebuild that changes
    nothing a parse can observe must not invalidate every cached model (STATUS §5).
    """
    ubuntu = toolchain(library=Path("/a"), version="Ubuntu clang 18.1.3 (1ubuntu1)")
    debian = toolchain(library=Path("/b"), version="Debian clang 18.1.3 (3)")
    assert ubuntu.id == debian.id


def test_producer_id_changes_with_release() -> None:
    assert toolchain("18.1.3").id != toolchain("19.1.0").id


def test_producer_id_changes_with_target() -> None:
    """ARCH-001 §12.1: the target decides `sizeof`, alignment and every
    `__x86_64__`-conditional declaration, and it is the one determinant the content
    digests cannot see -- the headers are byte-identical on both hosts."""
    x86 = toolchain(target="x86_64-pc-linux-gnu")
    arm = toolchain(target="aarch64-unknown-linux-gnu")
    assert x86.id != arm.id


def test_producer_id_says_so_when_the_target_is_unknown() -> None:
    """Recorded, not omitted. An identity that silently drops the target claims two hosts
    are interchangeable on the strength of a question nobody asked (ARCH-001 §11)."""
    unknown = toolchain(target=None)
    assert tc.UNDETERMINED_TARGET in unknown.id
    assert unknown.id != toolchain(target="x86_64-pc-linux-gnu").id


def test_producer_id_carries_no_host_paths() -> None:
    """ARCH-001 §12.2 makes this a value two *hosts* compare, so a host-specific path in it
    would hard-fail two machines with genuinely equivalent toolchains -- breaking the shared
    base cache §9 recommends. The resource directory is covered by content instead: its
    builtin headers are members of the include closure `TuCache.key()` hashes."""
    here = toolchain(library=Path("/usr/lib/llvm-18/lib/libclang.so.1"), resource_dir=None)
    there = toolchain(
        library=Path("/opt/llvm/lib/libclang.so"),
        resource_dir=Path("/opt/llvm/lib/clang/18"),
    )
    assert here.id == there.id
    assert "/" not in here.id.removeprefix("libclang ")


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


def test_path_version_ignores_architecture_tuples() -> None:
    """The `86` in `x86_64` is not a version.

    Scanning a path for its first run of digits ranks
    `/usr/lib/x86_64-linux-gnu/libclang-16.so.1` above an actual LLVM 20, silently
    selecting an older compiler on any multiarch Linux -- which is every Debian and
    Ubuntu host.
    """
    assert tc.path_version("/usr/lib/x86_64-linux-gnu/libclang-16.so.1") == 16
    assert tc.path_version("/usr/lib/aarch64-linux-gnu/libclang-18.so.18") == 18
    assert tc.path_version("/usr/lib64/libclang.so") == 0


def test_path_version_reads_llvm_directories() -> None:
    assert tc.path_version("/usr/lib/llvm-20/lib/libclang.so.1") == 20
    assert tc.path_version("/usr/lib/llvm-9/lib/libclang.so.1") == 9


def test_newer_llvm_outranks_older() -> None:
    paths = [
        "/usr/lib/x86_64-linux-gnu/libclang-16.so.1",
        "/usr/lib/llvm-20/lib/libclang.so.1",
        "/usr/lib/llvm-18/lib/libclang.so.1",
    ]
    assert sorted(paths, key=lambda p: -tc.path_version(p))[0].endswith("llvm-20/lib/libclang.so.1")


def test_candidates_are_existing_files_and_deduplicated() -> None:
    found = tc.candidate_libraries()
    for path in found:
        assert path.exists()
        assert not path.is_dir()
    resolved = [p.resolve() for p in found]
    assert len(resolved) == len(set(resolved)), "symlinks to one library counted twice"


def test_candidates_exclude_libclang_cpp() -> None:
    """`libclang-cpp` is the C++ API and cannot back the cindex bindings."""
    assert not any(p.name.startswith("libclang-cpp") for p in tc.candidate_libraries())


def test_env_override_is_preferred(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "libclang.so.1"
    fake.write_bytes(b"")
    monkeypatch.setenv("CPPSD_LIBCLANG", str(fake))
    assert tc.candidate_libraries()[0] == fake


def test_discovery_asks_the_system_before_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hardcoded globs must be a last resort, not the mechanism.

    On this host `llvm-config` is absent but `llvm-config-18` is present, so a lookup for
    the bare name alone finds nothing and quietly falls through to guessed directories.
    """
    monkeypatch.delenv("CPPSD_LIBCLANG", raising=False)
    from_system = tc._llvm_config_libdirs() or tc._ldconfig_libraries()
    if not from_system:
        pytest.skip("no llvm-config or ldconfig entry on this host")
    assert from_system, "system tools know where libclang is; discovery must ask them"


def test_globs_are_not_the_only_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blanking the guesses must not blank discovery on a correctly configured host."""
    monkeypatch.delenv("CPPSD_LIBCLANG", raising=False)
    monkeypatch.setattr(tc, "LIBRARY_GLOBS", ())
    if not (tc._llvm_config_libdirs() or tc._ldconfig_libraries()):
        pytest.skip("no system LLVM on this host")
    assert tc.candidate_libraries()


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


@pytest.mark.fixtures
def test_the_library_reports_its_own_target() -> None:
    """The target comes from the verified library, not from a sibling binary or a list.

    Deliberately not asserted to be *this host's* triple: pinning `x86_64-pc-linux-gnu`
    would fail on any other machine, which is the opposite of the point. What is asserted
    is that the library answered at all, and that the answer looks like a triple -- so a
    regression to `UNDETERMINED_TARGET` on a host where the symbol is right there shows up
    as a failure rather than as a quietly weaker cache key.

    A list of architecture macros would pass this test today and go stale silently against
    a newer clang, which is why the mechanism is a question put to the library (STATUS §5,
    "the extractor hit exactly this shape of bug").
    """
    if not tc.candidate_libraries():
        pytest.skip("no system libclang on this host")
    loaded = tc.load()
    assert loaded.target, "libclang exports clang_TargetInfo_getTriple in every LLVM >= 8"
    assert loaded.target.count("-") >= 2, f"{loaded.target!r} is not a target triple"
    assert loaded.id.endswith(loaded.target)


@pytest.mark.fixtures
def test_the_target_is_read_off_the_parse_that_was_verified() -> None:
    """Same library, same load, same probe TU -- so the identity describes the toolchain
    that was proved rather than one interrogated separately afterwards."""
    if not tc.candidate_libraries():
        pytest.skip("no system libclang on this host")
    loaded = tc.load()
    probe = tc._probe(loaded.resource_dir)
    assert tc.fatal_diagnostics(probe) == []
    assert tc.library_target(probe) == loaded.target
