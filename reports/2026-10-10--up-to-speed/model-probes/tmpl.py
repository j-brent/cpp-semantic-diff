"""Which call edges the extractor emits from templates, function pointers, virtual calls and
initializers: the-model.md §4.3.

Run from the repository root: uv run python reports/2026-10-10--up-to-speed/model-probes/tmpl.py
"""
import tempfile, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.produce.extract import extract
from cpp_semantic_diff.produce.toolchain import load
tc=load()
for src in ["void h(int); void k(); template<class U> void f(U u) { h(u); k(); }",
            "struct W { void go(); }; template<class U> void f(U u) { u.go(); }\nvoid use(){ f(W{}); }",
            "void cb(); void reg(void(*)()); void s() { reg(cb); }",
            "struct B{ virtual void d(); }; struct D:B{ void d() override; }; void call(B& b){ b.d(); }",
            "int g(); int x = g(); struct S { int y = g(); }; void f(int z = g());"]:
    d=Path(tempfile.mkdtemp()); (d/"a.cpp").write_text(src)
    r=extract(CompileEntry(file=d/"a.cpp",directory=d,arguments=["clang++","-std=c++20","a.cpp"]),root=d,toolchain=tc)
    print(src.replace("\n"," | "));
    for e in r.edges.values():
        if e.kind.value!="contains": print("   ", e.kind.value, e.src, "->", e.dst, e.attrs)
