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

import ctypes
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

#: Stands in for the target triple in `Toolchain.id` when the library will not report one.
#: Recording the ignorance rather than omitting the component is the point: an identity
#: that silently drops the target claims two hosts are interchangeable on the strength of a
#: question nobody asked (ARCH-001 §11, §12.1). It cannot collide with a real triple, and
#: `cppsd doctor` prints it, so the condition is visible before a cache is shared.
UNDETERMINED_TARGET = "target-undetermined"

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


class _CXTargetInfoImpl(ctypes.Structure):
    """Opaque to us, as it is to every C caller: only clang ever dereferences it."""


_CXTargetInfo = ctypes.POINTER(_CXTargetInfoImpl)


def library_target(tu: ci.TranslationUnit) -> str | None:
    """The normalized target triple this library parsed *that TU* for, or None.

    ARCH-001 §12.1 lists the target triple as a determinant of the AST second only to the
    parser itself: it decides `sizeof`, alignment, and every `__x86_64__`-conditional
    declaration in headers whose content is byte-identical across hosts. So the same
    libclang release on two machines can write two different partials under one key.

    Three ways to answer this and only one of them is sound.

    *Enumerating architecture macros* -- probe for `__x86_64__`, then `__aarch64__`, ... --
    is a hand-maintained list of the architectures that existed when it was written. It
    goes stale silently against a newer clang, which is the exact shape of the exception
    specification bug (STATUS §5): the fix there was to stop enumerating cases.

    *Asking `llvm-config --host-target`* asks a sibling binary. `CPPSD_LIBCLANG` can point
    at a library with no relationship to the `llvm-config` on PATH, and `find_scanner`
    already establishes what to do about a tool that disagrees with the parser -- refuse
    it, because it answers a different question. That would mean version-matching a second
    binary to learn something the parser already knows, and `--host-target` is in any case
    the *host* of the LLVM build, not the default target of this library.

    *Asking the verified library*, which is what this does. `clang_getTranslationUnitTargetInfo`
    has been in the stable C API since LLVM 8, far below `MINIMUM_MAJOR`; the Python
    bindings simply do not wrap it, so it is called through the raw handle exactly as
    `library_version` calls `clang_getClangVersion`. The answer comes from the same
    library, the same load, and the same parse that the probe already proved clean.

    Returns None rather than a guess if the symbol is absent or the call yields nothing --
    a library that will not say what it targets has not told us it targets what we assume.
    """
    lib = ci.conf.lib
    try:
        get_info = lib.clang_getTranslationUnitTargetInfo
        get_triple = lib.clang_TargetInfo_getTriple
        dispose = lib.clang_TargetInfo_dispose
    except AttributeError:  # pragma: no cover - present in every LLVM >= 8
        return None

    get_info.restype = _CXTargetInfo
    get_info.argtypes = [ci.c_object_p]
    get_triple.restype = ci._CXString
    get_triple.argtypes = [_CXTargetInfo]
    dispose.restype = None
    dispose.argtypes = [_CXTargetInfo]

    info = get_info(tu)
    if not info:  # pragma: no cover - only on a TU that failed to load at all
        return None
    try:
        triple = str(lib.clang_getCString(get_triple(info)))
    finally:
        dispose(info)
    return triple or None


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
    #: What the library reported for the probe parse, or None if it would not say.
    target: str | None = None

    @property
    def major(self) -> int:
        return int(self.release.split(".")[0])

    @property
    def id(self) -> str:
        """Recorded in `Completeness.producers` and folded into cache keys (ARCH-001 §9).

        A model built by a different parser is a different model, so this has to be
        stable, comparable, and specific enough to notice a version change.

        Two components, for the two ARCH-001 §12.1 determinants that nothing else in the
        key can see. The **release triple** identifies the parser -- and it is the release
        rather than the library's own version string precisely so a distro rebuild that
        changes nothing observable does not invalidate every cached model (STATUS §5). The
        **target triple** identifies the parse: the same headers under a different target
        yield different `sizeof`s and different conditional declarations with byte-identical
        content digests, so without it two hosts write two models under one key.

        The other four §12.1 determinants are covered by the key already and are
        deliberately *not* repeated here. The standard library, the resource directory's
        builtin headers, and anything `CPATH` drags in are content-hashed by
        `TuCache.key()` as members of the `clang-scan-deps` include closure -- measured:
        a TU including `<string>` and `<cstddef>` has a 151-file closure holding 68
        libstdc++ headers and 14 out of clang's own resource directory. The extractor is
        hashed by `_producer_code_digest`. A second, coarser statement of a fact the key
        already holds by content adds no detection and one more thing to keep true.

        `resource_dir` in particular must not appear here as a path. ARCH-001 §12.2 makes
        this value something two *hosts* compare, and a path would make two genuinely
        equivalent toolchains hard-fail on where their distribution puts things -- breaking
        the shared base cache §9 recommends, which is the scenario §12.1 exists to protect.
        """
        return f"libclang {self.release} {self.target or UNDETERMINED_TARGET}"

    def parse_args(self, args: list[str]) -> list[str]:
        if self.resource_dir is None:
            return args
        return [*args, f"-resource-dir={self.resource_dir}"]


def _probe(resource_dir: Path | None) -> ci.TranslationUnit:
    """Parse a trivial builtin-dependent TU and hand the caller the result.

    The TU rather than its diagnostics, because it answers two questions and only one of
    them is "is this library usable": `library_target` reads the target off this same
    parse, so the identity describes the library that was actually proved rather than one
    interrogated separately afterwards.
    """
    args = list(PROBE_ARGS)
    if resource_dir is not None:
        args.append(f"-resource-dir={resource_dir}")
    index = ci.Index.create()
    return index.parse(
        "cppsd_probe.cpp",
        args=args,
        unsaved_files=[("cppsd_probe.cpp", PROBE_SOURCE)],
    )


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

    probe = _probe(resource_dir)
    if failures := fatal_diagnostics(probe):
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

    # An undeterminable target is *recorded*, not raised on. ARCH-001 §11 refuses rather
    # than degrades, but the thing being refused there is a model that cannot be trusted,
    # and a host whose libclang parses builtin headers cleanly produces a perfectly good
    # model -- it just cannot prove to a second host that the two agree. Failing `doctor`
    # for that would refuse work that is sound. So the identity says so out loud, which
    # keeps it honest without making it fatal (ARCH-001 §12.1).
    _loaded = Toolchain(
        library=library,
        version=version,
        release=release,
        resource_dir=resource_dir,
        target=library_target(probe),
    )
    return _loaded
