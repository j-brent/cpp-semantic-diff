"""Reduce the two demo artifacts of run 38044461310 to the numbers the playground pages draw.

    python3 -I extract.py <artifacts-dir> <out.json>    # also writes <out>.js beside it

<artifacts-dir> is what `gh run download 38044461310` produces: demo-fmt/ and demo-spdlog/,
each holding diff.json, plan.txt and doctor.txt. Every number on the pages comes from here;
nothing is typed in by hand except the job step durations, which only the Actions API knows.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

RUN = {
    "id": 38044461310,
    "url": "https://github.com/j-brent/cpp-semantic-diff/actions/runs/38044461310",
    "commit": "4d8e367",
    # `gh run view --json jobs`: the Analyze step runs doctor, plan, a cold diff and a warm diff.
    "analyze_step_s": {"fmt": 192, "spdlog": 28},
}
TARGETS = {"fmt": "HEAD~12", "spdlog": "HEAD~30"}
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


def target(root: Path, name: str) -> dict:
    d = json.loads((root / f"demo-{name}" / "diff.json").read_text())
    ent, scores = d["entities"], d["scores"]
    events = {e["id"]: e for e in d["events"]}
    children = Counter(e["caused_by"] for e in d["events"] if e["caused_by"])

    def label(subject: str) -> tuple[str, str]:
        if "|" in subject:
            a, rel, b = subject.split("|", 2)
            ea, eb = ent.get(a), ent.get(b)
            na = short(ea["qualified_name"]) if ea else "?"
            nb = short(eb["qualified_name"]) if eb else "?"
            return f"{na} → {nb}", rel
        e = ent.get(subject)
        return (short(e["qualified_name"]) + " " + params(e.get("signature")), "") if e else ("?", "")

    top = []
    for rank, eid in enumerate(d["ranking"]):
        e = events[eid]
        if e["caused_by"]:
            continue
        s = scores[eid]
        lbl, rel = label(e["subject"])
        loc = next((p["location"] for p in e["provenance"] if p.get("location")), None)
        top.append({
            "rank": rank + 1,
            "kind": e["kind"],
            "label": lbl,
            "relation": rel,
            "where": f'{loc["file"]}:{loc["line"]}' if loc else None,
            "total": round(s["total"], 4),
            "impact": round(s["impact"], 4),
            "high_risk": s["impact"] >= 0.6,
            "component": s["component"],
            "nested": children.get(eid, 0),
            "factors": {f["name"]: {"c": round(f["contribution"], 4), "v": round(f["value"], 4), "detail": f["detail"]} for f in s["factors"]},
        })

    comp = d["completeness"]
    return {
        "name": name,
        "base_ref": TARGETS[name],
        "revs": [d["meta"]["base_rev"][:12], d["meta"]["head_rev"][:12]],
        "plan": plan_numbers((root / f"demo-{name}" / "plan.txt").read_text()),
        "doctor": (root / f"demo-{name}" / "doctor.txt").read_text().strip().splitlines(),
        "summary": d["summary"],
        "timings": d["meta"]["timings"],
        "kinds": dict(Counter(e["kind"] for e in d["events"]).most_common()),
        "completeness": {side: {k: comp[side][k] for k in ("tus_total", "tus_parsed", "tus_failed", "tus_cached", "edges_total", "edges_dropped", "tus_complete", "notes")} for side in ("base", "head")},
        "findings": len(d["findings"]),
        "top_level": top,
    }


def main() -> None:
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    doc = {"run": RUN, "targets": [target(root, n) for n in TARGETS]}
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    out.write_text(text)
    # The same document as a script, so the pages also open straight from disk (file:// blocks fetch).
    out.with_suffix(".js").write_text(f"window.RUN_DATA = {text};\n")


if __name__ == "__main__":
    main()
