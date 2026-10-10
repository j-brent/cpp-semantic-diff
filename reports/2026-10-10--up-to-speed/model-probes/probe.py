"""The 22 before/after changes in the-model.md §4.1 and §4.2, through the real extractor,
matcher and differ. Prints the change events each one produces.

Run from the repository root: uv run python reports/2026-10-10--up-to-speed/model-probes/probe.py [case ...]
"""
import sys, tempfile, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.produce.extract import extract
from cpp_semantic_diff.produce.build import assemble
from cpp_semantic_diff.produce.toolchain import load
from cpp_semantic_diff.match.matcher import match_models
from cpp_semantic_diff.diff.differ import diff_models

tc = load()
def model(src, rev):
    d = Path(tempfile.mkdtemp())
    (d/"a.cpp").write_text(src)
    e = CompileEntry(file=d/"a.cpp", directory=d, arguments=["clang++","-std=c++20","-c","a.cpp","-o","a.o"])
    return assemble(rev, [extract(e, root=d, toolchain=tc)], toolchain=tc)

CASES = {
 "default argument": ("void f(int x = 1) {}", "void f(int x = 2) {}"),
 "ctor init list": ("struct S { int x; S() : x(1) {} };", "struct S { int x; S() : x(2) {} };"),
 "default member init": ("struct S { int x = 1; };", "struct S { int x = 2; };"),
 "macro constant": ("#define LIMIT 10\nint f() { return LIMIT; }", "#define LIMIT 20\nint f() { return LIMIT; }"),
 "constexpr global": ("constexpr int kLimit = 10;\nint f() { return kLimit; }", "constexpr int kLimit = 20;\nint f() { return kLimit; }"),
 "enum value": ("enum E { A = 1, B = 2 };\nint f() { return B; }", "enum E { A = 1, B = 3 };\nint f() { return B; }"),
 "enum value added": ("enum E { A, B };", "enum E { A, B, C };"),
 "static data member": ("struct S { static constexpr int n = 4; };", "struct S { static constexpr int n = 8; };"),
 "field reorder": ("struct S { char a; double b; int c; };", "struct S { double b; char a; int c; };"),
 "base order": ("struct A{}; struct B{}; struct S : A, B {};", "struct A{}; struct B{}; struct S : B, A {};"),
 "virtual base": ("struct A{}; struct S : A {};", "struct A{}; struct S : virtual A {};"),
 "typedef change": ("using T = int;\nvoid f(T) {}\nvoid g() { f(1); }", "using T = long;\nvoid f(T) {}\nvoid g() { f(1); }"),
 "constexpr dropped": ("constexpr int f() { return 1; }", "int f() { return 1; }"),
 "final added": ("struct S { virtual void f(); };", "struct S final { virtual void f(); };"),
 "nodiscard added": ("int f();", "[[nodiscard]] int f();"),
 "deprecated added": ("void f();", "[[deprecated]] void f();"),
 "static_assert removed": ("struct S { int x; };\nstatic_assert(sizeof(S) == 4);", "struct S { int x; };"),
 "inline ns bump": ("namespace lib { inline namespace v1 { void f() {} } }", "namespace lib { inline namespace v2 { void f() {} } }"),
 "param added": ("void f(int) {}\nvoid g() { f(1); }", "void f(int, int = 0) {}\nvoid g() { f(1); }"),
 "template call dep": ("void h(int);\ntemplate<class T> void f(T t) { h(t); g(t); }", "void h(int);\ntemplate<class T> void f(T t) { g(t); }"),
 "fn pointer use": ("void cb();\nvoid reg(void(*)());\nvoid s() { reg(cb); }", "void cb();\nvoid cb2();\nvoid reg(void(*)());\nvoid s() { reg(cb2); }"),
 "move semantics lost": ("struct S { int* p; };", "struct S { int* p; ~S(); };"),
}
only = sys.argv[1:]
for name,(a,b) in CASES.items():
    if only and name not in only: continue
    try:
        ma, mb = model(a,"a"*40), model(b,"b"*40)
        ev = diff_models(ma, mb, match_models(ma, mb))
        desc = [f"{e.kind.value}({str(e.subject).split('|')[0][-30:]}{' '+str(e.payload) if e.payload else ''})" for e in ev]
        print(f"{name:32} {len(ev):2} events  {'; '.join(desc)[:230]}")
    except Exception as ex:
        print(f"{name:32} ERROR {type(ex).__name__}: {ex}"[:200])
