"""One translation unit in, one partial model out.

ARCH-001 §4 (what a model contains) and §9 (what a cache stores). The unit here is
deliberately the **translation unit**, not the worktree: §9 keys the cache on
`hash(TU content, transitive header content, flags, producer version)` and assembles a
commit's model from cached partials, so the boundary the cache needs has to be the
boundary the extractor already produces. Extracting a whole worktree in one pass would
have to be taken apart again to cache it.

Two consequences follow from that choice, and both are correctness issues rather than
tidiness:

**Every header is parsed once per TU that includes it.** A `TuExtract` therefore describes far
more than its own file, and the same entity appears in every TU that sees its declaration.
Nodes are keyed by USR so that merging is idempotent (ARCH-001 §5).

**A partial model has dangling edges by construction.** A call whose target is declared in
a header this TU does not include still belongs in the graph; it just cannot be resolved
here. Edges are emitted optimistically and resolved against the merged node set by the
builder, which is the only place that knows the whole picture.
"""

from __future__ import annotations

import ctypes
import hashlib
import itertools
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import clang.cindex as ci

from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.model.ir import (
    COMPILER_BUILTINS,
    FUNCTION_LOCAL,
    IMPLICIT,
    TEMPLATE_INSTANTIATION,
    UNRESOLVED,
    UNTRACKED,
    AttrValue,
    Crossing,
    Edge,
    EdgeKind,
    Location,
    Node,
    NodeId,
    NodeKind,
    TuResult,
    TuStatus,
    usr_file,
)
from cpp_semantic_diff.produce.toolchain import Toolchain, fatal_diagnostics

#: The extraction scope (ARCH-001 §13): namespace, class, function, method, field, and the
#: entities that carry values -- enum, enumerator, alias, and variable at namespace scope or
#: as a static data member (ADR-038). Macros and static assertions are modelled too, each on
#: a path of its own (`_Extractor.visit_macro`, `_Extractor.visit_static_assertion`,
#: ADR-039). Anything not named here is not modelled. Adding a kind is additive, and each
#: kind's compared facts need a novelty weight in `rank/weights.py`.
#:
#: Templates map onto their underlying kind with `attrs["template"]`. A template and its
#: instantiations need separate identity (ARCH-001 §5), and this gets that for free: only
#: *declared* templates appear as cursors, so implicit instantiations never enter the model
#: and cannot produce instantiation-only deltas.
#:
#: Two of the mappings here are *defaults* that `_node_kind` refines, both for the same
#: reason -- clang files a template under one cursor kind whatever it was written as.
#: CLASS_TEMPLATE covers `struct` too, and FUNCTION_TEMPLATE covers a member function
#: template just as it covers a free one.
MODELLED_KINDS: dict[ci.CursorKind, NodeKind] = {
    ci.CursorKind.NAMESPACE: NodeKind.NAMESPACE,
    ci.CursorKind.CLASS_DECL: NodeKind.CLASS,
    ci.CursorKind.CLASS_TEMPLATE: NodeKind.CLASS,
    ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION: NodeKind.CLASS,
    ci.CursorKind.STRUCT_DECL: NodeKind.STRUCT,
    ci.CursorKind.UNION_DECL: NodeKind.UNION,
    ci.CursorKind.FUNCTION_DECL: NodeKind.FUNCTION,
    ci.CursorKind.FUNCTION_TEMPLATE: NodeKind.FUNCTION,  # unless it is a member; see below
    ci.CursorKind.CXX_METHOD: NodeKind.METHOD,
    ci.CursorKind.CONSTRUCTOR: NodeKind.METHOD,
    ci.CursorKind.DESTRUCTOR: NodeKind.METHOD,
    ci.CursorKind.CONVERSION_FUNCTION: NodeKind.METHOD,
    ci.CursorKind.FIELD_DECL: NodeKind.FIELD,
    ci.CursorKind.ENUM_DECL: NodeKind.ENUM,
    ci.CursorKind.ENUM_CONSTANT_DECL: NodeKind.ENUMERATOR,
    ci.CursorKind.TYPEDEF_DECL: NodeKind.ALIAS,
    ci.CursorKind.TYPE_ALIAS_DECL: NodeKind.ALIAS,
    ci.CursorKind.TYPE_ALIAS_TEMPLATE_DECL: NodeKind.ALIAS,
    ci.CursorKind.VAR_DECL: NodeKind.VARIABLE,  # unless it is local; see below
}

#: What no model this extractor builds can show, stated on every run (ADR-029). libclang
#: does not expose the copy and move operations the compiler writes, so adding a destructor --
#: which removes a class's implicit move operations and turns every move into a copy --
#: changes nothing the model holds. It is not worked around; a run that cannot see a change
#: says so rather than reporting that nothing changed (ARCH-001 §11). Declarations the
#: extractor skips by choice are counted instead (`TuExtract.unmodelled`).
LIBCLANG_LIMITS: tuple[str, ...] = (
    "Implicit special members are not visible. libclang does not expose the copy and move "
    "operations the compiler writes, so a change that adds or removes one implicitly -- adding "
    "a destructor removes the implicit move operations -- produces no change event.",
)

#: Kinds whose children are worth descending into. A function body is walked separately
#: for calls; nothing else nests declarations that v0 models.
CONTAINER_KINDS = frozenset(
    {
        ci.CursorKind.NAMESPACE,
        ci.CursorKind.ENUM_DECL,  # its enumerators
        ci.CursorKind.CLASS_DECL,
        ci.CursorKind.CLASS_TEMPLATE,
        ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
        ci.CursorKind.STRUCT_DECL,
        ci.CursorKind.UNION_DECL,
        ci.CursorKind.UNEXPOSED_DECL,  # a linkage spec, from a libclang older than 18
        # `extern "C" { ... }` and the one-line `extern "C" void f();` alike, from libclang 18
        # on. Missing, the extractor stopped at the linkage spec and nothing declared in it --
        # often a library's whole C API -- was in the model.
        ci.CursorKind.LINKAGE_SPEC,
    }
)

CALLABLE_KINDS = frozenset(
    {
        ci.CursorKind.FUNCTION_DECL,
        ci.CursorKind.FUNCTION_TEMPLATE,
        ci.CursorKind.CXX_METHOD,
        ci.CursorKind.CONSTRUCTOR,
        ci.CursorKind.DESTRUCTOR,
        ci.CursorKind.CONVERSION_FUNCTION,
    }
)

#: Cursor kinds that stand for a templated record. Clang files `template <...> struct X`
#: under CLASS_TEMPLATE just as it does `class`, so the cursor kind alone cannot say which
#: tag was written and the node kind has to be asked for separately.
TEMPLATED_RECORD_KINDS = frozenset(
    {
        ci.CursorKind.CLASS_TEMPLATE,
        ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
    }
)

#: The tag a templated record was written with, as a node kind.
TAG_KINDS: dict[ci.CursorKind, NodeKind] = {
    ci.CursorKind.STRUCT_DECL: NodeKind.STRUCT,
    ci.CursorKind.CLASS_DECL: NodeKind.CLASS,
    ci.CursorKind.UNION_DECL: NodeKind.UNION,
}

#: The class-like scopes: a declaration whose *semantic* parent is one of these is a member.
#: All five are what clang actually reports as the semantic parent of a member -- `class`,
#: `struct` and `union` bodies, a class template's body, and a partial specialization's --
#: and a full specialization is a plain CLASS_DECL/STRUCT_DECL, so it needs no entry of its
#: own. A member of a class template is a member.
RECORD_KINDS = TEMPLATED_RECORD_KINDS | {
    ci.CursorKind.CLASS_DECL,
    ci.CursorKind.STRUCT_DECL,
    ci.CursorKind.UNION_DECL,
}

#: Function bodies are deliberately *not* skipped. `calls` edges and `body_hash` both need
#: them, and the spike found bodies cost most of the parse time while yielding few of the USRs
#: -- a real two-tier lever later, but not one v0 can pull.
#:
#: Compiler flags that are meaningless without a build and actively harmful to reuse:
#: `-c`/`-o` name outputs, and `-Werror` turns a warning into a fatal diagnostic, which
#: would report a TU as unparseable when its AST is fine.
DROPPED_FLAGS = ("-c", "-Werror")
DROPPED_FLAG_PAIRS = ("-o",)


def _mangled(cursor: ci.Cursor) -> str | None:
    """The linker symbol, when the entity has one.

    ARCH-001 §4.1: this is the join key to abidiff/bloaty/nm, populated from v0 even though
    the evidence layer is deferred, because adding it later invalidates every cached model.
    Templates, and anything without linkage, legitimately have none.
    """
    try:
        return cursor.mangled_name or None
    # Broad by necessity: cindex surfaces mangling failures as bare exceptions out of the
    # ctypes layer, and an unmanglable cursor is normal rather than exceptional.
    except Exception:
        return None


def specialized_template_usr(cursor: ci.Cursor) -> str | None:
    """The template a cursor was instantiated from, if it is an instantiation.

    Nothing calls a template -- calls resolve to an *instantiation*, whose USR
    (`...@F@ignore_unused<#L>#&1L#`) is not the template's (`...@FT@>1#T...`) and belongs to no
    declaration anyone wrote. On fmt this was 10,158 call edges, roughly a third of the graph
    (commit `8080f19`, 2026-08-01), so treating it as a curiosity is not an option.

    The answer is deliberately *not* applied here. Explicit specializations are hand-written
    code with their own identity and must not be collapsed into their primary template
    (ARCH-001 §5), and only the merged node set can tell which case a USR is. So this is
    recorded as a candidate alias and resolved by the builder, which knows.
    """
    template = ci.conf.lib.clang_getSpecializedCursorTemplate(cursor)
    if template is None:
        return None
    usr = template.get_usr()
    return usr or None


