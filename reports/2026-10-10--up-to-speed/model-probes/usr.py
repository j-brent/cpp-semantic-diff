"""Clang's identifiers for a macro, an enumerator and a static_assert, before and after a line is
added above them. A macro's identifier carries its byte offset; a static_assert has none.

Run from the repository root: uv run python reports/2026-10-10--up-to-speed/model-probes/usr.py
"""
import tempfile, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
import clang.cindex as ci
from cpp_semantic_diff.produce.toolchain import load
tc=load()
for src in ["#define LIMIT 10\nenum E{A};\nstatic_assert(true);", "// added line\n\n#define LIMIT 10\nenum E{A};\nstatic_assert(true);"]:
    d=Path(tempfile.mkdtemp()); (d/"a.cpp").write_text(src)
    tu=ci.Index.create().parse(str(d/"a.cpp"),args=tc.parse_args(["-std=c++20"]),options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
    for c in tu.cursor.get_children():
        if c.location.file and c.location.file.name.endswith("a.cpp"):
            print(c.kind.name, repr(c.get_usr()))
            for k in c.get_children():
                if k.kind==ci.CursorKind.ENUM_CONSTANT_DECL: print("  ",k.kind.name, repr(k.get_usr()))
