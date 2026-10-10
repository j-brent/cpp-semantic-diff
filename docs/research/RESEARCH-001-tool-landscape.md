<!-- metadata
id: RESEARCH-001
type: research-note
status: draft
author: Brent Runyan
created: 2026-08-01
dependencies: []
origin: written during codeatlas-cpp research (RESEARCH-007 there); renumbered on move
kind: frozen
frozen: 2026-08-01
-->

# The C++ Analysis Tool Landscape Outside clang

*Frozen document: research as of 2026-08-01. Not maintained.*

A survey of tools that parse, understand, and analyze C++ projects — source, intermediate
representations, and the object and executable files the compiler emits — classified by use case.
It deliberately looks past clang, because clang is already the assumed answer and the interesting
question is what else exists.

This is a landscape survey compiled from tool documentation and prior use, not a measurement
report. Nothing here was benchmarked. Measured numbers for the clang-side subset — parse times,
include-closure sizes, AST yields against seven real repos — exist in the sibling CodeAtlas C++
research note `docs/research/cpp-graph-feasibility.md` (RESEARCH-006) in the `dev-team` repo, and
should be re-measured here before any of it is treated as a constraint on this tool.

---

## The frontend constraint

Full-fidelity C++ semantics comes from exactly four frontends: **clang**, **GCC**, **EDG**, and
**MSVC**. Everything else either wraps one of those, uses a fuzzy or partial parser (tree-sitter,
Eclipse CDT, Cppcheck's tokenizer), or works on compiler output rather than source.

Lineage is tagged throughout: `[clang]` `[gcc]` `[edg]` `[own]` `[fuzzy]`.

---

## 1 · Source parsing — AST and frontends

| Tool | Notes |
|---|---|
| **GCC** `-fdump-tree-*`, `-fdump-ipa-cgraph`, `-fdump-rtl-*`, `-fcallgraph-info` | `[gcc]` Real semantic dumps including a call graph (VCG format). Underused. |
| **GCC plugins** (C API, or `gcc-python-plugin`) | `[gcc]` In-process GENERIC/GIMPLE access. |
| **EDG C++ Front End** | `[edg]` Commercial; the most standards-complete parser. Licensed by Intel, Coverity, SciTools, MSVC IntelliSense. |
| **ROSE Compiler Framework** (LLNL) | `[edg]` Source-to-source, full AST plus rewriting. The serious non-clang research platform. |
| **Eclipse CDT indexer** (headless) | `[own]` Java, tolerant of missing configuration, no build required. Powers Joern. |
| **srcML** | `[own]` Wraps source in XML preserving original text; query with XPath/XSLT. No preprocessing, no types. |
| **tree-sitter-cpp** | `[fuzzy]` Fast, incremental, error-tolerant. Syntax only. Basis of `ast-grep`, Semgrep, most editors. |
| **Doxygen** (`GENERATE_XML=YES`) | `[own]` Independent parser; class, inheritance, call, and include graphs as XML. Imperfect but zero-setup. |
| **Cppcheck** | `[own]` Own tokenizer plus value-flow engine; `--xml` dumps. |
| **Understand** (SciTools) | `[edg]` Proprietary database with Python/Perl API. Best commercial queryable code model. |
| **CastXML**, `cppast`, **Sourcetrail** (archived) | `[clang]` Listed so they are not mistaken for alternatives. `gccxml` is dead. |
| ANTLR grammars, Boost.Wave, `pycparser` | Grammar-level only. A C++ grammar without semantics is near-useless past declarations. |

## 2 · Whole-codebase indexes and code-as-graph databases

| Tool | Notes |
|---|---|
| **CodeQL** | `[own]` Compiler-wrapping extractor into a relational database with a Datalog-derived query language. Strongest general query story. |
| **Joern** | `[fuzzy/CDT]` Code Property Graph (AST + CFG + PDG), Scala/Gremlin queries, Neo4j export. Built for exactly this use case. |
| **Glean** (Meta) | Language-agnostic fact database; the C++ indexer is clang-based. |
| **Kythe**, **SCIP**, **LSIF** | Cross-reference schemas. C++ indexers are `[clang]`. |
| **Universal Ctags**, **GNU GLOBAL (gtags)**, **cscope**, **OpenGrok** | `[fuzzy]` Heuristic taggers. Cheap, scale to anything, wrong on templates and overloads. |
| **Semgrep**, **ast-grep**, **Comby** | Pattern matching and rewriting without a full frontend. |
| **Coccinelle**, **Frama-C** | C only — do not assume C++ support. |

## 3 · Build graph, include graph, compilation database

| Tool | Notes |
|---|---|
| `cmake --graphviz=out.dot` | Target dependency graph, free. |
| **CMake File API** (`.cmake/api/v1`) | JSON: targets, sources, link lines, defines. The correct programmatic route. |
| `ninja -t graph` / `-t deps` / `-t commands` | Actual build DAG and discovered header dependencies. |
| **Bazel** `query` / `cquery` / `aquery` | Best-in-class build introspection where available. |
| **Bear**, **compiledb**, **intercept-build** | Produce `compile_commands.json` for non-CMake builds. |
| `gcc -H`, `-MD -MF` | Include tree straight from the compiler. |
| **cpp-dependencies** (TomTom), **cppdep**, **IncludeGuardian** | Component and include-cycle analysis. |
| **include-what-you-use** | `[clang]` Include correctness. |
| **vcperf** / MSVC Build Insights, `/d1reportTime`, `-ftime-report` | Build-time attribution — the MSVC and GCC counterparts to clang's `-ftime-trace`. |
| **ninjatracing** + Perfetto | Build timeline visualization. |

## 4 · Static analysis and bug finding (non-clang engines)

- **Cppcheck** `[own]` — free, own engine, MISRA addon.
- **PVS-Studio** `[own]` — commercial, strong C++ coverage.
- **Coverity** `[edg]`, **Klocwork**, **Helix QAC** `[own]`, **Parasoft C++test**, **LDRA** — enterprise and safety-certification tools.
- **Polyspace** (MathWorks), **Astrée** (AbsInt) — sound abstract interpretation, DO-178C / ISO 26262.
- **SonarQube / SonarLint C++** `[own]`.
- **Infer** (Meta) — separation logic; the C++ frontend is clang.
- **CBMC**, **ESBMC** — bounded model checking, actual proofs over C/C++.
- **KLEE**, **SeaHorn**, **SVF**, **PhASAR** — symbolic execution, value-flow, and data-flow, all over LLVM IR.

## 5 · Architecture, metrics, duplication

- **Axivion Suite**, **Lattix**, **Structure101** — DSM, layering rules, architecture erosion.
- **CodeScene** — behavioral analysis from git history (hotspots, coupling, knowledge maps). Complements static tools rather than duplicating them.
- **Lizard** (CCN/NLOC, no build required), **Metrix++**, **CCCC**, **SourceMeter**.
- **cloc**, **scc**, **tokei** — line counting.
- **PMD-CPD**, **NiCad**, **CloneDR**, **Simian** — clone detection.

## 6 · Compiler IR — between source and object

- **LLVM bitcode toolchain**: `llvm-dis`, `opt -passes=dot-callgraph/dot-cfg`, `llvm-link`, `llvm-extract`. Adjacent to clang, but a distinct analyzable artifact.
- **gllvm / WLLVM** — extract whole-program bitcode from an ordinary build.
- **GCC GIMPLE/RTL dumps** — `-fdump-tree-optimized`, `-fdump-ipa-cgraph`, `-fdump-rtl-expand`.
- **egypt** — GCC RTL dump to Graphviz call graph. Tiny, old, still works.
- **llvm-mca**, **uiCA**, **OSACA** — static machine-code throughput modeling.
- **Compiler Explorer (godbolt) API** — scriptable multi-compiler codegen diffing.

## 7 · Object and executable inspection

**Cross-platform and ELF**
`nm`, `objdump`, `readelf`, `size`, `strings`, `ar`, `addr2line`, `c++filt`, `strip`, `ldd`
(binutils) · `eu-readelf`, `eu-stack`, **elfutils** libelf/libdw · `llvm-nm`, `llvm-objdump`,
`llvm-readobj`, `llvm-size`, `llvm-cxxfilt`, `llvm-symbolizer` · **LIEF** (Python/C++; ELF, PE and
Mach-O; reads *and* modifies) · **pyelftools** · **patchelf** · **objconv** (Agner Fog).

**Windows and PE**
`dumpbin`, `link /dump`, **Dependencies.exe** (modern replacement for Dependency Walker),
**pefile**, **PE-bear**, **CFF Explorer**, **DIA SDK** (PDB API), `llvm-pdbutil`, `undname`,
**SizeBench** (Microsoft; binary size attribution), Sysinternals ListDLLs / VMMap / Process Monitor.

**macOS and Mach-O**
`otool`, `dyld_info`, `vtool`, `size -m`, `dsymutil`, **MachOView**.

**Demangling**: `c++filt` (Itanium), `undname` (MSVC), **demumble** (both).

## 8 · Debug info, type and layout recovery

| Tool | Notes |
|---|---|
| **dwarves** suite — `pahole`, `pfunct`, `codiff`, `pglobal` | `pahole` gives exact struct layout, padding and holes. Best single tool for what the compiler actually laid out. |
| **llvm-dwarfdump**, **dwarfdump**/libdwarf, **pyelftools** | Raw DWARF. |
| `gcc`/`clang -fdump-record-layouts`, MSVC `/d1reportSingleClassLayout<Name>` | Layout from the frontend instead of the binary. |
| **bpftool btf dump**, `dwarf2json` | BTF and kernel-style type extraction. |
| **debuginfod** | Fetch debug info on demand for stripped binaries. |
| **GDB Python API**, **LLDB Python API**, **WinDbg** `dx` / JS provider | Scriptable type and symbol enumeration — an underrated bulk-extraction path. |

## 9 · ABI and binary compatibility

- **libabigail** — `abidw`, `abidiff`, `abipkgdiff`. The reference tool; understands C++ ABI properly.
- **abi-compliance-checker** with **abi-dumper** and **vtable-dumper**.
- `nm --dynamic -D` plus version scripts, for symbol-visibility audits.
- **BinDiff**, **Diaphora**, `radiff2`, Ghidra Version Tracking — function-level binary diffing.

## 10 · Size and bloat attribution

- **Bloaty McBloatface** — sections, symbols, compile units, template instantiation cost, and `--diff` between builds. Start here.
- **puncover** (embedded), `nm --size-sort`, `llvm-size`, **SizeBench** (Windows).
- **Linker maps**: `-Wl,-Map=out.map`, lld `-Map`, MSVC `/MAP`, plus map-file parsers.

## 11 · Reverse engineering and decompilation

- **Ghidra** (free, scriptable, headless analyzer, RTTI and vtable recovery), **IDA Pro + Hex-Rays**, **Binary Ninja**, **rizin/radare2 + Cutter**, **Hopper**, **RetDec**, **Reko**, **Snowman**.
- **Disassembler engines**: Capstone, Zydis, iced-x86, LLVM MC.
- **Binary analysis frameworks**: **angr**, **BAP**, **Triton**, **miasm**, **B2R2**, **Unicorn** (emulation).
- **C++-specific recovery**: SEI **Pharos/OOAnalyzer** (class recovery), IDA **Class Informer**, **HexRaysPyTools**, Ghidra RTTI analyzers.
- **llvm-bolt** — post-link optimizer that builds accurate CFGs from real binaries plus profiles.

## 12 · Dynamic analysis, tracing, profiling

- **Valgrind** suite — memcheck, **callgrind** (dynamic call graph), cachegrind, massif, helgrind/DRD; view with **KCachegrind**.
- **uftrace** — C/C++ function-level tracing with call graphs. The closest thing to watching a program's structure execute.
- **perf** (with **hotspot** GUI and **FlameGraph**), **bpftrace**/eBPF, `strace`, `ltrace`, DTrace.
- **Intel VTune**, **AMD uProf**, **Superluminal**, **Tracy**, **Optick**, **Very Sleepy**, ETW/WPA.
- **Intel Pin**, **DynamoRIO**, **Frida**, **Dr. Memory** — instrumentation.
- **Sanitizers** (ASan, UBSan, TSan, MSan — both GCC and clang), **heaptrack**, **gperftools**.
- **gprof2dot** — converts callgrind, perf, gprof and VTune output to Graphviz. Directly relevant when profiles should be rendered as graphs.

## 13 · Coverage and fuzzing

`gcov` / `lcov` / `gcovr` `[gcc]` · `llvm-cov` · **OpenCppCoverage** (Windows) ·
**BullseyeCoverage** · **Squish Coco** · **AFL++**, **honggfuzz**, **libFuzzer**, **Radamsa**.

## 14 · Dependency, supply chain, SBOM

`conan graph info`, **vcpkg** manifests, `cmake --graphviz`, `ldd` / `lddtree`, **Syft** (SBOM
generation), **Grype**, **Trivy**, **OSV-Scanner** (CVEs), **Dependency-Track**, CycloneDX, SPDX.

---

## Two caveats

**Static C++ call graphs are unsound.** Virtual dispatch, function pointers, templates and
inlining mean no source-level tool yields the true edge set. Cross-checking a static graph
(Doxygen, CodeQL) against a dynamic one (callgrind, uftrace), or against the post-inlining binary
(Ghidra, BOLT), is the only way to know what is missing.

**Anything that avoids a real frontend loses the preprocessor.** srcML, tree-sitter and ctags all
analyze text that no compiler ever saw. Fine for navigation, wrong for semantics.

## If the goal is feeding a graph model without depending on clang

- **CMake File API + Doxygen XML** — cheap structure.
- **Understand or CodeQL** — accurate, queryable semantics.
- **Bloaty + pahole + nm** — the binary side.
- **callgrind or uftrace, through gprof2dot** — observed behavior.
