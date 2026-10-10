"""PLAN-004 step 1: what the new boundary rule would do to each fixture's model.

For each revision of each range, builds the model the way `cppsd diff` does (serial, no cache,
so the probe below sees every cursor) and reports:

1. nodes per label the new rule would assign: third-party (a submodule path or a declared
   path), untracked (a file git does not track at that revision), first-party (the rest);
2. edges whose target is declared in a file under the repository root but resolves to no
   node: the "unresolved in-repository callees" §3.3 claims are all compiler-generated.

Fixture clones are expected at ~/fixtures/<name>. Prints one JSON object per fixture; measure.json
is the three merged. Run from the repository root:
  uv run python reports/2026-10-10--plan-004-baseline/measure.py fmt spdlog leveldb
"""

from __future__ import annotations

import collections
import importlib
import json
import subprocess
import sys
from pathlib import Path

import clang.cindex as ci

from cpp_semantic_diff.acquire import resolve_revs
from cpp_semantic_diff.acquire.workspace import checkout
from cpp_semantic_diff.produce.build import assemble, resolve, tu_extracts
from cpp_semantic_diff.produce.toolchain import load

# The module, not the `extract` function the package re-exports under the same name.
ex = importlib.import_module("cpp_semantic_diff.produce.extract")

FIXTURES = Path.home() / "fixtures"
RANGES = {
    "fmt": ("HEAD~12", "HEAD", ["test/gtest"]),
    "spdlog": ("HEAD~30", "HEAD", ["include/spdlog/fmt/bundled"]),
    "leveldb": ("HEAD~9", "HEAD~1", []),
}

# Every cursor an edge target came from, keyed by USR: where it is declared and what it is.
TARGETS: dict[str, dict[str, object]] = {}
_orig_note_alias = ex._Extractor.note_alias


def _note_alias(self: ex._Extractor, cursor: ci.Cursor, usr: str) -> None:
    if usr not in TARGETS:
        file = cursor.location.file
        rel = ex._relative(file.name, self.root, self.base) if file is not None else None
        parent = cursor.semantic_parent
        pfile = parent.location.file if parent is not None else None
        same_loc = bool(
            parent is not None
            and pfile is not None
            and file is not None
            and pfile.name == file.name
            and parent.location.line == cursor.location.line
            and parent.location.column == cursor.location.column
        )
        k = cursor.kind
        special = None
        if k is ci.CursorKind.DESTRUCTOR:
            special = "destructor"
        elif k is ci.CursorKind.CONSTRUCTOR:
            if cursor.is_default_constructor():
                special = "default ctor"
            elif cursor.is_copy_constructor():
                special = "copy ctor"
            elif cursor.is_move_constructor():
                special = "move ctor"
            else:
                special = "other ctor"
        elif k is ci.CursorKind.CXX_METHOD:
            if cursor.is_copy_assignment_operator_method():
                special = "copy assign"
            elif cursor.is_move_assignment_operator_method():
                special = "move assign"
        in_function = False
        walk = parent
        while walk is not None and walk.kind is not ci.CursorKind.TRANSLATION_UNIT:
            if walk.kind in ex.CALLABLE_KINDS:
                in_function = True
                break
            walk = walk.semantic_parent
        template_usr = ex.specialized_template_usr(cursor)
        parent_template = ex.specialized_template_usr(parent) if parent is not None else None
        TARGETS[usr] = {
            "lambda": bool(parent is not None and parent.spelling.startswith("(lambda at")),
            "local": in_function,
            "instantiation": bool(template_usr and template_usr != usr),
            "template_usr": template_usr,
            "in_instantiated_class": bool(parent_template and parent is not None
                                          and parent_template != parent.get_usr()),
            "builtin": cursor.spelling.startswith("__builtin"),
            "file": rel,
            "line": cursor.location.line,
            "kind": k.name,
            "special": special,
            "at_parent_location": same_loc,
            "defaulted": k in ex.CALLABLE_KINDS and cursor.is_default_method(),
            "parent_kind": parent.kind.name if parent is not None else None,
            "name": ("(lambda)" if parent is not None and parent.spelling.startswith("(lambda at")
                     else (parent.spelling if parent is not None else "")) + "::" + cursor.spelling,
        }
    _orig_note_alias(self, cursor, usr)


ex._Extractor.note_alias = _note_alias  # type: ignore[method-assign]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def submodules(repo: Path, rev: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "--blob", f"{rev}:.gitmodules", "--get-regexp",
         r"submodule\..*\.path"],
        capture_output=True, text=True,
    )
    return [line.split(" ", 1)[1] for line in proc.stdout.splitlines()]


