"""Write process/B-pipeline/pipeline.json, the archify spec of the pipeline with its timings.

    python3 -I gen_pipeline.py <run-38280732734.json> <out-dir>

Every number comes from `meta.timings` and `completeness` of each target in the reduced data.
Layout and wording are by hand. Then: `archify finalize architecture pipeline.json pipeline.html`.
"""

import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
out = Path(sys.argv[2])
T = {t["name"]: t for t in data["targets"]}
f, s = T["fmt"], T["spdlog"]


def secs(v: float) -> str:
    return f"{v:.1f} s" if v >= 0.95 else f"{v:.2f} s" if v >= 0.005 else f"{v:.3f} s"


def both(stage: str) -> str:
    return f'fmt {secs(f["timings"][stage])} · spdlog {secs(s["timings"][stage])}'


def comp(i, t, label, sub, x, y, tag=None, src=None, w=200, h=72):
    c = {"id": i, "type": t, "label": label, "sublabel": sub, "pos": [x, y], "size": [w, h]}
    if tag:
        c["tag"] = tag
    if src:
        c["sources"] = [{"path": f"src/cpp_semantic_diff/{src}"}]
    return c


def tus(t):
    return t["completeness"]["base"]["tus_total"]


def share(t):
    tm = t["timings"]
    p = tm["produce_base"] + tm["produce_head"]
    return p, tm["total"], 100 * p / tm["total"]


components = [
    comp("acquire", "external", "acquire", both("acquire"), 40, 330, "worktrees + compile db", "acquire/workspace.py"),
    comp("prod_base", "backend", "produce · base", both("produce_base"), 340, 170, f"{tus(f)} TUs · {tus(s)} TUs", "produce/build.py"),
    comp("cache", "database", "model cache", "empty root, so a cold run", 340, 330, "per-TU", "produce/cache.py"),
    comp("prod_head", "backend", "produce · head", both("produce_head"), 340, 490, f"{tus(f)} TUs · {tus(s)} TUs", "produce/build.py"),
    comp("match", "backend", "match", both("match"), 640, 170, "declared · inline-ns · name", "match/matcher.py"),
    comp("diff", "backend", "diff", both("diff"), 640, 330, "change events", "diff/differ.py"),
    comp("derive", "backend", "derive", both("derive"), 640, 490, "callers of a changed signature", "derive/deriver.py"),
    comp("classify", "backend", "classify", both("classify"), 640, 650, "cause → cascade", "classify/classifier.py"),
    comp("enrich", "external", "enrich", "not built: post-v0 (§13)", 940, 170, "findings = []"),
    comp("rank", "backend", "rank", both("rank"), 940, 330, "+ blast radius", "rank/ranker.py"),
    comp("coverage", "backend", "coverage", both("coverage"), 940, 650, "tokens on no event's lines", "diff/coverage.py"),
    comp("json", "database", "diff.json", f'schema {f["schema"]} · the product', 1240, 490, "artifact", "render/document.py"),
    comp("term", "frontend", "terminal view", "pasted into the run summary", 1240, 650, "--limit 25", "render/terminal.py"),
]
C = lambda i, a, b, label, **kw: {"id": i, "from": a, "to": b, "label": label, **kw}
connections = [
    C("a-pb", "acquire", "prod_base", "base worktree", variant="emphasis"),
    C("a-ph", "acquire", "prod_head", "head worktree", variant="emphasis"),
    C("pb-c", "prod_base", "cache", "per-TU", variant="dashed", fromSide="bottom", toSide="top"),
    C("ph-c", "prod_head", "cache", "per-TU", variant="dashed", fromSide="top", toSide="bottom"),
    C("pb-m", "prod_base", "match", "base Model", variant="emphasis"),
    C("ph-m", "prod_head", "match", "head Model", variant="emphasis"),
    C("m-d", "match", "diff", "correspondence", fromSide="bottom", toSide="top"),
    C("d-v", "diff", "derive", "change events", fromSide="bottom", toSide="top"),
    C("v-c", "derive", "classify", "+ derived", fromSide="bottom", toSide="top"),
    C("c-r", "classify", "rank", "caused_by links"),
    C("c-cov", "classify", "coverage", "final events"),
    C("e-r", "enrich", "rank", "evidence (none)", variant="dashed", fromSide="bottom", toSide="top"),
    C("r-j", "rank", "json", "scored, ordered"),
    C("cov-j", "coverage", "json", "uncovered lines"),
    C("j-t", "json", "term", "rendered from", fromSide="bottom", toSide="top"),
]
fp, ft, fpct = share(f)
sp, st, spct = share(s)
after = lambda t: t["timings"]["total"] - t["timings"]["produce_base"] - t["timings"]["produce_head"] - t["timings"]["acquire"]
cards = [
    {"dot": "rose", "title": "Where the time goes", "items": [
        f"fmt: produce is {fp:.1f} of {ft:.1f} s ({fpct:.0f}%)",
        f"spdlog: produce is {sp:.1f} of {st:.1f} s ({spct:.0f}%)",
        f'Everything after produce: {after(f):.2f} s on fmt, {after(s):.2f} s on spdlog; coverage is the largest part of it']},
    {"dot": "amber", "title": "New since run 38044461310", "items": [
        "derive: CallerOfChangedSignature change events (ADR-040)",
        "coverage: changed lines no change event explains (ADR-042)",
        "match runs three tiers, not one (ADR-036, ADR-044)",
        "third-party and untracked files are left out of produce (#48, ADR-037)"]},
    {"dot": "slate", "title": "Read with care", "items": [
        "Timings are meta.timings: in-process, cold, GitHub ubuntu-24.04 runner",
        "No render timing is recorded",
        "enrich is still not built; blast radius lives in rank"]},
]
doc = {
    "schema_version": 1,
    "diagram_type": "architecture",
    "meta": {
        "title": f'cppsd diff · pipeline and where the time went (run {data["run"]["id"]})',
        "output": "pipeline.html",
        "quality_profile": "showcase",
        "repository": {"url": "https://github.com/j-brent/cpp-semantic-diff", "provider": "github",
                       "revision": "c01c58bfe348636daca703316194b6e98214fc03"},
        "legend": {"entries": {"external": {"label": "boundary stage (git in / not built)"}, "backend": {"label": "pipeline stage"},
                               "database": {"label": "store / document"}, "frontend": {"label": "view"}}},
    },
    "components": components,
    "connections": connections,
    "cards": cards,
}
out.mkdir(parents=True, exist_ok=True)
(out / "pipeline.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False))
