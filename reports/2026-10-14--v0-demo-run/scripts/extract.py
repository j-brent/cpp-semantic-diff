"""Reduce the two demo artifacts of run 38280732734 to the numbers the pages draw.

    python3 -I extract.py <artifacts-dir> <previous.json> <out.json>    # also writes <out>.js

<artifacts-dir> is what `gh run download 38280732734` produces: demo-fmt/ and demo-spdlog/,
each holding diff.json (schema 2.0), diff.txt, diff-warm.txt, plan.txt and doctor.txt.
<previous.json> is demo run B's reduced data
(reports/2026-10-10--up-to-speed/demo-run-b/data/run-38044461310.json, schema 1.2), carried
along so the comparison page reads both runs from one file. Every number comes from those
files except the job step durations and the upstream commit counts, which only the GitHub API
knows; they are typed in below with where they came from.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

RUN = {
    "id": 38280732734,
    "url": "https://github.com/j-brent/cpp-semantic-diff/actions/runs/38280732734",
    "event": "pull_request",
    "pr": 75,
    "branch": "dev",
    "commit": "c01c58b",
    "created": "2026-10-12T16:55:15Z",
    # `gh run view --json jobs`: the Analyze step runs doctor, plan, a cold diff and a warm diff.
    "analyze_step_s": {"fmt": 217, "spdlog": 36},
}
PREVIOUS_RUN = {
    "id": 38044461310,
    "url": "https://github.com/j-brent/cpp-semantic-diff/actions/runs/38044461310",
    "commit": "4d8e367",
    # Copied from run B's own extract.py (the Actions API's Analyze step durations).
    "analyze_step_s": {"fmt": 192, "spdlog": 28},
}
TARGETS = {"fmt": "HEAD~12", "spdlog": "HEAD~30"}
THIRD_PARTY = {"fmt": "test/gtest", "spdlog": "include/spdlog/fmt/bundled"}
# `gh api repos/<owner>/<repo>/compare/<old>...<new>`, run 2026-10-12: how far each end of the
# range moved upstream between run B and this run. Each moved by one commit.
RANGE_SHIFT = {
    "fmt": {"base": ["cdc27f91a007", "ea0660a1d095", 1], "head": ["35c58f084e7c", "b4ffa39b9994", 1]},
    "spdlog": {"base": ["bfde7d3711d5", "a2976707813e", 1], "head": ["d6c93f38a08b", "6c9e898175c6", 1]},
}
NS = re.compile(r"\b(fmt::v12::|fmt::|spdlog::)")
TPARAM = re.compile(r"type-parameter-0-(\d)")


def short(name: str) -> str:
    return TPARAM.sub(lambda m: "T" if m.group(1) == "0" else f"T{m.group(1)}", NS.sub("", name))


def params(sig: str | None) -> str:
    """`void (basic_ostream<type-parameter-0-0, ...> &, unsigned long)` -> `(basic_ostream&, unsigned long)`."""
    if not sig or "(" not in sig:
        return ""
    inner = sig[sig.index("(") + 1 : sig.rindex(")")]
    depth, cur, out = 0, "", []
    for ch in inner:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        if depth == 0 and ch not in "<>":
            cur += ch
    if cur.strip():
        out.append(cur)
    out = [re.sub(r"\s+", " ", p.replace("const ", "").replace("std::", "")).strip().replace(" &", "&").replace(" *", "*") for p in out]
    return short("(" + ", ".join(out) + ")")


def plan_numbers(text: str) -> dict:
    get = lambda key: int(re.search(rf"^{key}\s+([\d,]+)", text, re.M).group(1).replace(",", ""))
    keys = {"files_changed": "files changed", "cpp_files": "C\\+\\+ files", "files_renamed": "files renamed", "lines_changed": "lines changed"}
    return {k: get(v) for k, v in keys.items()}


def in_process_seconds(text: str) -> float:
    """The last field of the terminal view's second compression line, e.g. `· 195.8s`."""
    return float(re.search(r"lines per change event .*· ([\d.]+)s$", text, re.M).group(1))


