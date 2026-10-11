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
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import clang.cindex as ci

from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.model.ir import (
    AttrValue,
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

#: The v0 extraction scope (ARCH-001 §13): namespace, class, function, method, field.
#: Anything not named here is not modelled -- enums, aliases, and namespace-scope variables
#: are deliberate omissions, not oversights. Adding a kind is additive; every kind added
#: before the delta exists is a kind whose events nothing consumes.
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
}

#: What no model this extractor builds can show, stated on every run (ADR-029). The first is
#: libclang's limit: it does not expose the copy and move operations the compiler writes, so
#: adding a destructor -- which removes a class's implicit move operations and turns every move
#: into a copy -- changes nothing the model holds. The second is this extractor's own scope:
#: a macro is not a declaration it records. Neither is worked around; a run that cannot see
#: a change says so rather than reporting that nothing changed (ARCH-001 §11).
LIBCLANG_LIMITS: tuple[str, ...] = (
    "Implicit special members are not visible. libclang does not expose the copy and move "
    "operations the compiler writes, so a change that adds or removes one implicitly -- adding "
    "a destructor removes the implicit move operations -- produces no change event.",
    "Macros are not modelled. A change to a macro definition produces no change event of its "
    "own; it shows only through the declarations it changes.",
)

#: Kinds whose children are worth descending into. A function body is walked separately
#: for calls; nothing else nests declarations that v0 models.
CONTAINER_KINDS = frozenset(
    {
        ci.CursorKind.NAMESPACE,
        ci.CursorKind.CLASS_DECL,
        ci.CursorKind.CLASS_TEMPLATE,
        ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
        ci.CursorKind.STRUCT_DECL,
        ci.CursorKind.UNION_DECL,
        ci.CursorKind.UNEXPOSED_DECL,  # extern "C" { ... } and linkage specs
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

    def __call__(self, cursor: ci.Cursor) -> list[str]:
        array = ctypes.POINTER(ci.Cursor)()
        count = ctypes.c_uint()
        self._get(cursor, ctypes.byref(array), ctypes.byref(count))
        if not count.value:
            return []
        try:
            usrs = []
            for i in range(count.value):
                overridden = array[i]
                # The bindings normally attach the owning TU in `from_result`; a cursor
                # lifted straight out of the C array has to be told, or `get_usr` faults.
                overridden._tu = cursor._tu
                usrs.append(overridden.get_usr())
            return usrs
        finally:
            self._dispose(array)


_overridden_cursors: _Overrides | None = None


def overridden_usrs(cursor: ci.Cursor) -> list[str]:
    global _overridden_cursors
    if _overridden_cursors is None:
        _overridden_cursors = _Overrides()
    return _overridden_cursors(cursor)


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
    if cursor.kind in TEMPLATED_RECORD_KINDS:
        return TAG_KINDS.get(template_tag_kind(cursor), kind)
    if cursor.kind is ci.CursorKind.FUNCTION_TEMPLATE and _is_member(cursor):
        return NodeKind.METHOD
    return kind


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
    deliberately *not* hashed -- only the compound statement. Hashing token spellings
    rather than source text is what makes reindentation and comment edits invisible.
    """
    body = next(
        (c for c in cursor.get_children() if c.kind is ci.CursorKind.COMPOUND_STMT),
        None,
    )
    if body is None:
        return None
    digest = hashlib.blake2b(digest_size=16)
    empty = True
    for token in body.get_tokens():
        if token.kind is ci.TokenKind.COMMENT:
            continue
        digest.update(token.spelling.encode())
        digest.update(b"\x00")
        empty = False
    return None if empty else digest.hexdigest()


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
        attrs["template"] = cursor.kind is ci.CursorKind.FUNCTION_TEMPLATE
        attrs["template_parameters"] = _template_parameters(cursor)
        attrs["deleted"] = cursor.is_deleted_method()
        attrs["defaulted"] = cursor.is_default_method()
        attrs["exception_spec"] = _exception_spec(cursor)
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
    return attrs


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
    template parameter, and `...` marks a pack. Default arguments are left out, as they are
    for function parameters.
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


def _observation_rank(node: Node) -> tuple[object, ...]:
    """A total order over the ways one entity can be observed. Highest wins.

    Three components, in order:

    1. **A definition outranks a declaration.** It carries the body, and its location is
       where a reviewer wants to be sent (ARCH-001 §5).
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
        node.body_hash is not None,
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
    """

    path: str
    result: TuResult
    nodes: dict[NodeId, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    #: Declarations this TU saw in the repository and did not model, as (file, line, column,
    #: kind). A set, so a header seen through two include paths counts once. Reserved and not
    #: yet filled: the cache stores it already, so filling it changes no entry format.
    unmodelled: set[tuple[str, int, int, str]] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return self.result.status is TuStatus.PARSED


class _Extractor:
    def __init__(self, root: Path, base: Path, toolchain: Toolchain, tu: str) -> None:
        self.root = root
        self.base = base
        self.toolchain = toolchain
        #: This TU's own primary source, repo-relative -- the same form `_location` produces,
        #: so `_defining_tu` compares like with like rather than a path against a path shape.
        self.tu = tu
        self.nodes: dict[NodeId, Node] = {}
        self.edges: dict[str, Edge] = {}
        self.aliases: dict[str, str] = {}

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
        """Descend the declaration structure, pruning anything outside the repository.

        Pruning by the cursor's own file is what keeps a TU's cost proportional to the
        project rather than to libstdc++. It is safe even for namespaces reopened across
        headers: each reopening is a separate cursor with its own location.
        """
        for child in cursor.get_children():
            file = child.location.file
            if file is not None and _relative(file.name, self.root, self.base) is None:
                continue
            self.visit_declaration(child)

    def visit_declaration(self, cursor: ci.Cursor) -> None:
        kind = _node_kind(cursor)
        if kind is None:
            if cursor.kind in CONTAINER_KINDS:
                self.visit(cursor)
            return

        usr = cursor.get_usr()
        if not usr:
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
            for base_usr in overridden_usrs(cursor):
                self.add_edge(
                    usr, base_usr, EdgeKind.OVERRIDES, _location(cursor, self.root, self.base)
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
                    base_usr = base.get_usr()
                    self.note_alias(base, base_usr)
                    self.add_edge(
                        usr,
                        base_usr,
                        EdgeKind.INHERITS,
                        _location(child, self.root, self.base),
                        access=_access(child) or "private",
                    )
            else:
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
            callee_usr = callee.get_usr()
            self.note_alias(callee, callee_usr)
            self.add_edge(
                usr,
                callee_usr,
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
) -> TuExtract:
    """Parse one translation unit into a partial model.

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
        tu = parser.parse(str(entry.file), args=parse_arguments(entry, toolchain))
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

    extractor = _Extractor(root, entry.directory, toolchain, relative)
    extractor.visit(tu.cursor)
    return TuExtract(
        path=relative,
        result=TuResult(path=relative, status=TuStatus.PARSED),
        nodes=extractor.nodes,
        edges=extractor.edges,
        aliases=extractor.aliases,
    )
