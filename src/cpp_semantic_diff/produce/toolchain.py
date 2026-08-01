"""Locating and verifying the C++ parser.

ARCH-001 §12 chose libclang over clangd. This module decides *which* libclang, and
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

#: Searched in order after the environment override and llvm-config.
LIBRARY_GLOBS = (
    "/usr/lib/llvm-*/lib/libclang.so.1",
    "/usr/lib/x86_64-linux-gnu/libclang-*.so.1",
    "/usr/lib/aarch64-linux-gnu/libclang-*.so.1",
    "/usr/local/lib/libclang.so.1",
    "/opt/homebrew/opt/llvm/lib/libclang.dylib",
)

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


def _version_sort_key(path: str) -> tuple[int, str]:
    """Newest LLVM first, so a box with several installs gets the most capable one."""
    found = re.findall(r"(\d+)", path)
    return (-int(found[0]) if found else 0, path)


def candidate_libraries() -> list[Path]:
    """Every plausible libclang on this host, most preferred first."""
    candidates: list[Path] = []

    if override := os.environ.get("CPPSD_LIBCLANG"):
        candidates.append(Path(override))

    if llvm_config := shutil.which("llvm-config"):
        try:
            libdir = subprocess.run(
                [llvm_config, "--libdir"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
        else:
            if libdir:
                candidates.extend(Path(libdir) / n for n in ("libclang.so.1", "libclang.so"))

    for pattern in LIBRARY_GLOBS:
        candidates.extend(Path(p) for p in sorted(glob(pattern), key=_version_sort_key))

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        if path not in seen and path.exists():
            seen.add(path)
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