def target(root: Path, name: str) -> dict:
    folder = root / f"demo-{name}"
    d = json.loads((folder / "diff.json").read_text())
    ent, scores = d["entities"], d["scores"]
    events = {e["id"]: e for e in d["change_events"]}
    children = Counter(e["caused_by"] for e in d["change_events"] if e["caused_by"])

    def label(subject: str) -> tuple[str, str]:
        if "|" in subject:
            a, rel, b = subject.split("|", 2)
            ea, eb = ent.get(a), ent.get(b)
            na = short(ea["qualified_name"]) if ea else "?"
            nb = short(eb["qualified_name"]) if eb else "?"
            return f"{na} → {nb}", rel
        e = ent.get(subject)
        return (short(e["qualified_name"]) + " " + params(e.get("signature")), "") if e else ("?", "")

    def detail(e: dict) -> str:
        """What changed, beyond the kind: the attribute, or the caller's edit state."""
        p = e.get("payload") or {}
        if e["kind"] == "NodeAttrChanged":
            return p.get("attr") or p.get("attribute") or ""
        if e["kind"] == "CallerOfChangedSignature":
            return "caller edited" if p.get("caller_edited") else "caller not edited"
        return ""

    top = []
    for rank, eid in enumerate(d["ranking"]):
        e = events[eid]
        if e["caused_by"]:
            continue
        s = scores[eid]
        lbl, rel = label(e["subject"])
        loc = next((p["location"] for p in e["provenance"] if p.get("location")), None)
        conf = e.get("confidence") or {}
        top.append({
            "rank": rank + 1,
            "kind": e["kind"],
            "detail": detail(e),
            "label": lbl,
            "relation": rel,
            "where": f'{loc["file"]}:{loc["line"]}' if loc else None,
            "total": round(s["total"], 4),
            "impact": round(s["impact"], 4),
            "informational": s.get("informational", False),
            # ADR-046: impact at or over 0.6, unless the change is one of spelling only.
            "high_risk": s["impact"] >= 0.6 and not s.get("informational", False),
            "component": s["component"],
            "nested": children.get(eid, 0),
            "tier": conf.get("tier"),
            "unpaired": conf.get("unpaired"),
            "factors": {f["name"]: {"c": round(f["contribution"], 4), "v": round(f["value"], 4), "detail": f["detail"]} for f in s["factors"]},
        })

    high = [eid for eid, s in scores.items() if s["impact"] >= 0.6 and not s.get("informational", False)]
    gated = [eid for eid, s in scores.items() if s["impact"] >= 0.6 and s.get("informational", False)]
    attr_kinds = Counter(detail(e) for e in d["change_events"] if e["kind"] == "NodeAttrChanged")
    tiers = Counter(str((e.get("confidence") or {}).get("tier")) for e in d["change_events"])

    comp = d["completeness"]
    keep = ("tus_total", "tus_parsed", "tus_failed", "tus_cached", "edges_total", "edges_dropped", "tus_complete",
            "crossings_by_label", "unmodelled", "language_modes", "notes")
    unc = comp["uncovered"]
    files = sorted(unc["files"], key=lambda f: -f["tokens"])
    diff_txt = (folder / "diff.txt").read_text()
    warm_txt = (folder / "diff-warm.txt").read_text()
    return {
        "name": name,
        "base_ref": TARGETS[name],
        "third_party_flag": THIRD_PARTY[name],
        "revs": [d["meta"]["base_rev"][:12], d["meta"]["head_rev"][:12]],
        "range_shift": RANGE_SHIFT[name],
        "schema": d["meta"]["schema_version"],
        "plan": plan_numbers((folder / "plan.txt").read_text()),
        "doctor": (folder / "doctor.txt").read_text().strip().splitlines(),
        "summary": d["summary"],
        "timings": d["meta"]["timings"],
        "in_process_s": {"cold": in_process_seconds(diff_txt), "warm": in_process_seconds(warm_txt)},
        "kinds": dict(Counter(e["kind"] for e in d["change_events"]).most_common()),
        "attr_kinds": dict(attr_kinds.most_common()),
        "tiers": dict(tiers.most_common()),
        "high_risk_by_kind": dict(Counter(events[i]["kind"] for i in high).most_common()),
        "spelling_only_gated": len(gated),
        "entity_kinds": dict(Counter(v.get("kind") for v in ent.values()).most_common()),
        "third_party": d["third_party"],
        "third_party_hints": d["third_party_hints"],
        "completeness": {side: {k: comp[side][k] for k in keep} for side in ("base", "head")},
        "renames": comp["renames"],
        "limits": comp["limits"],
        "uncovered": {
            "changed_cpp_lines": d["summary"]["changed_cpp_lines"],
            "lines": d["summary"]["uncovered_lines"],
            "tokens": d["summary"]["uncovered_tokens"],
            "moved_tokens": unc["moved_tokens"],
            "lines_in_unmodelled_files": unc["lines_in_unmodelled_files"],
            "excluded_files": unc["excluded_files"],
            "files_total": len(files),
            "files_unmodelled": sum(1 for f in files if not f["modelled"]),
            "top_files": [{k: f[k] for k in ("side", "path", "tokens", "modelled")} | {"lines": len(f["lines"])} for f in files[:12]],
        },
        "findings": len(d["findings"]),
        "top_level": top,
    }


def main() -> None:
    root, previous, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    prev = json.loads(previous.read_text())
    doc = {
        "run": RUN,
        "targets": [target(root, n) for n in TARGETS],
        "previous": {"run": PREVIOUS_RUN, "targets": prev["targets"]},
    }
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    out.write_text(text)
    # The same document as a script, so the pages also open straight from disk (file:// blocks fetch).
    out.with_suffix(".js").write_text(f"window.RUN_DATA = {text};\n")


if __name__ == "__main__":
    main()
