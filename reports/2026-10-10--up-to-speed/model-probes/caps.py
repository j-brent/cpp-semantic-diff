"""Which facts libclang exposes for constructs v0 does not model: the-model.md §4.5.

Run from the repository root: uv run python reports/2026-10-10--up-to-speed/model-probes/caps.py
"""
import tempfile, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
import clang.cindex as ci
from cpp_semantic_diff.produce.toolchain import load
tc = load()
import ctypes
L=ci.conf.lib
L.clang_Cursor_Evaluate.argtypes=[ci.Cursor]; L.clang_Cursor_Evaluate.restype=ctypes.c_void_p
L.clang_EvalResult_getKind.argtypes=[ctypes.c_void_p]; L.clang_EvalResult_getKind.restype=ctypes.c_int
L.clang_EvalResult_getAsInt.argtypes=[ctypes.c_void_p]; L.clang_EvalResult_getAsInt.restype=ctypes.c_int
L.clang_isVirtualBase.argtypes=[ci.Cursor]; L.clang_isVirtualBase.restype=ctypes.c_uint
src = r'''
#define LIMIT 10
namespace lib { inline namespace v1 {
using T = long;
enum class E : unsigned char { A = 1, B = 3 };
constexpr int kLimit = 20;
thread_local int tl;
struct A {}; struct B {};
struct [[nodiscard]] S final : B, virtual A {
  char a; double b; int c = 5;
  static constexpr int n = 8;
  S() : c(LIMIT) {}
  [[deprecated]] constexpr int m(int x = 2) const noexcept { return x; }
  friend void fr();
};
static_assert(sizeof(int) == 4, "int");
void h(int);
template<class U> void f(U u) { h(u); g(u); h(1); }
void cb(); void reg(void(*)());
void s() { reg(cb); }
}}
'''
d = Path(tempfile.mkdtemp()); (d/"a.cpp").write_text(src)
idx = ci.Index.create()
tu = idx.parse(str(d/"a.cpp"), args=tc.parse_args(["-std=c++20"]), options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
def walk(c, depth=0):
    for ch in c.get_children():
        if ch.location.file is None or not ch.location.file.name.endswith("a.cpp"): continue
        extra = []
        k = ch.kind
        try:
            if k == ci.CursorKind.ENUM_CONSTANT_DECL: extra.append(f"value={ch.enum_value}")
            if k in (ci.CursorKind.STRUCT_DECL,): extra.append(f"size={ch.type.get_size()} align={ch.type.get_align()}")
            if k == ci.CursorKind.FIELD_DECL: extra.append(f"offset_bits={ch.get_field_offsetof()}")
            if k == ci.CursorKind.VAR_DECL:
                ev = ci.conf.lib.clang_Cursor_Evaluate(ch)
                extra.append(f"storage={ch.storage_class.name} tls={ch.tls_kind.name if hasattr(ch,'tls_kind') else '?'} linkage={ch.linkage.name}")
                if ev: extra.append(f"eval={ci.conf.lib.clang_EvalResult_getAsInt(ev)}")
            if k == ci.CursorKind.CXX_BASE_SPECIFIER: extra.append(f"virtual_base={ci.conf.lib.clang_isVirtualBase(ch)}")
            if k in (ci.CursorKind.CXX_METHOD, ci.CursorKind.FUNCTION_DECL): extra.append(f"availability={ch.availability.name}")
            if k == ci.CursorKind.TYPE_ALIAS_DECL: extra.append(f"underlying={ch.underlying_typedef_type.get_canonical().spelling}")
        except Exception as e: extra.append(f"!{type(e).__name__}")
        print("  "*depth + f"{k.name} {ch.spelling!r} " + " ".join(extra))
        if k not in (ci.CursorKind.COMPOUND_STMT,) and depth < 6:
            walk(ch, depth+1)
walk(tu.cursor)