class _Overrides:
    """`clang_getOverriddenCursors`, which the Python bindings do not wrap.

    Worth reaching through to rather than reconstructing: deciding which base method a
    derived method overrides means handling covariant returns, `using`-declarations, and
    multiple inheritance. Clang already computed it, and a hand-rolled name-and-signature
    match would be quietly wrong on exactly the code most worth reviewing.
    """

    def __init__(self) -> None:
        lib = ci.conf.lib
        self._get = lib.clang_getOverriddenCursors
        self._get.argtypes = [
            ci.Cursor,
            ctypes.POINTER(ctypes.POINTER(ci.Cursor)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._get.restype = None
        self._dispose = lib.clang_disposeOverriddenCursors
        self._dispose.argtypes = [ctypes.POINTER(ci.Cursor)]

    def __call__(self, cursor: ci.Cursor) -> list[ci.Cursor]:
        array = ctypes.POINTER(ci.Cursor)()
        count = ctypes.c_uint()
        self._get(cursor, ctypes.byref(array), ctypes.byref(count))
        if not count.value:
            return []
        try:
            cursors = []
            for i in range(count.value):
                # Copied out of the array, which is freed below. A cursor's own data points
                # into the TU's AST, not into the array, so the copy stays valid.
                overridden = ci.Cursor.from_buffer_copy(array[i])
                # The bindings normally attach the owning TU in `from_result`; a cursor
                # lifted straight out of the C array has to be told, or `get_usr` faults.
                overridden._tu = cursor._tu
                cursors.append(overridden)
            return cursors
        finally:
            self._dispose(array)


_overridden_cursors: _Overrides | None = None


def overridden_cursors(cursor: ci.Cursor) -> list[ci.Cursor]:
    global _overridden_cursors
    if _overridden_cursors is None:
        _overridden_cursors = _Overrides()
    return _overridden_cursors(cursor)


def overridden_usrs(cursor: ci.Cursor) -> list[str]:
    return [c.get_usr() for c in overridden_cursors(cursor)]


class _TemplateTag:
    """`clang_getTemplateCursorKind`, which the Python bindings do not wrap.

    The tag a templated record was written with is not recoverable from the cursor kind:
    CLASS_TEMPLATE covers `class`, `struct` and `union` alike. Reading `class` off it split
    fmt's `formatter` 54 class / 43 struct under one qualified name -- full specializations
    are STRUCT_DECL cursors and read as structs, while the primary template and every
    partial specialization read as classes. Asking clang costs one call and settles it: 95
    struct and the 2 specializations fmt really does write `class`, and 21 fmt qualified
    names that were both a class and a struct come down to that one.
    """

    def __init__(self) -> None:
        self._get = ci.conf.lib.clang_getTemplateCursorKind
        self._get.argtypes = [ci.Cursor]
        self._get.restype = ctypes.c_uint

    def __call__(self, cursor: ci.Cursor) -> ci.CursorKind | None:
        try:
            return ci.CursorKind.from_id(self._get(cursor))
        # `CXCursor_NoDeclFound` for a cursor that is not a template, and any id this
        # binding release does not know. Neither is a reason to refuse the node.
        except ValueError:
            return None


_template_tag: _TemplateTag | None = None


def template_tag_kind(cursor: ci.Cursor) -> ci.CursorKind | None:
    global _template_tag
    if _template_tag is None:
        _template_tag = _TemplateTag()
    return _template_tag(cursor)


class _InlineNamespace:
    """`clang_Cursor_isInlineNamespace`, which the Python bindings do not wrap.

    Whether a namespace is inline is not in its USR or its spelling, and it decides how the
    matcher may treat a bump: `inline namespace v1` becoming `v2` keeps every unqualified
    name working, so its members are the same entities under a new version tag, while a
    plain `namespace v1` becoming `v2` breaks every caller that names it.
    """

    def __init__(self) -> None:
        self._get = ci.conf.lib.clang_Cursor_isInlineNamespace
        self._get.argtypes = [ci.Cursor]
        self._get.restype = ctypes.c_uint

    def __call__(self, cursor: ci.Cursor) -> bool:
        return bool(self._get(cursor))


_inline_namespace: _InlineNamespace | None = None


def is_inline_namespace(cursor: ci.Cursor) -> bool:
    global _inline_namespace
    if _inline_namespace is None:
        _inline_namespace = _InlineNamespace()
    return _inline_namespace(cursor)


#: `CXPrintingPolicyProperty` values, from clang-c/Index.h. Named here because the C API takes
#: them as bare integers, and a wrong one silently prints something else.
_POLICY_SUPPRESS_INITIALIZERS = 6
_POLICY_TERSE_OUTPUT = 17


class _PrettyPrinter:
    """`clang_getCursorPrettyPrinted`, which the Python bindings do not wrap.

    Clang's own declaration printer, for the facts the bindings expose no predicate for:
    `constexpr`, and the default arguments and default member initializers. It prints what
    clang understood rather than what was typed, so a macro arrives expanded and spacing and
    comments are gone -- the same insensitivity `body_hash` gets from hashing tokens.

    Terse output always: a function prints as its declaration, without its body.
    """

    def __init__(self) -> None:
        lib = ci.conf.lib
        self._policy = lib.clang_getCursorPrintingPolicy
        self._policy.argtypes = [ci.Cursor]
        self._policy.restype = ctypes.c_void_p
        self._set = lib.clang_PrintingPolicy_setProperty
        self._set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint]
        self._set.restype = None
        self._dispose = lib.clang_PrintingPolicy_dispose
        self._dispose.argtypes = [ctypes.c_void_p]
        self._dispose.restype = None
        self._print = lib.clang_getCursorPrettyPrinted
        self._print.argtypes = [ci.Cursor, ctypes.c_void_p]
        # The bindings' own CXString type and conversion, which also dispose of the string.
        # A second CXString declaration would clash with the argtypes cindex registered for
        # `clang_getCString` and `clang_disposeString`. `_CXString` is private to cindex:
        # re-check it whenever the bindings are upgraded.
        self._print.restype = ci._CXString
        self._print.errcheck = ci._CXString.from_result

    def __call__(self, cursor: ci.Cursor, *, suppress_initializers: bool = False) -> str:
        policy = self._policy(cursor)
        try:
            self._set(policy, _POLICY_TERSE_OUTPUT, 1)
            self._set(policy, _POLICY_SUPPRESS_INITIALIZERS, int(suppress_initializers))
            return str(self._print(cursor, policy))
        finally:
            self._dispose(policy)


_pretty_printer: _PrettyPrinter | None = None


def pretty_printed(cursor: ci.Cursor, *, suppress_initializers: bool = False) -> str:
    global _pretty_printer
    if _pretty_printer is None:
        _pretty_printer = _PrettyPrinter()
    return _pretty_printer(cursor, suppress_initializers=suppress_initializers)


class _VirtualBase:
    """`clang_isVirtualBase`, which the Python bindings declare but give no method.

    Whether a base specifier is `virtual`: an attribute of the `inherits` edge, beside its
    access, because it belongs to the relation and not to either class.
    """

    def __init__(self) -> None:
        self._get = ci.conf.lib.clang_isVirtualBase
        self._get.argtypes = [ci.Cursor]
        self._get.restype = ctypes.c_uint

    def __call__(self, cursor: ci.Cursor) -> bool:
        return bool(self._get(cursor))


_virtual_base: _VirtualBase | None = None


def is_virtual_base(cursor: ci.Cursor) -> bool:
    global _virtual_base
    if _virtual_base is None:
        _virtual_base = _VirtualBase()
    return _virtual_base(cursor)


def _is_member(cursor: ci.Cursor) -> bool:
    """Whether this declaration belongs to a class-like scope (ARCH-001 §4.1).

    The *semantic* parent, never the lexical one. A friend function template is written
    inside a class body and is not a member of it -- clang reports its lexical parent as the
    class and its semantic parent as the enclosing namespace, which is the distinction the
    language makes and the one a node kind should record. The reverse case needs the same
    answer from the other direction: an out-of-line `template <class T> void C::f(T)` is
    written at namespace scope and its semantic parent is still `C`.
    """
    parent = cursor.semantic_parent
    return parent is not None and parent.kind in RECORD_KINDS


def _node_kind(cursor: ci.Cursor) -> NodeKind | None:
    """The modelled kind of a cursor, or None if v0 does not model it.

    Kind is not identity -- the USR is -- so getting this wrong corrupts no diff. It does
    corrupt rendering and every kind-keyed rule downstream, and "21 qualified names are both
    a class and a struct" is the kind of inconsistency that gets read as a matching bug.

    Two refinements, both because a cursor kind is not the question being asked.

    **A templated record** is CLASS_TEMPLATE whichever tag was written, so the tag is asked
    for separately (`template_tag_kind`).

    **A member function template** is FUNCTION_TEMPLATE whether its semantic parent is a
    namespace or a class, so the cursor kind alone cannot tell `fmt::format` from
    `basic_memory_buffer::append` -- 414 of fmt's nodes were classified `function` while
    every non-template member beside them was a `method`. Membership is a property of the
    scope, so it is read off the scope. Getting it right is also what gives these nodes the
    method attributes: `_attributes` keys on the node kind, so `explicit` on a constructor
    template had no representation anywhere in the model until the kind was right.
    """
    kind = MODELLED_KINDS.get(cursor.kind)
    if kind is None:
        return None
    if kind is NodeKind.ALIAS and _names_its_own_tag(cursor):
        return None
    # A function body is never descended into for declarations, so a local variable should
    # not arrive here. Refused anyway: a local, `static` or not, is part of its function's
    # body, which `body_hash` already compares.
    if kind is NodeKind.VARIABLE and cursor.semantic_parent.kind in CALLABLE_KINDS:
        return None
    if cursor.kind in TEMPLATED_RECORD_KINDS:
        return TAG_KINDS.get(template_tag_kind(cursor), kind)
    if cursor.kind is ci.CursorKind.FUNCTION_TEMPLATE and _is_member(cursor):
        return NodeKind.METHOD
    return kind


#: The declarations a C-style `typedef struct X {...} X;` can name.
_TAG_DECL_KINDS = frozenset(
    {
        ci.CursorKind.STRUCT_DECL,
        ci.CursorKind.CLASS_DECL,
        ci.CursorKind.UNION_DECL,
        ci.CursorKind.ENUM_DECL,
    }
)


def _names_its_own_tag(cursor: ci.Cursor) -> bool:
    """Whether a typedef only gives a tag its own name again, as C does.

    `typedef struct X {...} X;` and `typedef struct {...} X;` declare one type under one name,
    and the struct is already a node called `X`. An alias node beside it would hold nothing
    -- its target is the struct itself, and it cannot change unless the struct is renamed --
    and two nodes with one qualified name would read as one entity seen twice.
    """
    if cursor.kind is not ci.CursorKind.TYPEDEF_DECL:
        return False
    tag = cursor.underlying_typedef_type.get_declaration()
    return (
        tag.kind in _TAG_DECL_KINDS
        and tag.spelling == cursor.spelling
        and tag.semantic_parent == cursor.semantic_parent
    )


#: Clang spells an unnamed entity with the source location that distinguishes it:
#: `(anonymous union at /abs/path/include/fmt/base.h:2148:3)`. Worktrees are named after the
#: commit SHA (`acquire/workspace.py`), so copying that spelling into the model writes both
#: the host's directory layout and the revision under analysis into `name` and
#: `qualified_name` -- and into the qualified name of every member nested inside. On fmt
#: that was 32 nodes by name, on spdlog 22, whose only difference between two revisions was
#: the path they were parsed from: an unconditional false-positive floor under every
#: comparison, a host-specific path in what ARCH-001 §10 calls the product, and a per-TU
#: cache entry (§9) that no second machine or second revision could ever reuse.
#:
#: The location is dropped rather than replaced with something stabler. Naming an unnamed
#: entity is not this field's job: the USR is intact and unaffected (`...@Ua`), so tier 1
#: still identifies it, and ARCH-001 §5 reserves tier 3 -- (parent, kind, ordinal) -- for
#: exactly the entities that have no name. A line and column would also move on any edit
#: above the record, which is the same false positive one machine further in.
_ANONYMOUS_AT = re.compile(r"\((anonymous|unnamed|lambda)(?: (\w+))? at .*?:\d+:\d+\)")


def _stable_spelling(spelling: str) -> str:
    """A clang spelling with any embedded source location removed."""
    # Guard the regex rather than the substitution: this runs once per cursor and again per
    # ancestor while building a qualified name, and essentially nothing matches.
    if " at " not in spelling:
        return spelling
    return _ANONYMOUS_AT.sub(lambda m: f"({m[1]} {m[2]})" if m[2] else f"({m[1]})", spelling)


def _canonical_spelling(type_: ci.Type) -> str:
    """A canonical type spelling, free of the host paths an unnamed type would carry.

    A field of unnamed struct type spells its type `struct (unnamed struct at /abs/path...)`,
    so the leak reaches `attrs["type"]`, `attrs["return_type"]` and `signature` as well as
    the names.
    """
    return _stable_spelling(type_.get_canonical().spelling)


def parse_arguments(entry: CompileEntry, toolchain: Toolchain) -> list[str]:
    """Turn a compile-database entry into libclang arguments.

    `-working-directory` rather than `os.chdir`: relative include paths in a compile
    database are relative to its `directory` field, and the process working directory is
    global state that a parallel producer could not share.
    """
    args: list[str] = []
    skip_next = False
    for arg in entry.arguments[1:]:  # [0] is the compiler driver
        if skip_next:
            skip_next = False
            continue
        if arg in DROPPED_FLAG_PAIRS:
            skip_next = True
            continue
        if arg in DROPPED_FLAGS:
            continue
        if Path(arg).name == entry.file.name:
            continue  # the source file is passed to `index.parse`, not as a flag
        args.append(arg)
    args.append(f"-working-directory={entry.directory}")
    return toolchain.parse_args(args)


def language_mode(entry: CompileEntry) -> str | None:
    """The `-std=` this TU is compiled at, or None if the build does not say.

    A determinant of the AST that the include closure's content hashing cannot see, and one a
    reviewer cannot infer: raising `CMAKE_CXX_STANDARD` is one line of CMake and rewrites part
    of the model, because a newer standard exposes declarations an older one hides and changes
    what belongs to a function's type. Measured 2026-08-04 on leveldb `HEAD~9..~1`, which bumps
    11 to 17: 117 events appeared in vendored googletest alone, and in the residue they are
    indistinguishable from new code (v0 tracker §5, reports/2026-08-01--v0-build/STATUS.md).

    So the mode is reported rather than left to be inferred. It is *not* folded into
    `Completeness.is_complete` -- a standards bump is not a hole in coverage, it is a
    different question asked of the same source, and §11 wants such a limit stated as a fact
    rather than raised as an alarm.

    Read here rather than in `build.py` because this module already owns what a compile
    argument means (`parse_arguments`), and a second place that knows the spelling of `-std`
    is a second place that can get it wrong.
    """
    for arg in reversed(entry.arguments):  # last wins, as it does for the compiler
        if arg.startswith("-std="):
            return arg.removeprefix("-std=")
    return None


#: One entry per distinct (spelling, root, build directory). A translation unit asks about the
#: same few hundred files thousands of times. Bounded rather than unbounded because a
#: long-lived producer would otherwise accumulate an entry per file per revision it ever saw,
#: and nothing here needs a hit rate better than one TU's worth of working set.
_RELATIVE_CACHE = 8192


@lru_cache(maxsize=_RELATIVE_CACHE)
def _relative(path: str, root: Path, base: Path) -> str | None:
    """Repository-relative path, or None if the file is outside the tree under analysis.

    `base` is the compile database's `directory`, and it is not optional. libclang echoes
    back whatever path resolved the include, so a header reached through a relative `-I`
    comes back relative -- and resolving that against the *process* working directory
    silently places it outside the repository, dropping every declaration in it from the
    model without a single diagnostic.

    Memoized because `resolve()` is a realpath walk of a long absolute path, once per *visited
    cursor*, a large share of extraction wall time against a 30 s budget for 20 TUs (ARCH-001
    §9). The key is all three arguments, not the path: the same spelling means a different file
    under a different `base`, and base and head worktrees differ in nothing *but* `root`, so a
    memo keyed on the path alone would answer one revision's question with the other revision's
    answer.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def third_party_owner(relative: str, third_party: tuple[str, ...]) -> str | None:
    """The third-party directory a repository-relative path lies in, or None (ADR-025).

    `third_party` is `ThirdPartySet.paths`. Where entries nest, the innermost wins, because it
    is the more specific name. Decided here rather than by asking `ThirdPartySet`, because
    what this answers decides what extraction keeps, and only this module and `ir.py` are
    hashed into the cache key (`cache._producer_code_digest`).
    """
    best: str | None = None
    for directory in third_party:
        if (relative == directory or relative.startswith(directory + "/")) and (
            best is None or len(directory) > len(best)
        ):
            best = directory
    return best


@dataclass(frozen=True)
class Boundary:
    """What the model leaves out inside the repository (ADR-025, ADR-037).

    `third_party` is `ThirdPartySet.paths`. `tracked` is every path git tracks at the revision
    (`acquire.tracked_files`); a file under the repository root that is not in it is
    `untracked` and not modelled. None means no tracked list was given, and every file under
    the root counts as tracked -- what a synthetic checkout with no git history needs.
    """

    third_party: tuple[str, ...] = ()
    tracked: frozenset[str] | None = None


#: No third-party paths and no tracked list: everything under the repository root is modelled.
NO_BOUNDARY = Boundary()


#: C standard and POSIX headers, by name. A header with one of these names directly in a
#: search directory, or in its architecture subdirectory, is labelled `C library`.
C_HEADERS = frozenset(
    [
        "assert.h",
        "complex.h",
        "ctype.h",
        "errno.h",
        "fenv.h",
        "float.h",
        "inttypes.h",
        "iso646.h",
        "limits.h",
        "locale.h",
        "math.h",
        "setjmp.h",
        "signal.h",
        "stdalign.h",
        "stdarg.h",
        "stdatomic.h",
        "stdbool.h",
        "stddef.h",
        "stdint.h",
        "stdio.h",
        "stdlib.h",
        "stdnoreturn.h",
        "string.h",
        "tgmath.h",
        "threads.h",
        "time.h",
        "uchar.h",
        "wchar.h",
        "wctype.h",
        "aio.h",
        "cpio.h",
        "dirent.h",
        "dlfcn.h",
        "fcntl.h",
        "fmtmsg.h",
        "fnmatch.h",
        "ftw.h",
        "glob.h",
        "grp.h",
        "iconv.h",
        "langinfo.h",
        "libgen.h",
        "monetary.h",
        "mqueue.h",
        "ndbm.h",
        "netdb.h",
        "nl_types.h",
        "poll.h",
        "pthread.h",
        "pwd.h",
        "regex.h",
        "sched.h",
        "search.h",
        "semaphore.h",
        "spawn.h",
        "strings.h",
        "stropts.h",
        "syslog.h",
        "tar.h",
        "termios.h",
        "trace.h",
        "ulimit.h",
        "unistd.h",
        "utime.h",
        "utmpx.h",
        "wordexp.h",
        "alloca.h",
        "byteswap.h",
        "endian.h",
        "execinfo.h",
        "features.h",
        "link.h",
        "malloc.h",
    ]
)

#: Subdirectories of a system search directory that hold the C library's own headers
#: (`sys/stat.h`, `bits/types.h`), which would otherwise be labelled `sys` or `bits`.
C_SUBDIRECTORIES = frozenset(
    {"sys", "bits", "gnu", "asm", "asm-generic", "linux", "netinet", "arpa", "net"}
)

#: The toolchain's default search directories, beyond its builtin headers. Not discovered:
#: libclang has no call that reports its header search list, and a wrong label costs nothing
#: beyond the picture it appears in, because no analysis reads it.
SYSTEM_INCLUDE_DIRS = (Path("/usr/local/include"), Path("/usr/include"))

CXX_STANDARD_LIBRARY = "C++ standard library"
C_LIBRARY = "C library"

#: The flags that add a header search directory, in their joined (`-Idir`) or separate form.
_SEARCH_FLAGS = ("-isystem", "-iquote", "-idirafter", "-I")

#: A `c++/<version>` or `c++/v1` directory: libstdc++ or libc++.
_CXX_VERSION = re.compile(r"(v1|\d+(\.\d+)*)")


def multiarch(target: str | None) -> str | None:
    """The Debian multiarch directory name for a target triple: `x86_64-pc-linux-gnu` →
    `x86_64-linux-gnu`. Only the vendor field is dropped."""
    if not target:
        return None
    parts = target.split("-")
    if len(parts) >= 4 or (len(parts) == 3 and parts[1] in ("pc", "unknown")):
        return "-".join([parts[0], *parts[2:]])
    return target


def search_dirs(entry: CompileEntry, toolchain: Toolchain) -> tuple[Path, ...]:
    """The directories a TU's headers are found through: its own flags, then the toolchain's.

    Includes the builtin header directory and the system directories. Only used to name a
    library outside the repository, so anything this misses costs a label, not a model.
    """
    dirs: list[Path] = []
    args = entry.arguments[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        value: str | None = None
        for flag in _SEARCH_FLAGS:
            if arg == flag and i + 1 < len(args):
                value = args[i + 1]
                i += 1
                break
            if arg.startswith(flag) and len(arg) > len(flag):
                value = arg[len(flag) :]
                break
        if value is not None:
            path = Path(value)
            dirs.append((path if path.is_absolute() else entry.directory / path).resolve())
        i += 1
    if toolchain.builtin_dir is not None:
        dirs.append(toolchain.builtin_dir)
    dirs.extend(SYSTEM_INCLUDE_DIRS)
    return tuple(dict.fromkeys(dirs))


@lru_cache(maxsize=_RELATIVE_CACHE)
def library_label(
    path: str,
    base: Path,
    dirs: tuple[Path, ...],
    builtin_dir: Path | None,
    arch: str | None,
) -> str:
    """The name of the library a header outside the repository belongs to.

    The header's path relative to the search directory that holds it: under the builtin
    header directory, `compiler builtins`; under `c++/<version>` or `c++/v1`, the C++ standard
    library; a C or POSIX header, the C library; inside a subdirectory, that subdirectory's
    name (`boost`); directly in a search directory, the header's name without its suffix
    (`zlib`). A header in no known search directory is named as if its own directory were one.

    For display only. The label is stored in cached partials, and the system directories are
    not in the cache key, so a host whose search list changes under the same toolchain keeps
    the old labels until the TU is reparsed. No analysis reads them.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if builtin_dir is not None and resolved.is_relative_to(builtin_dir):
        return COMPILER_BUILTINS
    parts = resolved.parts
    if any(
        parts[i] == "c++" and _CXX_VERSION.fullmatch(parts[i + 1]) for i in range(len(parts) - 1)
    ):
        return CXX_STANDARD_LIBRARY
    owners = [d for d in dirs if resolved.is_relative_to(d)]
    rest: tuple[str, ...] = (
        resolved.relative_to(max(owners, key=lambda d: len(d.parts))).parts
        if owners
        else (resolved.name,)
    )
    if arch and len(rest) > 1 and rest[0] == arch:
        rest = rest[1:]
    if len(rest) > 1:
        return C_LIBRARY if rest[0] in C_SUBDIRECTORIES else rest[0]
    name = rest[0] if rest else resolved.name
    return C_LIBRARY if name in C_HEADERS else Path(name).stem


#: Members clang generates at their class's own location when nobody declares them.
_SPECIAL_MEMBER_KINDS = frozenset(
    {ci.CursorKind.CONSTRUCTOR, ci.CursorKind.DESTRUCTOR, ci.CursorKind.CXX_METHOD}
)


def fallback_label(cursor: ci.Cursor, usr: str) -> str:
    """What an in-repository target is, should no declaration in the model produce it.

    Read from the cursor while it is in hand and used by the builder only if the edge stays
    unresolved. Measured on the fixtures (step 1 report §3), in the order
    tested here:

    - `compiler builtins`: `__builtin_*` functions, declared where first used;
    - `function-local`: members of a lambda or of a class declared inside a function, which
      v0 does not model;
    - `implicit`: special members clang generated at their class's own location;
    - `template instantiation`: an instantiation, or a member of one, whose template is no node;
    - `unresolved`: anything else, e.g. inherited constructors and friend operators defined
      in a class.
    """
    if cursor.spelling.startswith("__builtin"):
        return COMPILER_BUILTINS
    parent = cursor.semantic_parent
    if parent is not None and parent.spelling.startswith("(lambda"):
        return FUNCTION_LOCAL
    if (
        cursor.kind in _SPECIAL_MEMBER_KINDS
        and parent is not None
        and parent.location.file is not None
        and cursor.location.file is not None
        and parent.location.file.name == cursor.location.file.name
        and (parent.location.line, parent.location.column)
        == (cursor.location.line, cursor.location.column)
    ):
        return IMPLICIT
    walk = parent
    while walk is not None and walk.kind is not ci.CursorKind.TRANSLATION_UNIT:
        if walk.kind in CALLABLE_KINDS:
            return FUNCTION_LOCAL
        walk = walk.semantic_parent
    template = specialized_template_usr(cursor)
    if template and template != usr:
        return TEMPLATE_INSTANTIATION
    if parent is not None:
        parent_template = specialized_template_usr(parent)
        if parent_template and parent_template != parent.get_usr():
            return TEMPLATE_INSTANTIATION
    return UNRESOLVED


def _location(cursor: ci.Cursor, root: Path, base: Path) -> Location | None:
    if cursor.location.file is None:
        return None
    relative = _relative(cursor.location.file.name, root, base)
    if relative is None:
        return None
    extent = cursor.extent
    start = extent.start
    # Only an extent that starts in the same file as the name says where the declaration's
    # text starts. One that begins in another file -- a declaration whose first tokens come
    # from a macro defined elsewhere -- carries that file's line number, and applied here it
    # would widen the span over unrelated lines and count their changes as explained.
    same_file = start.file is not None and start.file.name == cursor.location.file.name
    return Location(
        file=relative,
        line=cursor.location.line,
        end_line=extent.end.line,
        column=cursor.location.column,
        start_line=start.line if same_file else None,
    )


def body_hash(cursor: ci.Cursor) -> str | None:
    """Hash of a definition's body, insensitive to formatting and comments.

    Tier-2 identity (ARCH-001 §5) has to survive a rename, so the name and signature are
    deliberately *not* hashed -- only the compound statement, and a constructor's member
    initializer list before it. Hashing token spellings rather than source text is what
    makes reindentation and comment edits invisible.

    The initializer list is code that runs when the constructor does, so a changed
    initializer is a body change (#11). Known gap: a function-try-block, `S() try : x(1)
    {} catch (...) {}`, has no compound statement of its own, and has no body hash at all.
    """
    body = next(
        (c for c in cursor.get_children() if c.kind is ci.CursorKind.COMPOUND_STMT),
        None,
    )
    if body is None:
        return None
    tokens: Iterable[ci.Token] = body.get_tokens()
    # A constructor template is a FUNCTION_TEMPLATE cursor; the kind it templates says.
    if ci.CursorKind.CONSTRUCTOR in (cursor.kind, template_tag_kind(cursor)):
        tokens = itertools.chain(_member_initializers(cursor, body), tokens)
    digest = hashlib.blake2b(digest_size=16)
    empty = True
    for token in tokens:
        if token.kind is ci.TokenKind.COMMENT:
            continue
        digest.update(token.spelling.encode())
        digest.update(b"\x00")
        empty = False
    return None if empty else digest.hexdigest()


def _member_initializers(cursor: ci.Cursor, body: ci.Cursor) -> list[ci.Token]:
    """A constructor's member initializer list, from its `:` to the body; empty if none.

    The tokens between the constructor's name and its body, from the first `:` outside any
    brackets. Anchored at the name, so a `:` in the template header before it is not read;
    outside brackets, so a `?:` in a default argument is not read either. `::` is a token
    of its own and never matches.
    """
    name = cursor.location.offset
    end = body.extent.start.offset
    after_name = False
    depth = 0
    initializers: list[ci.Token] = []
    # Only the tokens from the name to the body: the body is tokenized once, by the caller.
    span = ci.SourceRange.from_locations(cursor.location, body.extent.start)
    for token in ci.TokenGroup.get_tokens(cursor.translation_unit, span):
        offset = token.extent.start.offset
        if offset >= end:
            break
        if not after_name:
            after_name = offset == name
            continue
        spelling = token.spelling
        if initializers:
            initializers.append(token)
        elif spelling in ("(", "[", "{"):
            depth += 1
        elif spelling in (")", "]", "}"):
            depth = max(0, depth - 1)
        elif spelling == ":" and depth == 0:
            initializers.append(token)
    return initializers


def _access(cursor: ci.Cursor) -> str | None:
    """`public`/`protected`/`private`, or None where access is not a meaningful question."""
    access = cursor.access_specifier
    if access is None or access in (ci.AccessSpecifier.INVALID, ci.AccessSpecifier.NONE):
        return None
    return str(access.name).lower()


def _exception_spec(cursor: ci.Cursor) -> str | None:
    """The exception specification kind, or None where this TU never determined one.

    The spec kind, not a `noexcept: bool`. Collapsing it loses the distinction between
    `noexcept` and `noexcept(false)` -- both are COMPUTED/BASIC specs, and one of them still
    throws. `none -> basic_noexcept` diffs just as mechanically and does not claim something
    untrue.

    UNEVALUATED is folded into None because it is not a state of the entity: clang computes
    an implicit exception specification lazily, so it means only that this translation unit
    never odr-used the function. Publishing it as a value would make `unevaluated ->
    basic_noexcept` a reportable attribute change caused by adding a test file, and
    ARCH-001 §4 admits only identity, diffable state and provenance -- an artefact of when
    the compiler happened to look is none of the three. None already means "not determined",
    and `merge_node` prefers the observation that determined more.
    """
    spec = cursor.exception_specification_kind
    if spec is None or spec is ci.ExceptionSpecificationKind.UNEVALUATED:
        return None
    return str(spec.name).lower()


def _initializer(cursor: ci.Cursor) -> str | None:
    """What follows a parameter's or a field's name: its default, `""` for none, or None
    where it could not be read.

    Printed twice, with and without initializers, and the difference is the initializer.
    Read this way rather than from the cursor's tokens because clang prints the default a
    redeclaration inherits: `void f(int x) {}` after a header's `void f(int x = 1);` prints
    `int x = 1`, so the definition and the declaration agree. Its tokens would hold nothing.

    The suppressed spelling must be a prefix of the full one. If it is not, the printer did
    something this does not understand, and the answer is None rather than a guess.
    """
    full = pretty_printed(cursor)
    bare = pretty_printed(cursor, suppress_initializers=True)
    if not full.startswith(bare):
        return None
    rest = full[len(bare) :]
    if not rest:
        return ""
    if rest.startswith(" = "):
        return rest[len(" = ") :]
    if rest.lstrip().startswith("{"):  # a field's brace initializer, `int x{1}`
        return rest.strip()
    return None


def _default_arguments(cursor: ci.Cursor) -> str | None:
    """The default arguments of a function's parameters, in order, joined `"; "`.

    `""` means no parameter has one, and None that one of them could not be read. Only the
    parameters that have a default are listed. Defaults are trailing, so with the signature
    the list says which parameter each belongs to.

    An attribute, not part of the signature. A default argument is not part of the function's
    type, and changing one changes what callers that omit it pass, not which function they
    call (ADR-043).
    """
    defaults: list[str] = []
    for parameter in cursor.get_children():
        if parameter.kind is not ci.CursorKind.PARM_DECL:
            continue
        value = _initializer(parameter)
        if value is None:
            return None
        if value:
            defaults.append(value)
    return "; ".join(defaults)


#: A string literal, so that text inside one is never read as a keyword.
_STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')

#: A word, or one bracket of any of the four kinds.
_WORD_OR_BRACKET = re.compile(r"\w+|[()\[\]{}<>]")


def _constexpr(cursor: ci.Cursor) -> bool | None:
    """Whether a function is declared `constexpr`, or None where this could not be read.

    The bindings have no predicate for it, so it is read off clang's printed declaration:
    `constexpr` among the specifiers before the function's name, outside any brackets.
    Before the name only, so a `constexpr` inside a parameter's default argument or a
    template argument is not taken for the function's.

    Known gap: clang does not print `constexpr` on a defaulted special member, so `constexpr
    S() = default;` reads False.
    """
    text = pretty_printed(cursor)
    name = re.search(rf"(?<![\w~]){re.escape(cursor.spelling)}\s*(?:<[^()]*>)?\s*\(", text)
    if name is None:
        return None
    depth = 0
    for token in _WORD_OR_BRACKET.findall(_STRING_LITERAL.sub('""', text[: name.start()])):
        if token in "([{<":
            depth += 1
        elif token in ")]}>":
            depth = max(0, depth - 1)
        elif depth == 0 and token == "constexpr":
            return True
    return False


def _final(cursor: ci.Cursor) -> bool | None:
    """Whether a record is `final`, or None where this TU does not see its definition.

    Read from the definition, which is the declaration that carries `final`: a forward
    declaration cannot say, and reading False off one would contradict the TU that saw the
    class.
    """
    definition = cursor.get_definition()
    if definition is None:
        return None
    return any(c.kind is ci.CursorKind.CXX_FINAL_ATTR for c in definition.get_children())


def _record_deprecated(cursor: ci.Cursor) -> bool | None:
    """Whether a record is deprecated, or None where this TU does not see its definition.

    Read from the definition, as `final` is: a forward declaration seen before a
    `[[deprecated]]` definition is not deprecated in clang's eyes, so reading it there would
    make the answer depend on which declaration a TU happened to see.
    """
    definition = cursor.get_definition()
    if definition is None:
        return None
    return bool(definition.availability is ci.AvailabilityKind.DEPRECATED)


def _base_order(cursor: ci.Cursor) -> str | None:
    """A record's bases in the order written, joined `"; "`, or None where this TU does not
    see its definition. `""` means no bases.

    Base order is layout and construction order, and the `inherits` edges have no order of
    their own. The diff reports it only where the bases on both sides are in a different
    relative order, so adding or removing a base is not also a reorder (ADR-043).
    """
    definition = cursor.get_definition()
    if definition is None:
        return None
    return "; ".join(
        _canonical_spelling(c.type)
        for c in definition.get_children()
        if c.kind is ci.CursorKind.CXX_BASE_SPECIFIER
    )


def _attributes(cursor: ci.Cursor, kind: NodeKind) -> dict[str, AttrValue]:
    """Flat, always-present-per-kind scalars (ARCH-001 §4.3).

    Keys are set unconditionally for the kinds they apply to, never omitted when false. An
    attribute diff is meant to be mechanical -- `virtual: false -> true` is an event, while
    `absent -> false` is a question about the extractor.
    """
    attrs: dict[str, AttrValue] = {}
    if access := _access(cursor):
        attrs["access"] = access

    if kind in (NodeKind.FUNCTION, NodeKind.METHOD):
        attrs["return_type"] = _canonical_spelling(cursor.result_type)
        attrs["sugared_signature"] = _sugared_signature(cursor)
        attrs["template"] = cursor.kind is ci.CursorKind.FUNCTION_TEMPLATE
        attrs["template_parameters"] = _template_parameters(cursor)
        attrs["deleted"] = cursor.is_deleted_method()
        attrs["defaulted"] = cursor.is_default_method()
        attrs["exception_spec"] = _exception_spec(cursor)
        attrs["default_arguments"] = _default_arguments(cursor)
        attrs["constexpr"] = _constexpr(cursor)
        # A redeclaration inherits the attribute, so an out-of-line definition carries the
        # header's `[[nodiscard]]` as a child of its own.
        attrs["nodiscard"] = any(
            c.kind is ci.CursorKind.WARN_UNUSED_RESULT_ATTR for c in cursor.get_children()
        )
        # Clang's availability, which follows inherited attributes as `nodiscard` does. It is
        # the entity's own: a member of a deprecated class is not itself deprecated, and a
        # deleted function is unavailable, not deprecated.
        attrs["deprecated"] = cursor.availability is ci.AvailabilityKind.DEPRECATED
    # Keyed on the node kind rather than on the cursor kind, which is what lets a member
    # function template carry exactly what an ordinary method carries. Clang's
    # `clang_CXXMethod_*` predicates unwrap a FunctionTemplateDecl to the function it
    # templates, so a member template answers them for real: an `explicit` constructor template
    # reads explicit, a `static` one reads static, a `const` one reads const. `explicit` in
    # particular had no other representation in the model -- there is no attribute a `function`
    # carries that could hold it.
    if kind is NodeKind.METHOD:
        attrs["static"] = cursor.is_static_method()
        attrs["virtual"] = cursor.is_virtual_method()
        attrs["pure_virtual"] = cursor.is_pure_virtual_method()
        attrs["const"] = cursor.is_const_method()
        attrs["explicit"] = cursor.is_explicit_method()
    if kind is NodeKind.FIELD:
        attrs["type"] = _canonical_spelling(cursor.type)
        attrs["mutable"] = cursor.is_mutable_field()
        attrs["default_member_init"] = _initializer(cursor)
        if cursor.is_bitfield():
            attrs["bitfield_width"] = cursor.get_bitfield_width()
    if kind is NodeKind.NAMESPACE:
        attrs["inline"] = is_inline_namespace(cursor)
    if kind in (NodeKind.CLASS, NodeKind.STRUCT, NodeKind.UNION):
        attrs["template"] = cursor.kind in (
            ci.CursorKind.CLASS_TEMPLATE,
            ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
        )
        attrs["abstract"] = cursor.is_abstract_record()
        attrs["final"] = _final(cursor)
        attrs["deprecated"] = _record_deprecated(cursor)
        attrs["base_order"] = _base_order(cursor)
    if kind is NodeKind.VARIABLE:
        attrs["type"] = _canonical_spelling(cursor.type)
        value, initializer = _initial_value(cursor)
        attrs["value"] = value
        attrs["initializer_hash"] = initializer
        attrs["constexpr"] = _is_constexpr(cursor)
        attrs["storage"] = str(cursor.storage_class.name).lower()
        attrs["linkage"] = str(cursor.linkage.name).lower()
        attrs["thread_local"] = cursor.tls_kind is not ci.TLSKind.NONE
    if kind is NodeKind.STATIC_ASSERTION:
        # The message as written, quotes included; None without one. The condition is not
        # an attribute: it is the assertion's identity (`static_assertion_id`).
        message = list(cursor.get_children())[1:2]
        attrs["message"] = (
            " ".join(t.spelling for t in message[0].get_tokens()) if message else None
        )
    if kind is NodeKind.MACRO:
        # Everything after the name: the parameter list, if any, and the replacement.
        replacement = [t.spelling for t in cursor.get_tokens()][1:]
        attrs["replacement_hash"] = _hash_spellings(replacement)
        attrs["function_like"] = macro_queries().function_like(cursor)
    if kind is NodeKind.ALIAS:
        attrs["target"] = _alias_target(cursor)
        attrs["template"] = cursor.kind is ci.CursorKind.TYPE_ALIAS_TEMPLATE_DECL
    if kind is NodeKind.ENUM:
        attrs["underlying_type"] = _canonical_spelling(cursor.enum_type)
        attrs["scoped"] = cursor.is_scoped_enum()
    if kind is NodeKind.ENUMERATOR:
        attrs["value"] = _enumerator_value(cursor)
        # Whether the value is written or follows from the one before. Kept beside the value
        # because the two cases mean different things when the value changes: a written value
        # changed is an edit to this line, an implied one is a consequence of an edit above it
        # (the `enumerator-implicit-value-shifted` cause rule, ARCH-001 §6.3).
        attrs["explicit_value"] = next(iter(cursor.get_children()), None) is not None
    return attrs


class _Evaluate:
    """`clang_Cursor_Evaluate`, which the Python bindings do not wrap.

    Clang's constant evaluator, asked of a variable's initializer: the value the compiler
    would fold it to, which is what a reviewer compares. `kLimit = 2 * 5` and `kLimit = 10` are
    the same constant, and a hash of the text could not say so.

    Here rather than in a module of its own because the producer's cache key hashes this
    module and `ir.py` and nothing else (`produce/cache.py`). A value computed by code outside
    them could change while every key stayed the same, and a stale entry would be served
    (ARCH-001 §9).
    """

    #: `CXEvalResultKind`. Every other kind -- Objective-C and CoreFoundation strings,
    #: `Other`, `UnExposed` -- is a value this model has no scalar for.
    INT, FLOAT, STR = 1, 2, 4

    def __init__(self) -> None:
        lib = ci.conf.lib
        self._evaluate = lib.clang_Cursor_Evaluate
        self._evaluate.argtypes = [ci.Cursor]
        self._evaluate.restype = ctypes.c_void_p
        self._kind = lib.clang_EvalResult_getKind
        self._kind.argtypes = [ctypes.c_void_p]
        self._kind.restype = ctypes.c_int
        self._is_unsigned = lib.clang_EvalResult_isUnsignedInt
        self._is_unsigned.argtypes = [ctypes.c_void_p]
        self._is_unsigned.restype = ctypes.c_uint
        self._unsigned = lib.clang_EvalResult_getAsUnsigned
        self._unsigned.argtypes = [ctypes.c_void_p]
        self._unsigned.restype = ctypes.c_ulonglong
        self._signed = lib.clang_EvalResult_getAsLongLong
        self._signed.argtypes = [ctypes.c_void_p]
        self._signed.restype = ctypes.c_longlong
        self._double = lib.clang_EvalResult_getAsDouble
        self._double.argtypes = [ctypes.c_void_p]
        self._double.restype = ctypes.c_double
        self._str = lib.clang_EvalResult_getAsStr
        self._str.argtypes = [ctypes.c_void_p]
        self._str.restype = ctypes.c_char_p
        self._dispose = lib.clang_EvalResult_dispose
        self._dispose.argtypes = [ctypes.c_void_p]
        self._dispose.restype = None

    def __call__(self, cursor: ci.Cursor) -> int | float | str | None:
        """The value, or None where clang cannot evaluate it to a number or a string."""
        result = self._evaluate(cursor)
        if not result:
            return None
        try:
            kind = self._kind(result)
            if kind == self.INT:
                return int(
                    self._unsigned(result) if self._is_unsigned(result) else self._signed(result)
                )
            if kind == self.FLOAT:
                number = float(self._double(result))
                # Spelled rather than stored: NaN is unequal to itself, so it would read as a
                # change on every comparison, and JSON has no infinity to round-trip through
                # the cache.
                return number if math.isfinite(number) else repr(number)
            if kind == self.STR:
                text = self._str(result)
                return None if text is None else text.decode("utf-8", errors="replace")
            return None
        finally:
            self._dispose(result)


_evaluator: _Evaluate | None = None


def evaluate(cursor: ci.Cursor) -> int | float | str | None:
    global _evaluator
    if _evaluator is None:
        _evaluator = _Evaluate()
    return _evaluator(cursor)


def _initial_value(cursor: ci.Cursor) -> tuple[int | float | str | None, str | None]:
    """A variable's initial value, else a hash of its initializer; both None without one.

    The value where clang can evaluate it, so two spellings of one constant are no change. The
    hash only where it cannot -- a call, a constructor, an address -- so that a changed
    initializer is still seen. Hashing the written tokens means a macro in it is hashed by
    name: a change to the macro's definition does not change the hash.

    Never evaluated inside a template. The initializer may depend on a template argument, and
    clang's evaluator is not safe to ask about a value-dependent expression.
    """
    initializer = next(
        (c for c in reversed(list(cursor.get_children())) if c.kind.is_expression()), None
    )
    if initializer is None:
        return None, None
    if not _in_template(cursor):
        value = evaluate(cursor)
        if value is not None:
            return value, None
    return None, _token_hash(initializer)


def _token_hash(cursor: ci.Cursor) -> str | None:
    """Hash of a cursor's tokens, comments skipped; None when it has none."""
    return _hash_spellings(
        t.spelling for t in cursor.get_tokens() if t.kind is not ci.TokenKind.COMMENT
    )


def _hash_spellings(spellings: Iterable[str]) -> str | None:
    digest = hashlib.blake2b(digest_size=16)
    empty = True
    for spelling in spellings:
        digest.update(spelling.encode())
        digest.update(b"\x00")
        empty = False
    return None if empty else digest.hexdigest()


#: Preprocessor cursors that record a use rather than declare anything.
_PREPROCESSOR_USES = frozenset(
    {ci.CursorKind.MACRO_INSTANTIATION, ci.CursorKind.INCLUSION_DIRECTIVE}
)


def _is_include_guard(cursor: ci.Cursor) -> bool:
    """Whether a macro is its header's include guard.

    Three conditions: it is object-like with nothing after its name; clang reports its file
    as guarded against a second inclusion; and the file tested the name with `#ifndef` or
    `defined` before defining it. The last is read from the file's tokens up to the macro,
    which is cheap because a guard comes first.
    """
    if len(list(cursor.get_tokens())) != 1 or macro_queries().function_like(cursor):
        return False
    if not macro_queries().guarded(cursor):
        return False
    tu = cursor.translation_unit
    start = ci.SourceLocation.from_offset(tu, cursor.location.file, 0)
    before = [
        t.spelling
        for t in tu.get_tokens(extent=ci.SourceRange.from_locations(start, cursor.extent.start))
    ]
    return any(
        spelling == cursor.spelling and before[i - 1] in ("ifndef", "defined", "(")
        for i, spelling in enumerate(before)
        if i > 0
    )


def _is_constexpr(cursor: ci.Cursor) -> bool:
    """Whether `constexpr` is written before the variable's name.

    Read from the tokens, which the bindings leave as the only way to ask. A `constexpr`
    spelled through a macro is missed.
    """
    name_at = cursor.location.offset
    for token in cursor.get_tokens():
        if token.extent.start.offset >= name_at:
            return False
        if token.spelling == "constexpr":
            return True
    return False


class _MacroQueries:
    """`clang_Cursor_isMacroFunctionLike` and `clang_isFileMultipleIncludeGuarded`, which the
    Python bindings do not wrap."""

    def __init__(self) -> None:
        lib = ci.conf.lib
        self._function_like = lib.clang_Cursor_isMacroFunctionLike
        self._function_like.argtypes = [ci.Cursor]
        self._function_like.restype = ctypes.c_uint
        self._guarded = lib.clang_isFileMultipleIncludeGuarded
        self._guarded.argtypes = [ci.TranslationUnit, ci.File]
        self._guarded.restype = ctypes.c_uint

    def function_like(self, cursor: ci.Cursor) -> bool:
        return bool(self._function_like(cursor))

    def guarded(self, cursor: ci.Cursor) -> bool:
        """Whether the file the macro is defined in is guarded against a second inclusion."""
        return bool(self._guarded(cursor.translation_unit, cursor.location.file))


_macro_queries: _MacroQueries | None = None


def macro_queries() -> _MacroQueries:
    global _macro_queries
    if _macro_queries is None:
        _macro_queries = _MacroQueries()
    return _macro_queries


#: The byte offset clang writes into a macro's USR: `c:a.cpp@8@macro@LIMIT`.
_MACRO_OFFSET = re.compile(r"^(c:[^@]*)@\d+@macro@")


def macro_id(usr: str, ordinal: int) -> str:
    """A macro's identity: its USR without the byte offset, and an ordinal from the second
    definition of one name in one file on.

    Clang writes the offset of the definition into a macro's USR, so a line added above it
    would make it a different macro (ADR-039). The file name stays, so a macro is identified
    by file and name, as a `static` is, and `usr_file` reads it as file-scoped. A name
    defined again in the same file, after an `#undef`, is a second macro: `#2`, `#3`, in the
    order the file defines them. A USR not in the expected form is kept whole, which costs
    only the stability this function exists for.
    """
    stripped = _MACRO_OFFSET.sub(r"\1@macro@", usr, count=1)
    return stripped if ordinal == 1 else f"{stripped}#{ordinal}"


#: The keywords a `static_assert` written in place starts with.
_STATIC_ASSERT_KEYWORDS = frozenset({"static_assert", "_Static_assert"})


def _written_by_a_macro(cursor: ci.Cursor) -> bool:
    """Whether a `static_assert` comes from a macro expansion rather than being written where
    it stands.

    libclang reports a declaration's location as the point of expansion -- spelling, file and
    expansion location all name the macro's use -- so the text there is read instead: written
    in place, the first token is `static_assert`; written by a macro, it is the macro's name.
    The range is rebuilt from file offsets, because the cursor's own extent is a macro range
    whose tokens libclang mixes up with the macro's definition.
    """
    start, end = cursor.extent.start, cursor.extent.end
    if start.file is None:
        return False
    tu = cursor.translation_unit
    span = ci.SourceRange.from_locations(
        ci.SourceLocation.from_offset(tu, start.file, start.offset),
        ci.SourceLocation.from_offset(tu, start.file, max(start.offset, end.offset)),
    )
    first = next(iter(tu.get_tokens(extent=span)), None)
    return first is not None and first.spelling not in _STATIC_ASSERT_KEYWORDS


def static_assertion_id(file: str, parent: str, condition: str | None, ordinal: int) -> str:
    """A static assertion's identity, which clang does not give it: its USR is a bare `c:`.

    `c:<file>@SA@<parent USR>@<condition hash>`, with `#n` from the second assertion of one
    condition in one scope on. Made of what the assertion *is* -- where it is declared and
    what it asserts -- so a line added above it changes nothing, while changing its condition
    makes it a different assertion: the old one removed, the new one added (ADR-039). The file
    is the file name, written first, as clang writes it into a file-scoped USR, so `usr_file`
    reads it and a renamed file is resolved from git before matching (ARCH-001 §5).
    """
    sid = f"c:{file}@SA@{parent}@{condition or ''}"
    return sid if ordinal == 1 else f"{sid}#{ordinal}"


def _alias_target(cursor: ci.Cursor) -> str:
    """The type an alias stands for, canonical: what every use of the alias compiles to.

    An alias template's target is on the `using` declaration it templates, its only
    non-parameter child, and spells its parameters by position (`type-parameter-0-0 *`), so
    renaming a parameter is no change.
    """
    if cursor.kind is ci.CursorKind.TYPE_ALIAS_TEMPLATE_DECL:
        cursor = next(
            (c for c in cursor.get_children() if c.kind is ci.CursorKind.TYPE_ALIAS_DECL), cursor
        )
    return _canonical_spelling(cursor.underlying_typedef_type)


#: Cursor kinds that make everything inside them a template, whose values may depend on a
#: template argument.
_TEMPLATE_KINDS = frozenset(
    {
        ci.CursorKind.CLASS_TEMPLATE,
        ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
        ci.CursorKind.FUNCTION_TEMPLATE,
        ci.CursorKind.TYPE_ALIAS_TEMPLATE_DECL,
    }
)


def _in_template(cursor: ci.Cursor) -> bool:
    """Whether any semantic ancestor is a template."""
    parent = cursor.semantic_parent
    while parent is not None and parent.kind is not ci.CursorKind.TRANSLATION_UNIT:
        if parent.kind in _TEMPLATE_KINDS:
            return True
        parent = parent.semantic_parent
    return False


def _enumerator_value(cursor: ci.Cursor) -> int | None:
    """An enumerator's value, or None inside a template.

    Clang does not compute the enumerators of an enum declared inside a template: every one
    reads 0, even `z = 4`, since the enum as a whole may depend on a template argument. A 0 that
    means "not computed" would read as a real value and change when the enum moves into or out
    of a template, so it is not recorded.
    """
    if _in_template(cursor):
        return None
    return int(cursor.enum_value)


#: The cursor kinds of a template's own parameter list, in the order they are declared.
_TEMPLATE_PARAMETER_KINDS = frozenset(
    {
        ci.CursorKind.TEMPLATE_TYPE_PARAMETER,
        ci.CursorKind.TEMPLATE_NON_TYPE_PARAMETER,
        ci.CursorKind.TEMPLATE_TEMPLATE_PARAMETER,
    }
)


def _template_parameters(cursor: ci.Cursor) -> str | None:
    """A function template's parameter list in a canonical spelling, or None for a function
    that is not a template.

    Compared state, not identity. Two function templates that differ only in their
    parameter lists can have the same signature, because the signature spells a template
    parameter by position (`type-parameter-0-0`), never by what it is. Where the matcher
    pairs two such templates by name rather than by USR, this attribute is the only place a
    reviewer can see that the parameter list changed.

    Names are left out, so renaming `T` to `U` is no change. Each parameter is `class` for a
    type, its canonical type for a non-type parameter, or a nested list for a template
    template parameter, and `...` marks a pack. Default arguments are left out.
    """
    if cursor.kind is not ci.CursorKind.FUNCTION_TEMPLATE:
        return None
    return _template_parameter_list(cursor)


def _template_parameter_list(owner: ci.Cursor) -> str:
    params = [c for c in owner.get_children() if c.kind in _TEMPLATE_PARAMETER_KINDS]
    # An out-of-line member template's definition also lists its class's parameters, first.
    # Only its own header's are its own, or the attribute would differ between the TU that
    # sees the definition and the TU that sees only the declaration.
    packs = _packs(owner)
    if packs is not None and len(packs) <= len(params):
        params = params[len(params) - len(packs) :]
    else:
        packs = [False] * len(params)
    return (
        "<"
        + ", ".join(_template_parameter(p, pack) for p, pack in zip(params, packs, strict=True))
        + ">"
    )


def _template_parameter(param: ci.Cursor, pack: bool) -> str:
    if param.kind is ci.CursorKind.TEMPLATE_TYPE_PARAMETER:
        text = "class"
    elif param.kind is ci.CursorKind.TEMPLATE_NON_TYPE_PARAMETER:
        text = _canonical_spelling(param.type)
    else:
        text = f"template {_template_parameter_list(param)} class"
    return text + ("..." if pack else "")


def _packs(owner: ci.Cursor) -> list[bool] | None:
    """For each parameter in `owner`'s own template header, whether it is a pack; None
    where the tokens hold no header.

    The bindings do not expose clang's pack flag, and an unnamed parameter's own extent
    leaves out its `...`, so this reads the list as written: the last `template <...>`
    header, split at its top-level commas, one part per parameter. The last, because an
    out-of-line member template is written after its class's header. A part is a pack where
    it holds `...` at its own level -- not inside a nested list, which is how
    `template <class...> class X` takes a pack without being one -- and before any default
    argument, which a pack cannot have.

    Angle brackets inside parentheses are not counted, so a `>` used as an operator in a
    default argument, such as `enable_if_t<(N > 0)>`, does not close the header. Known limit:
    an unparenthesized `>` in a default argument still does; C++ requires those parentheses
    in a template argument, so it should not arise in code that compiles.
    """
    header: list[bool] | None = None
    parts: list[bool] | None = None  # the header being read, if any
    angles = parens = 0
    previous = ""
    pack = in_default = False
    for token in owner.get_tokens():
        spelling = token.spelling
        if spelling in ("(", "["):
            parens += 1
        elif spelling in (")", "]"):
            parens = max(0, parens - 1)
        elif parens:
            pass  # inside parentheses: `<` and `>` are operators, and nothing is top level
        elif spelling == "<":
            if angles == 0 and previous == "template":
                parts, pack, in_default = [], False, False
            angles += 1
        elif spelling in (">", ">>"):
            angles = max(0, angles - (2 if spelling == ">>" else 1))
            if angles == 0 and parts is not None:
                header = [*parts, pack] if parts or pack or previous != "<" else []
                parts = None
        elif angles == 1 and parts is not None:
            if spelling == ",":
                parts.append(pack)
                pack = in_default = False
            elif spelling == "=":
                in_default = True
            elif spelling == "..." and not in_default:
                pack = True
        previous = spelling
    return header


#: The exception specification, which a canonical function type spells last: ` noexcept`,
#: ` noexcept(<expr>)` for a dependent one, or ` throw(...)`. Anchored at the end so that a
#: parameter of function-pointer type -- `void (void (*)() noexcept)` -- keeps its own.
_EXCEPTION_SPEC = re.compile(r"\s+(?:noexcept|throw)\s*(?:\(.*\))?$")


def _signature(cursor: ci.Cursor, kind: NodeKind) -> str | None:
    """Canonical type spelling: typedefs resolved, qualifiers explicit (ARCH-001 §4.1).

    Tier-1 identity cannot distinguish overloads without it. The canonical form is what
    makes `size_t` and `unsigned long` the same signature, so an unrelated typedef change
    does not read as a signature change.

    The exception specification is stripped, and this is the load-bearing part. Clang
    computes an *implicit* specification lazily, so a special member that a TU never
    odr-uses comes back UNEVALUATED and its canonical type has no `noexcept` where the same
    entity, seen from a TU that did use it, spells one. Measured on fmt,
    `fmt::v12::ostream::~ostream` is `void () noexcept` from `src/os.cc` and `void ()` from
    `test/gtest-extra.cc`. Signature is tier-1 identity input (ARCH-001 §5), so leaving that
    in makes an untouched destructor read as delete + add the moment a test file is added or
    CMake reorders the database -- a difference in the *observers*, reported as a difference
    in the code.

    Stripped from every signature rather than only the unevaluated ones, because normalizing
    one side of a disagreement leaves the other side still spelling `noexcept`, and any rule
    that enumerates which functions get a lazy specification is a rule that goes quietly
    stale against the next clang. Nothing is lost: `attrs["exception_spec"]` carries the
    specification as diffable state, where a function that gains `noexcept` is the same
    entity with a changed attribute -- which is what it is -- rather than a different one.
    """
    if kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
        return None
    return _EXCEPTION_SPEC.sub("", _canonical_spelling(cursor.type)) or None


def _sugared_signature(cursor: ci.Cursor) -> str:
    """The function type as written, aliases kept, exception specification stripped.

    Compared state beside the canonical `signature`. When an alias's target changes, every
    function declared with the alias gets a new canonical signature while this one stays as
    written, and that pair -- canonical changed, written unchanged, the alias named in it -- is
    how classify files the function's change under the alias's (the
    `alias-target-changes-signature` rule, ARCH-001 §6.3). The canonical signature alone
    cannot tell that change from an edit to the function.

    The exception specification is stripped for the reason `_signature` gives: an implicit one
    is computed lazily, so it differs between translation units that see the same declaration.
    """
    return _EXCEPTION_SPEC.sub("", _stable_spelling(cursor.type.spelling))


def _observation_rank(node: Node) -> tuple[object, ...]:
    """A total order over the ways one entity can be observed. Highest wins.

    Three components, in order:

    1. **A definition outranks a declaration** (`_is_definition`). It carries the body or the
       value, and its location is where a reviewer wants to be sent (ARCH-001 §5).
    2. **Knowing outranks not knowing.** Whether a TU determined a given fact about an
       entity depends on what that TU needed -- clang leaves an unused special member's
       exception specification uncomputed, so one TU reports it and another reports None.
       Preferring the observation that determined more never discards a fact for a blank.
    3. **The content itself**, as a tie-break. It has to cover every field, because `max`
       over equal keys returns whichever it saw first, and "whichever it saw first" is the
       compile-database order this function exists to be independent of.
    """
    known = sum(
        value is not None
        for value in (node.signature, node.symbol, node.location, node.tu, *node.attrs.values())
    )
    return (
        _is_definition(node),
        known,
        node.name,
        node.qualified_name,
        node.signature or "",
        node.symbol or "",
        node.body_hash or "",
        node.tu or "",
        tuple(sorted((key, repr(value)) for key, value in node.attrs.items())),
        (node.location.file, node.location.line) if node.location else ("", 0),
    )


#: The fields one observation of an entity is compared on: all of them but `sites`, which is
#: merged rather than chosen (see `merge_node`).
_OBSERVED_FIELDS: tuple[str, ...] = tuple(name for name in Node.model_fields if name != "sites")


def _same_observation(a: Node, b: Node) -> bool:
    return all(getattr(a, name) == getattr(b, name) for name in _OBSERVED_FIELDS)


def _union_sites(have: tuple[Location, ...], more: tuple[Location, ...]) -> tuple[Location, ...]:
    """Sorted, deduplicated union; `have` itself, unchanged, when `more` adds nothing."""
    if not more or more == have:
        return have
    merged = set(have)
    merged.update(more)
    if len(merged) == len(have):
        return have
    return tuple(sorted(merged, key=lambda location: location.sort_key))


def _is_definition(node: Node) -> bool:
    """Whether an observation is of a definition, as far as the node can tell.

    A function's definition is the one with a body. A variable has none, so `extern` marks the
    declaration: `extern int x;` in a header and `int x = 5;` in a source file are one entity,
    and the definition holds its value and the `storage` it was defined with. A static data
    member's declaration in its class is not `extern`, so it ties with the definition here, and
    the definition wins on what it determined -- its value -- under `_observation_rank`.
    """
    if node.body_hash is not None:
        return True
    return node.kind is NodeKind.VARIABLE and node.attrs.get("storage") != "extern"


def merge_node(nodes: dict[NodeId, Node], node: Node) -> None:
    """Merge by USR. A declaration and its definition are one entity.

    The merged node is *one* of the observations, whole -- never a field from each. A node
    assembled field by field can describe a state that exists in no translation unit and in
    no source file: one TU's signature beside another TU's `exception_spec`, which is a
    contradiction the diff would faithfully report on. So the rule is to rank the
    observations and keep the winner entire (`_observation_rank`), which also makes the
    result independent of the order the observations arrive in -- `max` over a total order
    is commutative and associative, and a model that depended on compile-database order
    would diff non-empty against itself after a CMake reorder (ARCH-001 §9).

    Module-level, and not a method, because the rule has to be *one* rule. Within a TU a
    header is seen once per include path; across TUs it is seen once per TU, and the second
    case is the same merge as the first at a different scale. The builder calls this rather
    than reimplementing it, so a change to what "definitions win" means cannot apply to one
    scale and not the other.

    Nothing handed in is mutated, and nothing handed in is stored by reference once a merge
    has happened -- a partial model may come from the cache, and a cache entry silently
    rewritten by the build that read it is the confidently-wrong output ARCH-001 §9 names as
    the worst failure available here.

    **`sites` is the one deliberate exception to "one observation, whole".** It is the union
    of every observation's sites, because each observation sees only the declarations its TU
    reads -- the header's, or the source file's -- and the entity is declared in all of them.
    It is provenance and describes no state, so a union cannot assemble a contradiction. It is
    left out of the ranking, out of the fast path's comparison below, and out of
    `Model.fingerprint`, so the winner and the model's content are what they would be without
    it. A union is also order-independent, which keeps the merge commutative.
    """
    existing = nodes.get(node.id)
    if existing is None:
        nodes[node.id] = node
        return
    # The overwhelmingly common case, and the reason ranking is affordable: the same header
    # declaration seen again through another include path or another TU. Many entities are seen
    # by more than one TU, and very few of them are seen differently. Identical nodes have
    # identical sites, so this one comparison settles it before any site is looked at; it runs
    # millions of times on a real repository, and the field-by-field test below costs several
    # times more.
    if existing == node:
        return
    sites = _union_sites(existing.sites, node.sites)
    if _same_observation(existing, node):
        if sites is not existing.sites:
            nodes[node.id] = existing.model_copy(update={"sites": sites})
        return
    if _observation_rank(node) > _observation_rank(existing):
        nodes[node.id] = node.model_copy(deep=True, update={"sites": sites})
    elif sites is not existing.sites:
        nodes[node.id] = existing.model_copy(update={"sites": sites})


def merge_edge(
    edges: dict[str, Edge],
    src: str,
    dst: str,
    kind: EdgeKind,
    locations: Iterable[Location] = (),
    attrs: dict[str, AttrValue] | None = None,
) -> None:
    """Merge by (src, kind, dst); a repeated call site is a location, not an edge.

    Takes the parts rather than an `Edge` so that the object is built once per *distinct* edge
    instead of once per arrival -- an edge arrives once per TU that sees it -- and so that the
    merged graph never aliases a partial model's objects.

    Attributes belong to the relation, not to the site (`virtual` is a property of the
    callee, `access` of the base specifier), so two translation units observing the same
    edge should agree. When they do not, one of them knew less: `virtual` is read off
    `callee.is_virtual_method()` and is `False` both for "not virtual" and for a callee this
    TU could not resolve as a method, and `access` falls back to `"private"` where clang
    reports none. So the merge takes the *best-informed* observation under a total order
    (`_edge_attr_rank`) rather than the first to arrive.

    First-to-arrive was the bug this replaced, and it was silent: `setdefault` made the value
    a function of compile-database order, so the same two revisions could diff clean or dirty
    depending on which TU the pool happened to finish first. `merge_node` has been ordered
    for exactly this reason since it was written; edges were not.
    """
    # A self-edge is not a relation worth an event, and an empty USR is not an identity.
    # Enforced here rather than at the call sites because the builder creates both cases
    # by resolving two different instantiations of the same template onto it.
    if not src or not dst or src == dst:
        return
    key = f"{src}|{kind.value}|{dst}"
    edge = edges.get(key)
    if edge is None:
        edge = Edge(src=NodeId(src), dst=NodeId(dst), kind=kind, attrs=attrs or {})
        edges[key] = edge
    for location in locations:
        if location not in edge.locations:
            edge.locations.append(location)
    for name, value in (attrs or {}).items():
        current = edge.attrs.get(name)
        if current is None or _edge_attr_rank(value) > _edge_attr_rank(current):
            edge.attrs[name] = value


def _edge_attr_rank(value: AttrValue) -> tuple[bool, str]:
    """A total order over one edge attribute's observed values. Highest wins.

    Two components, and only the first is a judgement.

    **Truthy outranks falsy**, because every falsy value this producer emits is also what it
    emits when it does not know: `virtual=False` covers "not a virtual method" and "not
    resolvable as a method here", and `access` falls back to `"private"`. Preferring the
    positive observation never discards a fact for a default.

    **Then `repr`**, which is a tie-break and not a claim. Two truthy observations that
    disagree -- `"public"` against `"protected"` on one base specifier -- is a contradiction
    the producer should not be able to create, and if it ever does, the answer this picks is
    stable but arbitrary. Stable is the part that matters: `max` over a total order is
    commutative, so the model does not depend on which TU the pool finished first (§9).
    """
    return (bool(value), repr(value))


def merge_crossing(
    crossings: dict[str, Crossing],
    src: str,
    callee: str,
    kind: EdgeKind,
    label: str,
    locations: Iterable[Location] = (),
) -> None:
    """Merge by (src, kind, callee), as `merge_edge` merges edges: a repeated site is a location.

    Two overloads of one name in two libraries are one crossing; the label kept is the least,
    so the result does not depend on which arrived first.
    """
    if not src:
        return
    key = f"{src}|{kind.value}|{callee}"
    crossing = crossings.get(key)
    if crossing is None:
        crossing = Crossing(src=NodeId(src), callee=callee, kind=kind, label=label)
        crossings[key] = crossing
    elif label < crossing.label:
        crossing.label = label
    for location in locations:
        if location not in crossing.locations:
            crossing.locations.append(location)


def _qualified_name(cursor: ci.Cursor) -> str:
    # Every component is normalized, not just the cursor's own: a field of an anonymous
    # union is named through the union, so the leak reaches members that have perfectly
    # good names of their own -- 28 of fmt's 32 affected nodes were fields.
    parts = [_stable_spelling(cursor.spelling)]
    parent = cursor.semantic_parent
    while parent is not None and parent.kind is not ci.CursorKind.TRANSLATION_UNIT:
        if parent.spelling:
            parts.append(_stable_spelling(parent.spelling))
        parent = parent.semantic_parent
    return "::".join(reversed(parts))


@dataclass
class TuExtract:
    """A partial model: everything one translation unit can see.

    This is the cache unit (ARCH-001 §9), so it holds what one TU can *know* and nothing
    that depends on the others. `aliases` is the shape of that boundary: instantiation USR
    → the template it came from, offered to the builder rather than applied here, because
    whether an entity needs the alias depends on the merged node set.

    `targets` has the same shape for the same reason: every in-model edge target's qualified
    name and `fallback_label`, which the builder needs only if the edge stays unresolved and
    becomes a crossing. `crossings` are the relations this TU already saw leave the model.
    """

    path: str
    result: TuResult
    nodes: dict[NodeId, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    #: Declarations this TU saw in the repository's own code and did not model, as (file,
    #: line, column, kind). A set, so a header seen through two include paths counts once.
    unmodelled: set[tuple[str, int, int, str]] = field(default_factory=set)
    crossings: dict[str, Crossing] = field(default_factory=dict)
    targets: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.result.status is TuStatus.PARSED


#: Declaration cursors that are not entities: counted as unmodelled, they would claim the
#: model is missing something that is part of another declaration.
_NOT_ENTITIES = frozenset(
    {
        ci.CursorKind.CXX_ACCESS_SPEC_DECL,
        ci.CursorKind.TEMPLATE_TYPE_PARAMETER,
        ci.CursorKind.TEMPLATE_NON_TYPE_PARAMETER,
        ci.CursorKind.TEMPLATE_TEMPLATE_PARAMETER,
    }
)


class _Extractor:
    def __init__(
        self,
        root: Path,
        base: Path,
        toolchain: Toolchain,
        tu: str,
        boundary: Boundary = NO_BOUNDARY,
        dirs: tuple[Path, ...] = (),
    ) -> None:
        self.root = root
        self.base = base
        self.toolchain = toolchain
        self.boundary = boundary
        #: This TU's header search directories (`search_dirs`), for naming libraries.
        self.dirs = dirs
        self.arch = multiarch(toolchain.target)
        #: File name → where it lies, None for a file the model covers. Asked per cursor.
        self._outside_memo: dict[str, str | None] = {}
        self.crossings: dict[str, Crossing] = {}
        self.targets: dict[str, tuple[str, str]] = {}
        #: USR → qualified name, for every target `relate` has named.
        self._names: dict[str, str] = {}
        #: This TU's own primary source, repo-relative -- the same form `_location` produces,
        #: so `_defining_tu` compares like with like rather than a path against a path shape.
        self.tu = tu
        self.nodes: dict[NodeId, Node] = {}
        self.edges: dict[str, Edge] = {}
        self.aliases: dict[str, str] = {}
        self.unmodelled: set[tuple[str, int, int, str]] = set()
        #: A macro's clang USR -> its node id, or None for an include guard; and how many
        #: macros of each (file, name) this TU has seen, for the ordinal in `macro_id`. Keyed
        #: on the USR so that a header included twice is not counted twice.
        self.macros: dict[str, str | None] = {}
        self.macro_counts: dict[tuple[str, str], int] = {}
        #: A static assertion's position (file, offset) -> its node id, and how many of each id
        #: this TU has seen. Keyed on the position for the reason `macros` is keyed on the USR.
        self.static_assertions: dict[tuple[str, int], str] = {}
        self.static_assertion_counts: dict[str, int] = {}

    def _defining_tu(self, usr: str, location: Location | None) -> str | None:
        """The defining translation unit, set only where it is a *fact* (ARCH-001 §5).

        §5 requires identity to include the TU for entities whose USR is file-scoped: clang
        writes a file *name* into those, so `static void helper()` in `a/util.cpp` and an
        unrelated one in `b/util.cpp` produce the same USR and merge into one node. A body
        change in whichever lost the merge then produces no event at all -- under-reporting,
        which §5 refuses more strongly than it refuses noise.

        Two conditions, and the second is the load-bearing one.

        **A file-scoped USR only.** For external linkage the same entity legitimately appears
        in every TU that includes its header, and `DeclaredTier.key` deliberately excludes the
        TU for exactly that reason: the set of TUs that can see a declaration changes for
        reasons that have nothing to do with the change under review.

        **And the declaration is in this TU's own primary source.** A `static` declared in a
        *header* is a different entity per includer, and USR dedup collapses all of them into
        one node whose TU is whichever observation ranked highest. That winner flips when an
        unrelated source file joins the build -- so recording it would make identity depend on
        which *other* files exist, and break tier 1 for an entity the review never touched.
        Where the declaration is in the TU's own source, exactly one TU can see it and there
        is nothing to choose between.

        The second condition trades some §5 coverage for not inventing a new source of
        revision-flippable identity. Everywhere it declines, `tu` stays None and matching is
        exactly what it was before -- `DeclaredTier` already documents that fallback.

        Repo-relative on both sides of the comparison, because both come from `_relative` with
        this extractor's root. An absolute path would carry the SHA-named worktree directory
        into every partial, base and head would never agree, and every internal-linkage match
        would break on every comparison -- the class of bug that once leaked the host path into
        identity.
        """
        if usr_file(usr) is None:
            return None
        if location is None or location.file != self.tu:
            return None
        return self.tu

    def outside(self, file_name: str) -> str | None:
        """The crossing label for a file outside the model, or None if the model covers it.

        Outside the repository, the library's name; under a third-party path, that path
        (ADR-025), which wins over `untracked` for a submodule's files; a file git does not
        track at this revision, `untracked` (ADR-037).
        """
        if file_name in self._outside_memo:
            return self._outside_memo[file_name]
        relative = _relative(file_name, self.root, self.base)
        label: str | None
        if relative is None:
            label = library_label(
                file_name, self.base, self.dirs, self.toolchain.builtin_dir, self.arch
            )
        elif owner := third_party_owner(relative, self.boundary.third_party):
            label = owner
        elif self.boundary.tracked is not None and relative not in self.boundary.tracked:
            label = UNTRACKED
        else:
            label = None
        self._outside_memo[file_name] = label
        return label

    def relate(
        self,
        src: str,
        target: ci.Cursor,
        kind: EdgeKind,
        location: Location | None,
        **attrs: AttrValue,
    ) -> None:
        """An edge to `target`, or a crossing if `target` is declared outside the model.

        The label is known here, from the declaration's file, so nothing guesses it later
        (ADR-025). A target inside the model gets an edge and a `targets` entry, which
        the builder turns into a crossing only if no node answers the edge.
        """
        usr = target.get_usr()
        if not usr:
            return
        file = target.location.file
        label = self.outside(file.name) if file is not None else None
        if label is not None:
            merge_crossing(
                self.crossings,
                src,
                self._name(target, usr),
                kind,
                label,
                () if location is None else (location,),
            )
            return
        self.note_alias(target, usr)
        if usr not in self.targets:
            self.targets[usr] = (self._name(target, usr), fallback_label(target, usr))
        self.add_edge(src, usr, kind, location, **attrs)

    def _name(self, cursor: ci.Cursor, usr: str) -> str:
        """`_qualified_name`, once per USR per TU: it walks every ancestor, and a target is
        met once per call site (ARCH-001 §9's budget is spent here)."""
        name = self._names.get(usr)
        if name is None:
            name = self._names[usr] = _qualified_name(cursor)
        return name

    def note_alias(self, cursor: ci.Cursor, usr: str) -> None:
        """Record `usr` as a possible instantiation of a template the builder may know."""
        if usr in self.aliases:
            return
        if (template := specialized_template_usr(cursor)) and template != usr:
            self.aliases[usr] = template

    # -- graph assembly -------------------------------------------------------

    def add_node(self, node: Node) -> None:
        merge_node(self.nodes, node)

    def add_edge(
        self,
        src: str,
        dst: str,
        kind: EdgeKind,
        location: Location | None,
        **attrs: AttrValue,
    ) -> None:
        merge_edge(self.edges, src, dst, kind, () if location is None else (location,), attrs)

    # -- traversal ------------------------------------------------------------

    def visit(self, cursor: ci.Cursor) -> None:
        """Descend the declaration structure, pruning anything outside the model.

        Outside the model is outside the repository or inside a third-party directory
        (ADR-025). Pruning by the cursor's own file is what keeps a TU's cost proportional
        to the project rather than to libstdc++. It is safe even for namespaces reopened
        across headers: each reopening is a separate cursor with its own location.
        """
        for child in cursor.get_children():
            # Checked before the location, which costs a call into libclang: a translation
            # unit holds one of these per macro use and per `#include`, in every header.
            if child.kind in _PREPROCESSOR_USES:
                continue
            file = child.location.file
            if file is not None and self.outside(file.name) is not None:
                continue
            if child.kind is ci.CursorKind.MACRO_DEFINITION:
                self.visit_macro(child)
            else:
                self.visit_declaration(child)

    def visit_macro(self, cursor: ci.Cursor) -> None:
        """A macro defined in the repository, as a node with its replacement hashed (ADR-039).

        A macro with no file is built in or given on the command line, and is not a
        declaration the repository wrote. An include guard is skipped: it is how a header is
        written, not a value, and it would be renamed with its header.
        """
        location = _location(cursor, self.root, self.base)
        usr = cursor.get_usr()
        if location is None or not usr:
            return
        if usr not in self.macros:
            self.macros[usr] = self._macro_node_id(cursor, usr, location)
        node_id = self.macros[usr]
        if node_id is None:
            return
        self.add_node(
            Node(
                id=NodeId(node_id),
                kind=NodeKind.MACRO,
                name=cursor.spelling,
                qualified_name=cursor.spelling,
                attrs=_attributes(cursor, NodeKind.MACRO),
                location=location,
                sites=(location,),
                tu=self._defining_tu(node_id, location),
            )
        )

    def _macro_node_id(self, cursor: ci.Cursor, usr: str, location: Location) -> str | None:
        if _is_include_guard(cursor):
            return None
        key = (location.file, cursor.spelling)
        self.macro_counts[key] = self.macro_counts.get(key, 0) + 1
        return macro_id(usr, self.macro_counts[key])

    def visit_static_assertion(self, cursor: ci.Cursor) -> None:
        """A `static_assert` at namespace or class scope, as a node (ADR-039).

        One inside a function body is part of the body, which `body_hash` compares.

        One a macro writes is skipped, wherever the macro is defined. googletest's `TEST`
        writes a `static_assert` at each use, so modelling those would add a top-level
        addition for every new test -- noise in the most-reviewed kind of change -- and the
        assertion is the macro's text, which the macro's own node already compares when the
        macro is the repository's.
        """
        location = _location(cursor, self.root, self.base)
        if location is None or _written_by_a_macro(cursor):
            return
        parent = cursor.semantic_parent
        if parent is not None and parent.kind not in MODELLED_KINDS:
            parent = None
        parent_usr = parent.get_usr() if parent is not None else ""
        position = (location.file, cursor.location.offset)
        node_id = self.static_assertions.get(position)
        if node_id is None:
            children = list(cursor.get_children())
            condition = _token_hash(children[0]) if children else None
            file = location.file.rsplit("/", 1)[-1]
            first = static_assertion_id(file, parent_usr, condition, 1)
            count = self.static_assertion_counts.get(first, 0) + 1
            self.static_assertion_counts[first] = count
            node_id = static_assertion_id(file, parent_usr, condition, count)
            self.static_assertions[position] = node_id
        scope = _qualified_name(parent) if parent is not None else ""
        self.add_node(
            Node(
                id=NodeId(node_id),
                kind=NodeKind.STATIC_ASSERTION,
                name="static_assert",
                qualified_name=f"{scope}::static_assert" if scope else "static_assert",
                attrs=_attributes(cursor, NodeKind.STATIC_ASSERTION),
                location=location,
                sites=(location,),
                tu=self._defining_tu(node_id, location),
            )
        )
        if parent is not None:
            self.note_alias(parent, parent_usr)
            self.add_edge(parent_usr, node_id, EdgeKind.CONTAINS, None)

    def note_unmodelled(self, cursor: ci.Cursor) -> None:
        """Record a declaration the model skips, so the run can count it by kind (#6).

        Only real declarations: an access specifier is a label inside a class and a template
        parameter belongs to its template, and counting either would report as missing what
        is not an entity. A skipped declaration is not descended into, so an unmodelled
        record's members are not counted again. Located in the repository or not at all:
        `visit` has already pruned what lies outside the model. A C-style typedef that only
        names its own struct is skipped on purpose and loses nothing -- the struct is the
        node -- so it is not counted either.
        """
        if not cursor.kind.is_declaration() or cursor.kind in _NOT_ENTITIES:
            return
        if _names_its_own_tag(cursor):
            return
        location = _location(cursor, self.root, self.base)
        if location is None:
            return
        label = cursor.kind.name.lower().removesuffix("_decl")
        self.unmodelled.add((location.file, location.line, location.column or 0, label))

    def visit_declaration(self, cursor: ci.Cursor) -> None:
        if cursor.kind is ci.CursorKind.STATIC_ASSERT:
            self.visit_static_assertion(cursor)
            return
        kind = _node_kind(cursor)
        if kind is None:
            if cursor.kind in CONTAINER_KINDS:
                self.visit(cursor)
            else:
                self.note_unmodelled(cursor)
            return

        usr = cursor.get_usr()
        if not usr:
            # A kind the model covers, but with no identity to key it on: skipped, so counted.
            self.note_unmodelled(cursor)
            return

        location = _location(cursor, self.root, self.base)
        self.add_node(
            Node(
                id=NodeId(usr),
                kind=kind,
                name=_stable_spelling(cursor.spelling),
                qualified_name=_qualified_name(cursor),
                signature=_signature(cursor, kind),
                symbol=_mangled(cursor),
                attrs=_attributes(cursor, kind),
                body_hash=body_hash(cursor) if cursor.is_definition() else None,
                location=location,
                sites=() if location is None else (location,),
                tu=self._defining_tu(usr, location),
            )
        )

        parent = cursor.semantic_parent
        if parent is not None and parent.kind in MODELLED_KINDS:
            parent_usr = parent.get_usr()
            self.note_alias(parent, parent_usr)
            self.add_edge(parent_usr, usr, EdgeKind.CONTAINS, None)

        if cursor.kind in CALLABLE_KINDS:
            for overridden in overridden_cursors(cursor):
                self.relate(
                    usr, overridden, EdgeKind.OVERRIDES, _location(cursor, self.root, self.base)
                )
            if cursor.is_definition():
                self.visit_body(cursor, usr)

        # Modelled containers still have to be descended into -- a namespace is a node
        # *and* the parent of everything a review is actually about.
        if kind in (NodeKind.CLASS, NodeKind.STRUCT, NodeKind.UNION):
            self.visit_record(cursor, usr)
        elif cursor.kind in CONTAINER_KINDS:
            self.visit(cursor)

    def visit_record(self, cursor: ci.Cursor, usr: str) -> None:
        for child in cursor.get_children():
            if child.kind is ci.CursorKind.CXX_BASE_SPECIFIER:
                base = child.referenced
                if base is not None:
                    # The base specifier's own USR is empty; identity is the base's.
                    self.relate(
                        usr,
                        base,
                        EdgeKind.INHERITS,
                        _location(child, self.root, self.base),
                        access=_access(child) or "private",
                        virtual=is_virtual_base(child),
                    )
                continue
            # A member can come from another file: an `#include` inside the class body.
            # Pruned like `visit` prunes, so a third-party member never enters the model.
            file = child.location.file
            if file is not None and self.outside(file.name) is not None:
                continue
            self.visit_declaration(child)

    def visit_body(self, cursor: ci.Cursor, usr: str) -> None:
        """Call edges out of one definition.

        `CALL_EXPR` only. The `DECL_REF_EXPR` that accompanies every call would double
        every edge, and taking a function's address is a different relation than calling it
        -- one this scope does not model (ARCH-001 §13).
        """
        for node in cursor.walk_preorder():
            if node.kind is not ci.CursorKind.CALL_EXPR:
                continue
            callee = node.referenced
            if callee is None or callee.kind not in CALLABLE_KINDS:
                continue
            self.relate(
                usr,
                callee,
                EdgeKind.CALLS,
                _location(node, self.root, self.base),
                virtual=callee.is_virtual_method()
                if callee.kind is ci.CursorKind.CXX_METHOD
                else False,
            )


def extract(
    entry: CompileEntry,
    *,
    root: Path,
    toolchain: Toolchain,
    index: ci.Index | None = None,
    boundary: Boundary = NO_BOUNDARY,
) -> TuExtract:
    """Parse one translation unit into a partial model.

    `boundary` says what inside the repository is not modelled: third-party paths (ADR-025)
    and untracked files (ADR-037). Relations into them, and out of the repository, are
    crossings.

    Never raises for a bad translation unit: a TU that cannot be parsed is recorded as a
    `TuResult` and carried into `Completeness` (ARCH-001 §4.4, §11). One unparseable file
    is incompleteness to be reported, not a reason to abandon a review -- while a broken
    *toolchain* is a precondition failure and is refused before this function runs.
    """
    root = root.resolve()
    relative = _relative(str(entry.file), root, entry.directory) or entry.file.name
    # A fresh name rather than reassigning `index`: cindex leaves `Index.create` unannotated,
    # so its result does not narrow the `Index | None` the parameter is declared as.
    parser: ci.Index = index if index is not None else ci.Index.create()

    try:
        # The detailed processing record is what puts macro definitions in the AST.
        tu = parser.parse(
            str(entry.file),
            args=parse_arguments(entry, toolchain),
            options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
    except ci.TranslationUnitLoadError as error:
        return TuExtract(
            path=relative,
            result=TuResult(path=relative, status=TuStatus.FAILED, detail=str(error)),
        )

    # libclang reports a truncated AST through diagnostics, not exceptions. Extracting
    # from one produces a model that is confidently missing declarations, which is the
    # failure this project exists to avoid (ARCH-001 §11).
    if failures := fatal_diagnostics(tu):
        return TuExtract(
            path=relative,
            result=TuResult(
                path=relative,
                status=TuStatus.FAILED,
                detail="; ".join(failures[:3]),
            ),
        )

    extractor = _Extractor(
        root, entry.directory, toolchain, relative, boundary, search_dirs(entry, toolchain)
    )
    extractor.visit(tu.cursor)
    return TuExtract(
        path=relative,
        result=TuResult(path=relative, status=TuStatus.PARSED),
        nodes=extractor.nodes,
        edges=extractor.edges,
        aliases=extractor.aliases,
        unmodelled=extractor.unmodelled,
        crossings=extractor.crossings,
        targets=extractor.targets,
    )
