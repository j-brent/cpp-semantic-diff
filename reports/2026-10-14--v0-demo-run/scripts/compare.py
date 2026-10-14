"""Set the change events of run 38280732734 beside those of demo run B (38044461310).

    python3 -I compare.py <new-artifacts-dir> <old-artifacts-dir> <out.json>    # also writes <out>.js

Both directories are what `gh run download <run>` produces. A change event is keyed by its kind
and its subject (USRs, or `src|relation|dst` for an edge), with the attribute for a
`NodeAttrChanged`; the subject's file and every endpoint's file come from `entities`. The key
is the same in schema 1.2 and 2.0 for the kinds both have. It is not identity across runs: the
two runs compare ranges one commit apart at each end, so an event can appear or disappear
because upstream changed, as well as because the tool did. `classify` sorts each difference
into the bucket its subject points at, and the buckets are named for what they test, not for
a cause; the report says which cause each one is evidence of.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

THIRD_PARTY = {"fmt": "test/gtest/", "spdlog": "include/spdlog/fmt/bundled/"}


def load(folder: Path) -> dict:
    d = json.loads((folder / "diff.json").read_text())
    d["_events"] = d.get("change_events", d.get("events"))
    return d


def ids(subject: str) -> list[str]:
    if "|" in subject:
        a, _, b = subject.split("|", 2)
        return [a, b]
    return [subject]


def key(e: dict) -> str:
    attr = (e.get("payload") or {}).get("attr", "")
    return f'{e["kind"]}|{e["subject"]}|{attr}'


def files(d: dict, e: dict) -> list[str]:
    out = []
    for i in ids(e["subject"]):
        ent = d["entities"].get(i)
        if ent and ent.get("location"):
            out.append(ent["location"]["file"])
    return out


def name(d: dict, e: dict) -> str:
    parts = []
    for i in ids(e["subject"]):
        ent = d["entities"].get(i)
        parts.append(ent["qualified_name"] if ent else i)
    return " -> ".join(parts)


#: Entity kinds run B's model did not have (PR #54, ADR-038).
NEW_KINDS = {"variable", "alias", "macro", "enum", "enumerator", "static_assert"}


def classify(repo: str, d: dict, e: dict, split: set[str]) -> str:
    """`split`: names that one run reports as a NodeRemoved plus a NodeAdded, and the other not."""
    fs = files(d, e)
    names = name(d, e).split(" -> ")
    kinds = {(d["entities"].get(i) or {}).get("kind") for i in ids(e["subject"])}
    if any(f.startswith(THIRD_PARTY[repo]) for f in fs) or any(n.startswith("testing::") or "@N@testing@" in n for n in names):
        return "an endpoint is third-party (path, or googletest's namespace testing)"
    if any(n in split for n in names):
        return "about an entity one run splits into removal + addition"
    if kinds & NEW_KINDS:
        return "about an entity of a kind run B did not model"
    if e["kind"] == "NodeAttrChanged":
        return f'NodeAttrChanged({(e.get("payload") or {}).get("attr")})'
    return "other"


def target(new_root: Path, old_root: Path, repo: str) -> dict:
    new, old = load(new_root / f"demo-{repo}"), load(old_root / f"demo-{repo}")
    nk = {key(e): e for e in new["_events"]}
    ok = {key(e): e for e in old["_events"]}
    def split_names(d: dict) -> set[str]:
        removed = {name(d, e) for e in d["_events"] if e["kind"] == "NodeRemoved"}
        added = {name(d, e) for e in d["_events"] if e["kind"] == "NodeAdded"}
        return removed & added

    split = split_names(old) ^ split_names(new)

    only_old = [ok[k] for k in ok.keys() - nk.keys()]
    only_new = [nk[k] for k in nk.keys() - ok.keys()]

    def table(events: list[dict], d: dict) -> dict:
        buckets: dict[str, Counter] = defaultdict(Counter)
        for e in events:
            buckets[classify(repo, d, e, split)][e["kind"]] += 1
        return {b: dict(c.most_common()) for b, c in sorted(buckets.items())}

    def sample(events: list[dict], d: dict, n: int = 400) -> list[str]:
        return sorted(f'{e["kind"]}  {name(d, e)}  [{(e.get("payload") or {}).get("attr", "")}]' for e in events)[:n]

    return {
        "repo": repo,
        "old_total": len(old["_events"]),
        "new_total": len(new["_events"]),
        "in_both": len(nk.keys() & ok.keys()),
        "only_old": {"count": len(only_old), "by_bucket": table(only_old, old), "events": sample(only_old, old)},
        "only_new": {"count": len(only_new), "by_bucket": table(only_new, new), "events": sample(only_new, new)},
    }


def main() -> None:
    new_root, old_root, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    doc = {"new_run": 38280732734, "old_run": 38044461310, "targets": [target(new_root, old_root, r) for r in THIRD_PARTY]}
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    out.write_text(text)
    # As a script too, so page F opens straight from disk (file:// blocks fetch).
    out.with_suffix(".js").write_text(f"window.COMPARE = {text};\n")


if __name__ == "__main__":
    main()
