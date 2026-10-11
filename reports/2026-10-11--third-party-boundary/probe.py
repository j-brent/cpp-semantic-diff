"""Build both models as `cppsd diff` does and report what PLAN-004 §5 checks inside them.

The JSON document lists only the entities change events reference, so "no node under the
third-party path" cannot be read from it. This builds the two models the same way `cppsd diff`
does (third-party set, tracked files, cache on) and prints, per side: the node count, nodes
under each third-party path, crossings per label, nodes per exposure class, and the unmatched
entities grouped by their first two path components.

Usage, from the repository root, with fixture clones at ~/fixtures/<name>:
  uv run python reports/2026-10-11--third-party-boundary/probe.py NAME BASE HEAD [THIRD_PARTY ...]
"""
from __future__ import annotations

import collections
import json
import sys
from contextlib import ExitStack
from pathlib import Path

from cpp_semantic_diff.acquire import file_changes, resolve_revs, third_party_set
from cpp_semantic_diff.acquire.workspace import checkout
from cpp_semantic_diff.acquire.git import tracked_files
from cpp_semantic_diff.match import match_models
from cpp_semantic_diff.produce import build_model, load
from cpp_semantic_diff.rank.exposure import exposure_map


def main() -> None:
    name, base, head, *declared = sys.argv[1:]
    repo = Path.home() / "fixtures" / name
    toolchain = load()
    revs = resolve_revs(repo, base_ref=base, head_ref=head)
    tp = third_party_set(repo, revs, declared)
    tp_paths = sorted(tp.paths)
    changes = file_changes(repo, revs)
    out: dict[str, object] = {"revs": revs.describe(), "third_party": tp_paths}
    models = {}
    with ExitStack() as stack:
        for label, sha in (("base", revs.base_sha), ("head", revs.head_sha)):
            co = stack.enter_context(checkout(repo, sha))
            models[label] = build_model(
                co, toolchain=toolchain, cache=True, workers=None, third_party=tp,
                tracked=tracked_files(repo, co.rev),
            )
    for label, m in models.items():
        under = {p: 0 for p in tp_paths}
        for node in m.nodes.values():
            f = node.location.file if node.location else ""
            for p in tp_paths:
                if f == p or f.startswith(p.rstrip("/") + "/"):
                    under[p] += 1
        out[label] = {
            "nodes": len(m.nodes),
            "nodes_under_third_party": under,
            "crossings_by_label": dict(m.completeness.crossings_by_label),
            "exposure": dict(collections.Counter(str(v) for v in exposure_map(m).values())),
        }
    corr = match_models(models["base"], models["head"], renames=changes.renames,
                        missed_rename_files=changes.missed_rename_files)
    for side, attr in (("base", "unmatched_base"), ("head", "unmatched_head")):
        ids = getattr(corr, attr, None)
        if ids is None:
            continue
        m = models[side]
        groups: collections.Counter[str] = collections.Counter()
        for i in ids:
            n = m.nodes.get(i)
            f = n.location.file if n is not None and n.location else "?"
            groups["/".join(f.split("/")[:2])] += 1
        out[side]["unmatched"] = len(ids)
        out[side]["unmatched_by_dir"] = dict(groups.most_common())
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
