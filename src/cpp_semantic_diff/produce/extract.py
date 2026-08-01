"""One translation unit in, one partial model out.

ARCH-001 §4 (what a model contains) and §9 (what a cache stores). The unit here is
deliberately the **translation unit**, not the worktree: §9 keys the cache on
`hash(TU content, transitive header content, flags, producer version)` and assembles a
commit's model from cached partials, so the boundary the cache needs has to be the
boundary the extractor already produces. Extracting a whole worktree in one pass would
have to be taken apart again to cache it.

Two consequences follow from that choice, and both are correctness issues rather than
tidiness:

**Every header is parsed once per TU that includes it.** A `TuExtract` therefore describes
far more than its own file, and the same entity appears in every TU that sees its
declaration. Nodes are keyed by USR so that merging is idempotent -- the CodeAtlas
measurements found 15,656 method cursors collapsing to 161 classes in FTXUI (ARCH-001 §5).

**A partial model has dangling edges by construction.** A call whose target is declared in
a header this TU does not include still belongs in the graph; it just cannot be resolved
here. Edges are emitted optimistically and resolved against the merged node set by the
builder, which is the only place that knows the whole picture.
"""

from __future__ import annotations

import ctypes
import hashlib
from dataclasses import dataclass, field
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
)
from cpp_semantic_diff.produce.toolchain import Toolchain, fatal_diagnostics

#: The v0 extraction scope (ARCH-001 §12): namespace, class, function, method, field.
#: Anything not named here is not modelled -- enums, aliases, and namespace-scope variables
#: are deliberate omissions, not oversights. Adding a kind is additive; every kind added
#: before the delta exists is a kind whose events nothing consumes.
#:
#: Templates map onto their underlying kind with `attrs["template"]`. A template and its
#: instantiations need separate identity (ARCH-001 §5), and this gets that for free: only
#: *declared* templates appear as cursors, so implicit instantiations never enter the model
#: and cannot produce instantiation-only deltas.
MODELLED_KINDS: dict[ci.CursorKind, NodeKind] = {
    ci.CursorKind.NAMESPACE: NodeKind.NAMESPACE,
    ci.CursorKind.CLASS_DECL: NodeKind.CLASS,
    ci.CursorKind.CLASS_TEMPLATE: NodeKind.CLASS,
    ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION: NodeKind.CLASS,
    ci.CursorKind.STRUCT_DECL: NodeKind.STRUCT,
    ci.CursorKind.UNION_DECL: NodeKind.UNION,
    ci.CursorKind.FUNCTION_DECL: NodeKind.FUNCTION,
    ci.CursorKind.FUNCTION_TEMPLATE: NodeKind.FUNCTION,
    ci.CursorKind.CXX_METHOD: NodeKind.METHOD,
    ci.CursorKind.CONSTRUCTOR: NodeKind.METHOD,
    ci.CursorKind.DESTRUCTOR: NodeKind.METHOD,
    ci.CursorKind.CONVERSION_FUNCTION: NodeKind.METHOD,
    ci.CursorKind.FIELD_DECL: NodeKind.FIELD,
}

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

#: Function bodies are deliberately *not* skipped. `calls` edges and `body_hash` both need
#: them, and the spike measured bodies at 61 % of parse time for only 2 % of the USRs --
#: a real two-tier lever later, but not one v0 can pull.
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
    (`...@F@ignore_unused<#L>#&1L#`) is not the template's (`...@FT@>1#T...`) and belongs to
    no declaration anyone wrote. On fmt this is 10,158 call edges, roughly a third of the
    graph, so treating it as a curiosity is not an option.

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


