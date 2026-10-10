"""Citations in comments and docstrings point at sections that exist.

A comment explains why in its own words and may point at a frozen document, or at ARCH-001,
for further reading. ARCH-001's section numbers are permanent, so a citation of a section it
does not have is a broken pointer, and nothing else would notice: the comment still reads
well, and the reader who follows it lands somewhere else or nowhere.

The forms recognized:

- `ARCH-001 §4.3`, and a list that continues it: `ARCH-001 §3, §6.3` or `§5 and §9`.
- `RESEARCH-002 §3.11` and `RESEARCH-001 §n`, checked against those documents.
- `v0 tracker §5`, the frozen tracker of the v0 build, checked against its headings.
- **A bare `§n` cites ARCH-001.** That is the convention the code was written under, so a
  bare section meaning any other document has to name it.

`§11 rule 2` checks `§11`; the rule numbers are not headings.

A second test holds the other half of the rule: no comment cites a living document other than
ARCH-001. Living documents are rewritten, and a pointer into one drifts without anyone editing
the comment that holds it.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")])

#: Every document a comment may cite by section, and where its headings are.
DOCUMENTS: dict[str, Path] = {
    "ARCH-001": ROOT / "docs" / "ARCH-001-architecture.md",
    "RESEARCH-001": ROOT / "docs" / "research" / "RESEARCH-001-tool-landscape.md",
    "RESEARCH-002": ROOT / "docs" / "research" / "RESEARCH-002-semantic-diff-for-review.md",
    "v0 tracker": ROOT / "reports" / "2026-08-01--v0-build" / "STATUS.md",
}

SECTION = r"§\s?(\d+(?:\.\d+)*)"
#: A document name, one section, and any sections listed after it.
CHAIN = re.compile(
    r"\b(ARCH-001|ARCH|RESEARCH-00\d|v0 tracker|[A-Z]+-\d{3})\s+"
    + SECTION
    + r"((?:(?:,\s*|,?\s+(?:and|or)\s+)"
    + SECTION
    + r")*)"
)

#: Living documents. A comment that needs one is a comment that has not said why.
LIVING = re.compile(
    r"\bSTATUS(?:\.md)?\s+§|\bROADMAP\b|\bCONTRIBUTING\b|\bREADME\b|\bPLAN-\d{3}\b"
    r"|\bknown[- ]issue\b|\bdocs/plans/",
    re.IGNORECASE,
)


def headings(path: Path) -> set[str]:
    numbered = re.compile(r"^#+ (\d+(?:\.\d+)*)\b")
    return {m.group(1) for line in path.read_text().splitlines() if (m := numbered.match(line))}


def prose(path: Path) -> Iterator[tuple[int, str]]:
    """Each docstring, and each run of consecutive comment lines joined into one text.

    Joined so that a list of sections broken across two comment lines is still one list.
    String literals that are not docstrings are runtime output, and are not read. Nor is the
    docstring of a CLI command: typer prints it as the command's `--help`.
    """
    source = path.read_text()
    tree = ast.parse(source)
    help_text = {
        id(fn.body[0])
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr in {"command", "callback"}
            for d in fn.decorator_list
        )
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and id(node) not in help_text
        ):
            yield node.lineno, node.value.value
    run: list[str] = []
    start = previous = 0
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        line = token.start[0]
        text = token.string.lstrip("#:").strip()
        if run and line == previous + 1:
            run.append(text)
        else:
            if run:
                yield start, " ".join(run)
            run, start = [text], line
        previous = line
    if run:
        yield start, " ".join(run)


def citations(text: str) -> Iterator[tuple[str, str]]:
    """(document, section) for every section cited in `text`."""
    text = re.sub(r"\s+", " ", text)
    claimed: list[tuple[int, int]] = []
    for chain in CHAIN.finditer(text):
        name = "ARCH-001" if chain.group(1) == "ARCH" else chain.group(1)
        claimed.append(chain.span())
        yield name, chain.group(2)
        for section in re.finditer(SECTION, chain.group(3)):
            yield name, section.group(1)
    for bare in re.finditer(SECTION, text):
        if not any(a <= bare.start() < b for a, b in claimed):
            yield "ARCH-001", bare.group(1)


def test_every_cited_section_exists() -> None:
    sections = {name: headings(path) for name, path in DOCUMENTS.items()}
    broken = [
        f"{path.relative_to(ROOT)}:{line}: {name} §{section}"
        + ("" if name in sections else " (not a document a comment may cite)")
        for path in SOURCES
        for line, text in prose(path)
        for name, section in citations(text)
        if section not in sections.get(name, set())
    ]
    assert not broken, "cited sections that do not exist:\n" + "\n".join(broken)


def test_no_comment_cites_a_living_document() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{line}: {match.group(0)}"
        for path in SOURCES
        for line, text in prose(path)
        for match in LIVING.finditer(re.sub(r"\s+", " ", text))
    ]
    assert not found, "comments citing a living document:\n" + "\n".join(found)


def test_a_bare_section_is_read_as_arch_and_a_list_keeps_its_document() -> None:
    cited = list(citations("ARCH-001 §3, §6.3 and RESEARCH-002 §3.11 or §4; see §9 rule 2"))
    assert cited == [
        ("ARCH-001", "3"),
        ("ARCH-001", "6.3"),
        ("RESEARCH-002", "3.11"),
        ("RESEARCH-002", "4"),
        ("ARCH-001", "9"),
    ]
