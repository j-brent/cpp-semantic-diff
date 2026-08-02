"""Locating and verifying the C++ parser.

ARCH-001 §13 chose libclang over clangd. This module decides *which* libclang, and
proves it works before any real parsing happens.

The motivating failure: the `libclang` PyPI wheel ships the parsing engine but not the
compiler's builtin headers (`stddef.h`, `stdarg.h`, ...), which live in Clang's own
resource directory rather than in the OS. Without them the first `#include <stddef.h>`
is a fatal error, clang abandons the rest of the file, and libclang returns a
**truncated AST with no exception raised**. Measured on fmt's `src/format.cc`: 144,027
cursors instead of 159,343, and 3,474 USRs instead of 3,818.

That is the worst failure mode this project has. A two-point analysis built on models
that are consistently missing the same declarations does not report an error -- it
reports that those declarations do not exist, at both revisions, with total confidence.

So the parser is a system LLVM install, not the wheel: the engine and its headers come
from one package and are version-consistent by construction, and they match the
`clang-scan-deps` that feeds cache keys (ARCH-001 §9). Nothing here degrades -- an
unusable toolchain is a precondition failure (ARCH-001 §11).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from glob import glob
from pathlib import Path

import clang.cindex as ci

from cpp_semantic_diff.errors import ClangToolchainError

#: Bindings older than this do not know cursor kinds the v0 extraction scope relies on.
MINIMUM_MAJOR = 16

#: Last resort only. Everything above this in `candidate_libraries` *asks* the system
#: where its libraries are -- these are guesses about where a distribution puts them, and
#: a guess is wrong on any host that does not match. They exist because a machine can
#: have a usable LLVM with neither llvm-config on PATH nor an ldconfig cache entry
#: (unpacked tarballs, Homebrew, nix). They are not the primary mechanism.
LIBRARY_GLOBS = (
    "/usr/lib/llvm-*/lib/libclang.so*",
    "/usr/lib/*-linux-gnu/libclang-*.so*",
    "/usr/lib64/libclang.so*",
    "/usr/local/lib/libclang.so*",
    "/opt/homebrew/opt/llvm/lib/libclang.dylib",
    "/opt/homebrew/opt/llvm@*/lib/libclang.dylib",
    "/usr/local/opt/llvm/lib/libclang.dylib",
)

#: `libclang-cpp` is the C++ API, not the stable C API the bindings call through. It sits
#: right beside the real thing in every install and matches naive patterns, so it is
#: excluded by name rather than by hoping the probe rejects it later.
EXCLUDED_STEMS = ("libclang-cpp",)

#: Nothing here is exotic; it fails only when builtin headers are unreachable, which is
#: exactly the condition this module exists to catch.
PROBE_SOURCE = """
#include <stddef.h>
#include <stdarg.h>
struct cppsd_probe {
  size_t size;
  ptrdiff_t offset;
};
"""

PROBE_ARGS = ("-x", "c++", "-std=c++17")

#: `set_library_file` is process-global and rejects a second call once the library has
#: been loaded, so the resolved toolchain is memoized rather than re-derived.
_loaded: Toolchain | None = None


def path_version(path: str) -> int:
    """The LLVM major version a path advertises, or 0 if it advertises none.

    Only version-bearing components count: the `llvm-18` directory or the `-18`/`.so.18`
    on the library itself. Scanning the whole string for digits reads the `86` out of
    `x86_64-linux-gnu` and ranks an LLVM 16 above an LLVM 20 -- architecture tuples,
    `lib64`, and `libc6` all look like versions to a naive regex.
    """
    versions = [int(m) for m in re.findall(r"(?:llvm-|libclang-|\.so\.)(\d+)", path) if m.isdigit()]
    return max(versions, default=0)


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _llvm_config_libdirs() -> list[Path]:
    """Ask every llvm-config on PATH where its libraries live, newest first.

    Versioned names matter: Debian and Ubuntu ship `llvm-config-18` and frequently no
    unsuffixed `llvm-config` at all, so looking only for the bare name finds nothing on
    the exact systems this is meant to support.
    """
    names = ["llvm-config"] + [f"llvm-config-{v}" for v in range(30, MINIMUM_MAJOR - 1, -1)]
    found = [p for name in names if (p := shutil.which(name))]
    dirs: list[Path] = []
    for tool in sorted(found, key=lambda p: -path_version(p)):
        if libdir := _run([tool, "--libdir"]):
            dirs.append(Path(libdir))
    return dirs


def _ldconfig_libraries() -> list[Path]:
    """Ask the dynamic linker what it has, rather than guessing at directories.

    This is how a correctly installed library is found on Linux regardless of where the
    distribution chose to put it -- on this host it reports
    `/lib/x86_64-linux-gnu/libclang-18.so.18`, which no reasonable hardcoded glob covers.
    """
    out = _run(["ldconfig", "-p"]) or _run(["/sbin/ldconfig", "-p"])
    found: list[str] = []
    for line in out.splitlines():
        _, sep, path = line.partition("=>")
        if not sep:
            continue
        path = path.strip()
        stem = Path(path).name
        if stem.startswith("libclang") and not stem.startswith(EXCLUDED_STEMS):
            found.append(path)
    return [Path(p) for p in sorted(found, key=lambda p: -path_version(p))]


def candidate_libraries() -> list[Path]:
    """Every plausible libclang on this host, most preferred first.

    Ordered by how much the host is being *asked* versus *guessed at*: an explicit
    override, then llvm-config, then the dynamic linker's own cache, and only then
    directory patterns. Whatever comes back is still probe-verified before use -- ranking
    decides which working toolchain is picked, never whether a broken one is trusted.
    """
    candidates: list[Path] = []

    if override := os.environ.get("CPPSD_LIBCLANG"):
        candidates.append(Path(override))

    for libdir in _llvm_config_libdirs():
        candidates.extend(sorted(libdir.glob("libclang.so*"), key=lambda p: -path_version(str(p))))
        candidates.extend(libdir.glob("libclang.dylib"))

    candidates.extend(_ldconfig_libraries())

    for pattern in LIBRARY_GLOBS:
        candidates.extend(Path(p) for p in sorted(glob(pattern), key=lambda p: -path_version(p)))

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen or not path.exists() or path.is_dir():
            continue
        if path.name.startswith(EXCLUDED_STEMS):
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def library_version(config: ci.Config) -> str:
    """The loaded library's own version string, not the bindings'.

    Read through the raw handle: the Python bindings do not wrap `clang_getClangVersion`.
    """
    fn = config.lib.clang_getClangVersion
    fn.restype = ci._CXString
    fn.argtypes = []
    return str(ci.conf.lib.clang_getCString(fn()))


def parse_release(version: str) -> str | None:
    """`18.1.3` out of `Ubuntu clang version 18.1.3 (1ubuntu1)`.

    The vendor prefix and packaging suffix are deliberately dropped: the release triple
    is what changes parser behaviour, and it is what belongs in a cache key. Keying on
    the raw string would invalidate every cached model on a distro rebuild that changed
    nothing observable.
    """
    match = re.search(r"(\d+\.\d+\.\d+)", version)
    return match.group(1) if match else None


def fatal_diagnostics(tu: ci.TranslationUnit) -> list[str]:
    """Diagnostics severe enough that the AST past them is not to be trusted.

    Per-TU counterpart to the startup probe. A file can parse incompletely for reasons
    the probe cannot anticipate -- a header the build generates, a flag the compile
    database records but this compiler rejects -- and libclang signals none of it by
    raising. Every caller that parses must consult this (ARCH-001 §4.4).
    """
    return [
        f"{d.location.file.name if d.location.file else '<unknown>'}:"
        f"{d.location.line}: {d.spelling}"
        for d in tu.diagnostics
        if d.severity >= ci.Diagnostic.Error
    ]


@dataclass(frozen=True)
class Toolchain:
    """A libclang proven able to parse builtin headers."""

    library: Path
    version: str
    release: str
    resource_dir: Path | None = None

    @property
    def major(self) -> int:
        return int(self.release.split(".")[0])

    @property
    def id(self) -> str:
        """Recorded in `Completeness.producers` and folded into cache keys (ARCH-001 §9).

        A model built by a different parser is a different model, so this has to be
        stable, comparable, and specific enough to notice a version change.
        """
        return f"libclang {self.release}"

    def parse_args(self, args: list[str]) -> list[str]:
        if self.resource_dir is None:
            return args
        return [*args, f"-resource-dir={self.resource_dir}"]


def _probe(config: ci.Config, resource_dir: Path | None) -> list[str]:
    """Parse a trivial builtin-dependent TU. Returns fatal diagnostics, empty if healthy."""
    args = list(PROBE_ARGS)
    if resource_dir is not None:
        args.append(f"-resource-dir={resource_dir}")
    index = ci.Index.create()
    tu = index.parse(
        "cppsd_probe.cpp",
        args=args,
        unsaved_files=[("cppsd_probe.cpp", PROBE_SOURCE)],
    )
    return fatal_diagnostics(tu)


def load() -> Toolchain:
    """Resolve, load, and verify the parser. Raises rather than returning a doubtful one."""
    global _loaded
    if _loaded is not None:
        return _loaded

    candidates = candidate_libraries()
    if not candidates:
        raise ClangToolchainError(
            "No system libclang found.",
            searched=[*LIBRARY_GLOBS, "llvm-config --libdir", "$CPPSD_LIBCLANG"],
        )

    library = candidates[0]
    try:
        ci.Config.set_library_file(str(library))
        config = ci.Config()
        version = library_version(config)
    # Broad by necessity: cindex raises bare `Exception` for a double `set_library_file`,
    # and a wrong-architecture .so surfaces as OSError out of ctypes.
    except Exception as exc:
        raise ClangToolchainError(
            f"Found {library} but could not load it: {exc}",
            searched=[str(c) for c in candidates],
        ) from exc

    release = parse_release(version)
    if release is None:
        raise ClangToolchainError(f"Could not read a version out of {version!r} ({library}).")
    if int(release.split(".")[0]) < MINIMUM_MAJOR:
        raise ClangToolchainError(
            f"{library} is {version}, but LLVM {MINIMUM_MAJOR} or newer is required.",
        )

    # An explicit resource dir is an escape hatch, not a fallback: the probe below runs
    # either way, so a truncating toolchain cannot slip through by taking this branch.
    override = os.environ.get("CPPSD_RESOURCE_DIR")
    resource_dir = Path(override) if override else None

    if failures := _probe(config, resource_dir):
        raise ClangToolchainError(
            f"{library} ({version}) cannot parse the compiler's own builtin headers.\n"
            + "\n".join(f"  {f}" for f in failures[:3]),
            hint=(
                "This is the silent-truncation failure mode: libclang would return a "
                "partial AST without raising. Install the matching builtin headers "
                "(Debian/Ubuntu: libclang-common-<version>-dev), or point "
                "CPPSD_RESOURCE_DIR at a directory containing stddef.h."
            ),
        )

    _loaded = Toolchain(
        library=library,
        version=version,
        release=release,
        resource_dir=resource_dir,
    )
    return _loaded