def under(path: str, prefixes: list[str]) -> str | None:
    for p in prefixes:
        if path == p or path.startswith(p.rstrip("/") + "/"):
            return p
    return None


def measure(name: str) -> dict[str, object]:
    repo = FIXTURES / name
    base_ref, head_ref, declared = RANGES[name]
    revs = resolve_revs(repo, base_ref=base_ref, head_ref=head_ref)
    toolchain = load()
    third_party = sorted(
        set(declared) | set(submodules(repo, revs.base_sha)) | set(submodules(repo, revs.head_sha))
    )
    report: dict[str, object] = {
        "range": f"{base_ref}..{head_ref}",
        "base": revs.base_sha,
        "head": revs.head_sha,
        "third_party_set": third_party,
    }
    for label, rev in (("base", revs.base_sha), ("head", revs.head_sha)):
        TARGETS.clear()
        tracked = set(git(repo, "ls-tree", "-r", "--name-only", rev).splitlines())
        with checkout(repo, rev) as co:
            partials = list(tu_extracts(co.cpp_entries, root=co.path, toolchain=toolchain))
            model = assemble(rev, partials, toolchain=toolchain)

        def file_label(path: str | None) -> str:
            if path is None:
                return "no location"
            if under(path, third_party):
                return "third-party"
            if path not in tracked:
                return "untracked"
            return "first-party"

        nodes = collections.Counter()
        untracked_dirs = collections.Counter()
        for node in model.nodes.values():
            f = node.location.file if node.location else None
            lab = file_label(f)
            nodes[lab] += 1
            if lab == "untracked" and f is not None:
                untracked_dirs["/".join(f.split("/")[:3])] += 1

        aliases: dict[str, str] = {}
        for tu in partials:
            for k, v in tu.aliases.items():
                aliases.setdefault(k, v)
        dropped: dict[tuple[str, str, str], dict[str, object]] = {}
        unprobed = collections.Counter()
        for tu in partials:
            for e in tu.edges.values():
                if resolve(e.src, model.nodes, aliases) is None:
                    continue
                if resolve(e.dst, model.nodes, aliases) is not None:
                    continue
                target = TARGETS.get(e.dst)
                if target is None:
                    unprobed[e.kind.value] += 1
                    continue
                if target["file"] is None:
                    continue  # outside the repository: already the boundary
                dropped[(e.src, e.dst, e.kind.value)] = {**target, "edge": e.kind.value,
                                                         "label": file_label(target["file"])}
        by_target: dict[str, dict[str, object]] = {}
        for (_, dst, _), info in dropped.items():
            by_target.setdefault(dst, info)
        cats = collections.Counter()
        for dst, info in by_target.items():
            if info["builtin"]:
                cat = "compiler builtin function"
            elif info["lambda"]:
                cat = "lambda member (operator() or conversion)"
            elif info["at_parent_location"] and info["kind"] in ("CONSTRUCTOR", "DESTRUCTOR", "CXX_METHOD"):
                cat = f"implicit special member ({info['special'] or 'other'})"
            elif info["local"]:
                cat = "member of a class local to a function"
            elif info["instantiation"] or info["in_instantiated_class"]:
                t = info["template_usr"]
                cat = ("template instantiation, template is a node"
                       if t and resolve(t, model.nodes, aliases) is not None
                       else "template instantiation, template not a node")
            else:
                cat = f"other: {info['kind']}"
            info["category"] = cat
            cats[(cat, info["label"])] += 1
        report[label] = {
            "nodes": dict(nodes),
            "nodes_total": len(model.nodes),
            "untracked_by_dir": dict(untracked_dirs.most_common()),
            "edges_total": model.completeness.edges_total,
            "edges_dropped": model.completeness.edges_dropped,
            "dropped_edges_into_repo": len(dropped),
            "dropped_edges_into_repo_by_kind": dict(
                collections.Counter(i["edge"] for i in dropped.values())
            ),
            "dropped_edges_target_not_probed_by_kind": dict(unprobed),
            "unresolved_repo_targets": len(by_target),
            "unresolved_repo_targets_by_category": {f"{c} [{l}]": n for (c, l), n in
                                                    sorted(cats.items())},
            "examples_by_category": {
                c: sorted({f"{i['file']}:{i['line']} {i['kind']} {i['name']}"
                           for i in by_target.values() if i["category"] == c})[:6]
                for c in sorted({i["category"] for i in by_target.values()})
            },
        }
    return report


if __name__ == "__main__":
    for name in sys.argv[1:]:
        print(json.dumps({name: measure(name)}, indent=1))
        sys.stdout.flush()
