"""Write product/A-fmt/base.json and head.json: fmt's component 0 in run 38280732734, by hand.

    cd product/A-fmt && python3 -I ../../scripts/gen_fmt_component0.py .

Read off component 0 of demo-fmt/diff.json: the nodes are the subjects and endpoints of its 15
change events, and the edges in both drawings are the ones no change event touches (they map
through the correspondence). Same node ids as run B's drawing
(reports/2026-10-10--up-to-speed/demo-run-b/scripts/gen_fmt_component0.py), so the two read
side by side. Then: `archify compare architecture base.json head.json fmt-write-buffer.html`.
"""

import json
import sys

out = sys.argv[1]
legend = {"entries": {"frontend": {"label": "caller (library entry point)"}, "external": {"label": "caller (test)"},
                      "backend": {"label": "function the change is about"}, "cloud": {"label": "callee"}}}


def C(i, t, l, s, x, y, w=210, h=58, tag=None):
    return dict({"id": i, "type": t, "label": l, "sublabel": s, "pos": [x, y], "size": [w, h]}, **({"tag": tag} if tag else {}))


common = [
    C("print", "frontend", "fmt::print", "(std::ostream&, fstring, T&&...)", 40, 40),
    C("vprint_c", "frontend", "fmt::vprint", "(std::ostream&, string_view, args)", 40, 130),
    C("vprint_w", "frontend", "fmt::vprint (wchar_t)", "(std::wostream&, wstring_view, args)", 40, 220),
    C("t_write", "external", "test: write_to_ostream", "ostream-test.cc:127", 40, 310),
    C("max_value", "cloud", "detail::max_value", "T ()", 800, 150, 190),
    C("to_unsigned", "cloud", "detail::to_unsigned", "make_unsigned<T> (T)", 800, 240, 190),
    C("buf_ctor", "cloud", "detail::buffer::buffer", "(grow_fn, T*, size, capacity)", 800, 370, 190),
    C("mem_buf", "cloud", "basic_memory_buffer", "ctor (const Allocator&)", 800, 460, 190),
    C("sv_ctor", "cloud", "basic_string_view", "ctor (const Char*)", 800, 550, 190),
    C("buf_data", "cloud", "detail::buffer::data", "T* ()", 800, 640, 190),
]
base_nodes = common + [
    C("t_max", "external", "test: write_to_ostream_max_size", "ostream-test.cc", 40, 410, 230, 80),
    C("wb", "backend", "detail::write_buffer", "(ostream&, buffer&) · ostream.h:55", 420, 150, 250, 130),
]
head_nodes = common + [
    C("t_max", "external", "test: write_to_ostream_max_size", "ostream-test.cc:131 · body changed", 40, 410, 230, 80),
    C("wb", "backend", "detail::write_buffer", "(ostream&, buffer&, unsigned long = …) · ostream.h:56", 420, 150, 250, 130,
      "NodeSignatureChanged · risk 0.74"),
]
callers = ["print", "vprint_c", "vprint_w", "t_write", "t_max"]
calls = [{"id": f"{c}-wb", "from": c, "to": "wb"} for c in callers]
# The head edge from each caller carries its CallerOfChangedSignature change event.
head_calls = [dict(e, label="caller edited" if e["from"] == "t_max" else "caller not edited") for e in calls]
callees = [{"id": f"wb-{t}", "from": "wb", "to": t} for t in ["max_value", "to_unsigned"]]
base_conn = calls + callees + [{"id": f"t_max-{t}", "from": "t_max", "to": t} for t in ["max_value", "to_unsigned", "buf_ctor"]]
head_conn = head_calls + callees + [{"id": f"t_max-{t}", "from": "t_max", "to": t} for t in ["mem_buf", "sv_ctor", "buf_data"]]
cards = [
    {"dot": "rose", "title": "What the tool reported", "items": [
        "15 change events in component 0, 10 of them top-level",
        "write_buffer: NodeSignatureChanged (0.74), NodeAttrChanged(default_arguments) (0.71), NodeBodyChanged (0.57), all high-risk",
        "5 CallerOfChangedSignature nested under the signature change; one caller (the max_size test) was edited",
        "The max_size test's body changed: 3 calls removed, 3 added"]},
    {"dot": "amber", "title": "What changed since run 38044461310", "items": [
        "Then: write_buffer removed plus write_buffer added, 8 consequences under each, 28 events in the component",
        "Now: the name tier pairs the two (ADR-036), so the callers' edges map and no edge event is reported for them",
        "fill_n is no longer in this component; it is component 1"]},
    {"dot": "slate", "title": "Drawn differently from the document", "items": [
        "The 'contains' edge from namespace detail is left out; it is not a change event in this run",
        "Edges drawn in both drawings are inferred: no change event touches them, so they map through the correspondence"]},
]


def doc(title, outp, comps, conns):
    return {"schema_version": 1, "diagram_type": "architecture",
            "meta": {"title": title, "output": outp, "quality_profile": "showcase", "legend": legend},
            "components": comps, "connections": conns, "cards": cards}


json.dump(doc("fmt · component 0 · base (ea0660a1d095)", "base.html", base_nodes, base_conn), open(f"{out}/base.json", "w"), indent=1)
json.dump(doc("fmt · component 0 · head (b4ffa39b9994)", "head.html", head_nodes, head_conn), open(f"{out}/head.json", "w"), indent=1)
