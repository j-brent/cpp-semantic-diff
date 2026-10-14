"""Write product/A-spdlog/base.json and head.json: spdlog's component 1 in run 38280732734, by hand.

    cd product/A-spdlog && python3 -I ../../scripts/gen_spdlog_component1.py .

Component 1, not 0: it holds `basic_logger_mt`, the first of the file-logger factories run B
drew as component 0. This run's component 0 is a set of spelling-only and sink body changes;
page A says so. Read off component 1 of demo-spdlog/diff.json (8 change events). The callee
edge to `file_event_handlers` is drawn in both because run B's document has it on both sides and
this run reports no change event on it. Then:
`archify compare architecture base.json head.json spdlog-basic-logger.html`.
"""

import json
import sys

out = sys.argv[1]
legend = {"entries": {"external": {"label": "caller (example)"}, "backend": {"label": "function the change is about"},
                      "cloud": {"label": "callee"}, "frontend": {"label": "same component, no call drawn"}}}


def C(i, t, l, s, x, y, w=230, h=58, tag=None):
    return dict({"id": i, "type": t, "label": l, "sublabel": s, "pos": [x, y], "size": [w, h]}, **({"tag": tag} if tag else {}))


common = [
    C("ex_basic", "external", "basic_example", "example.cpp:121", 40, 60),
    C("ex_async", "external", "async_example", "example.cpp:165", 40, 160),
    C("ex_replace", "external", "replace_default_logger_example", "example.cpp:374", 40, 260, 250),
    C("feh", "cloud", "file_event_handlers", "ctor", 920, 160, 210),
]
base_nodes = common + [
    C("blmt", "backend", "spdlog::basic_logger_mt · basic_file_sink.h", "(string&, string&, bool = false, handlers& = {}) · :45",
      420, 120, 380, 130),
    C("sdl", "frontend", "spdlog::set_default_logger", "void (std::shared_ptr<spdlog::logger>)", 420, 360, 280, 64),
    C("dl", "frontend", "spdlog::default_logger", "std::shared_ptr<spdlog::logger> ()", 420, 460, 280, 64),
]
head_nodes = common + [
    C("blmt", "backend", "spdlog::basic_logger_mt · basic_file_sink.h",
      "(string&, string&, bool = false, handlers& = {}, unsigned long = 0) · :48", 420, 120, 380, 130,
      "NodeSignatureChanged · risk 0.67"),
    C("sdl", "frontend", "spdlog::set_default_logger", "void (std::shared_ptr<logger>) · spelling only", 420, 360, 280, 64),
    C("dl", "frontend", "spdlog::default_logger", "std::shared_ptr<logger> () · spelling only", 420, 460, 280, 64),
]
callers = ["ex_basic", "ex_async", "ex_replace"]
base_conn = [{"id": f"{c}-blmt", "from": c, "to": "blmt"} for c in callers] + [{"id": "blmt-feh", "from": "blmt", "to": "feh"}]
head_conn = [{"id": f"{c}-blmt", "from": c, "to": "blmt", "label": "caller not edited"} for c in callers] + [{"id": "blmt-feh", "from": "blmt", "to": "feh"}]
cards = [
    {"dot": "rose", "title": "What the tool reported", "items": [
        "8 change events in component 1, 5 of them top-level",
        "basic_logger_mt: NodeSignatureChanged (0.67) and NodeAttrChanged(default_arguments) (0.64), both high-risk; NodeBodyChanged (0.50)",
        "3 CallerOfChangedSignature nested under it, none of the callers edited",
        "set_default_logger and default_logger: NodeAttrChanged(sugared_signature), the spelling of logger only, not high-risk (ADR-046)"]},
    {"dot": "amber", "title": "What changed since run 38044461310", "items": [
        "Then: the 8 file-logger factories were 8 removals plus 8 additions in one 62-event component",
        "Now: each factory is one NodeSignatureChanged, paired by the name tier, and the factories fall into components 1 to 9",
        "The sink constructors they wrap are in component 2, beside the buffer_size methods"]},
    {"dot": "slate", "title": "Drawn differently from the document", "items": [
        "The document does not say which edge puts set_default_logger and default_logger in this component, so none is drawn",
        "The file_event_handlers edge is in run B's document on both sides; this run reports no change on it"]},
]


def doc(title, outp, comps, conns):
    return {"schema_version": 1, "diagram_type": "architecture",
            "meta": {"title": title, "output": outp, "quality_profile": "showcase", "legend": legend},
            "components": comps, "connections": conns, "cards": cards}


json.dump(doc("spdlog · component 1 · base (a2976707813e)", "base.html", base_nodes, base_conn), open(f"{out}/base.json", "w"), indent=1)
json.dump(doc("spdlog · component 1 · head (6c9e898175c6)", "head.html", head_nodes, head_conn), open(f"{out}/head.json", "w"), indent=1)