def _relative(path: str, root: Path, base: Path) -> str | None:
    """Repository-relative path, or None if the file is outside the tree under analysis.

    `base` is the compile database's `directory`, and it is not optional. libclang echoes
    back whatever path resolved the include, so a header reached through a relative `-I`
    comes back relative -- and resolving that against the *process* working directory
    silently places it outside the repository, dropping every declaration in it from the
    model without a single diagnostic.
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
    return Location(
        file=relative,
        line=cursor.location.line,
        end_line=cursor.extent.end.line,
        column=cursor.location.column,
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
        attrs["return_type"] = cursor.result_type.get_canonical().spelling
        attrs["template"] = cursor.kind is ci.CursorKind.FUNCTION_TEMPLATE
        attrs["deleted"] = cursor.is_deleted_method()
        attrs["defaulted"] = cursor.is_default_method()
        # The spec kind, not a `noexcept: bool`. Collapsing it loses the distinction
        # between `noexcept` and `noexcept(false)` -- both are COMPUTED/BASIC specs, and
        # one of them still throws. `none -> basic_noexcept` diffs just as mechanically
        # and does not claim something untrue.
        spec = cursor.exception_specification_kind
        attrs["exception_spec"] = str(spec.name).lower() if spec is not None else None
    if kind is NodeKind.METHOD:
        attrs["static"] = cursor.is_static_method()
        attrs["virtual"] = cursor.is_virtual_method()
        attrs["pure_virtual"] = cursor.is_pure_virtual_method()
        attrs["const"] = cursor.is_const_method()
        attrs["explicit"] = cursor.is_explicit_method()
    if kind is NodeKind.FIELD:
        attrs["type"] = cursor.type.get_canonical().spelling
        attrs["mutable"] = cursor.is_mutable_field()
        if cursor.is_bitfield():
            attrs["bitfield_width"] = cursor.get_bitfield_width()
    if kind in (NodeKind.CLASS, NodeKind.STRUCT, NodeKind.UNION):
        attrs["template"] = cursor.kind in (
            ci.CursorKind.CLASS_TEMPLATE,
            ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
        )
        attrs["abstract"] = cursor.is_abstract_record()
    return attrs


def _signature(cursor: ci.Cursor, kind: NodeKind) -> str | None:
    """Canonical type spelling: typedefs resolved, qualifiers explicit (ARCH-001 §4.1).

    Tier-1 identity cannot distinguish overloads without it. The canonical form is what
    makes `size_t` and `unsigned long` the same signature, so an unrelated typedef change
    does not read as a signature change.
    """
    if kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
        return None
    return cursor.type.get_canonical().spelling or None


def _qualified_name(cursor: ci.Cursor) -> str:
    parts = [cursor.spelling]
    parent = cursor.semantic_parent
    while parent is not None and parent.kind is not ci.CursorKind.TRANSLATION_UNIT:
        if parent.spelling:
            parts.append(parent.spelling)
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

    @property
    def ok(self) -> bool:
        return self.result.status is TuStatus.PARSED


class _Extractor:
    def __init__(self, root: Path, base: Path, toolchain: Toolchain) -> None:
        self.root = root
        self.base = base
        self.toolchain = toolchain
        self.nodes: dict[NodeId, Node] = {}
        self.edges: dict[str, Edge] = {}
        self.aliases: dict[str, str] = {}

    def note_alias(self, cursor: ci.Cursor, usr: str) -> None:
        """Record `usr` as a possible instantiation of a template the builder may know."""
        if usr in self.aliases:
            return
        if (template := specialized_template_usr(cursor)) and template != usr:
            self.aliases[usr] = template

    # -- graph assembly -------------------------------------------------------

    def add_node(self, node: Node) -> None:
        """Merge by USR. A declaration and its definition are one entity.

        Definitions win: they carry the body, and their location is where a reviewer
        wants to be sent. ARCH-001 §5 -- dedup is a correctness requirement, since every
        header is reparsed once per TU.
        """
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return
        if node.body_hash is not None and existing.body_hash is None:
            merged = node.model_copy()
            merged.attrs = {**existing.attrs, **node.attrs}
            self.nodes[node.id] = merged
        else:
            existing.attrs.update(node.attrs)
            if existing.symbol is None:
                existing.symbol = node.symbol

    def add_edge(
        self,
        src: str,
        dst: str,
        kind: EdgeKind,
        location: Location | None,
        **attrs: AttrValue,
    ) -> None:
        """Merge by (src, kind, dst); a repeated call site is a location, not an edge."""
        if not src or not dst or src == dst:
            return
        key = f"{src}|{kind.value}|{dst}"
        edge = self.edges.get(key)
        if edge is None:
            edge = Edge(src=NodeId(src), dst=NodeId(dst), kind=kind, attrs=attrs)
            self.edges[key] = edge
        if location is not None and location not in edge.locations:
            edge.locations.append(location)

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
        kind = MODELLED_KINDS.get(cursor.kind)
        if kind is None:
            if cursor.kind in CONTAINER_KINDS:
                self.visit(cursor)
            return

        usr = cursor.get_usr()
        if not usr:
            return

        self.add_node(
            Node(
                id=NodeId(usr),
                kind=kind,
                name=cursor.spelling,
                qualified_name=_qualified_name(cursor),
                signature=_signature(cursor, kind),
                symbol=_mangled(cursor),
                attrs=_attributes(cursor, kind),
                body_hash=body_hash(cursor) if cursor.is_definition() else None,
                location=_location(cursor, self.root, self.base),
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
        -- one this scope does not model (ARCH-001 §12).
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
    index = index or ci.Index.create()

    try:
        tu = index.parse(str(entry.file), args=parse_arguments(entry, toolchain))
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

    extractor = _Extractor(root, entry.directory, toolchain)
    extractor.visit(tu.cursor)
    return TuExtract(
        path=relative,
        result=TuResult(path=relative, status=TuStatus.PARSED),
        nodes=extractor.nodes,
        edges=extractor.edges,
        aliases=extractor.aliases,
    )
