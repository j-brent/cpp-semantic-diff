"""The text diff of the C++ files, as changed tokens.

The other half of uncovered-change reporting (ADR-042): a change event says which lines it
came from (ARCH-001 §6.2), and this says which lines changed. A line diff alone would count a
reindented block or an edited comment as changed, and the model, which ignores both, would
then be blamed for not explaining them. So changes are compared as **tokens**: comments,
whitespace and line splices are not tokens, and a hunk whose tokens are the same on both sides
is formatting.

Two layers. The core -- `lex`, `parse_patch`, `changed_runs` -- takes texts and hunks and
touches no git, so it is tested on strings. `token_delta` is the one function that asks git:
one `git diff` for the hunks and one `git cat-file --batch` for the file contents.

The lexer is this project's own, a regular expression rather than libclang (ADR-042). It has
to lex both sides of every changed file, headers that no translation unit includes among
them, with no compile flags, and it only has to agree with itself: a token here is compared
with another token here, never with one clang produced.
"""

from __future__ import annotations

import bisect
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

from cpp_semantic_diff.acquire.git import RevPair, resolve_rename_similarity, run_git_raw
from cpp_semantic_diff.errors import GitError
from cpp_semantic_diff.model.events import Side

#: Suffixes treated as C++. What is *modelled* is whatever the compile database lists
#: (`Checkout.cpp_entries`); this is what counts as a C++ file in the text diff, which has to
#: include headers no translation unit reads, because those are changes the model cannot see.
CPP_SUFFIXES = frozenset({".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".inl"})


def is_cpp(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in CPP_SUFFIXES


# -- lexing ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    #: Last line the token touches: a raw string literal can span several.
    end_line: int


#: Translation phases 1-3, approximately, and only as far as comparing two texts needs. Order
#: matters: a raw string and an encoding prefix both start like an identifier, and a comment
#: starts like a division.
#:
#: `>>` and `>>=` are deliberately not single tokens. `vector<vector<int> >` and
#: `vector<vector<int>>` are the same code, and reformatting one into the other is not a
#: change for the model to explain.
_LEXEME = re.compile(
    r"""
    (?P<space> [ \t\v\f\n]+ | \\\n )
  | (?P<comment> //(?:[^\n\\]|\\.)* | /\*.*?(?:\*/|\Z) )
  | (?P<raw> (?:u8|u|U|L)?R"(?P<delim>[^ ()\\\t\v\f\n"]{0,16})\(.*?\)(?P=delim)" )
  | (?P<string> (?:u8|u|U|L)?"(?:[^"\\\n]|\\.)*" )
  | (?P<char> (?:u8|u|U|L)?'(?:[^'\\\n]|\\.)*' )
  | (?P<number> \.?[0-9](?:[eEpP][+-]|'[0-9A-Za-z_]|[0-9A-Za-z_.])* )
  | (?P<word> [^\W\d]\w* )
  | (?P<punct> <<=|<=>|->\*|\.\.\.|::|->|\+\+|--|<<|<=|>=|==|!=|&&|\|\||[-+*/%&|^]=|\#\#|\.\* )
  | (?P<other> . )
    """,
    re.VERBOSE | re.DOTALL,
)


def lex(text: str) -> list[Token]:
    """The tokens of a C++ text, with the line each starts and ends on.

    Total: every character is consumed by some alternative, so malformed or truncated code
    lexes into *something* rather than raising, and an unterminated literal becomes a quote
    token instead of swallowing the file.
    """
    tokens: list[Token] = []
    line = 1
    for match in _LEXEME.finditer(text):
        lexeme = match.group()
        newlines = lexeme.count("\n")
        if match.lastgroup not in ("space", "comment"):
            tokens.append(Token(lexeme, line, line + newlines))
        line += newlines
    return tokens


def decode(blob: bytes) -> str:
    """Bytes to text, losslessly, with CRLF line endings made LF.

    `surrogateescape` because a C++ file need not be UTF-8 and every byte must survive into a
    token that compares equal only to the same byte. Line endings are normalized because a
    file converted from CRLF to LF has changed no token, and git reports every line of it.
    """
    return blob.decode("utf-8", "surrogateescape").replace("\r\n", "\n")


# -- hunks ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LineHunk:
    """One `@@ -old_start,old_count +new_start,new_count @@` of a zero-context diff."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int

    def lines(self, side: Side) -> tuple[int, int]:
        """The inclusive line range on one side; empty (start > end) for a pure insertion."""
        if side is Side.BASE:
            return self.old_start, self.old_start + self.old_count - 1
        return self.new_start, self.new_start + self.new_count - 1


@dataclass(frozen=True)
class FilePatch:
    """One file's part of the patch. A path is None on the side where the file does not exist."""

    old_path: str | None
    new_path: str | None
    old_blob: str | None
    new_blob: str | None
    hunks: tuple[LineHunk, ...]
    binary: bool = False

    @property
    def path(self) -> str:
        """The head path, or the base path for a deletion -- what `--name-only` prints."""
        return self.new_path or self.old_path or ""


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_INDEX = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)")
_NULL_BLOB = re.compile(r"^0+$")
_ESCAPES = {
    ord("a"): 7,
    ord("b"): 8,
    ord("t"): 9,
    ord("n"): 10,
    ord("v"): 11,
    ord("f"): 12,
    ord("r"): 13,
    ord('"'): 34,
    ord("\\"): 92,
}


def _unquote(field: str) -> str:
    """A path as git prints it: verbatim, or C-quoted when it holds a special character.

    `core.quotePath=false` stops git quoting non-ASCII bytes, but a quote, a backslash or a
    control character in a name is still quoted, and the name that comes back must be the
    file's real one -- it is joined against the rename map and the model's locations.
    """
    if not (field.startswith('"') and field.endswith('"') and len(field) >= 2):
        return field
    raw = field[1:-1].encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        byte = raw[i]
        if byte != ord("\\") or i + 1 == len(raw):
            out.append(byte)
            i += 1
            continue
        nxt = raw[i + 1]
        if ord("0") <= nxt <= ord("7"):
            digits = raw[i + 1 : i + 4]
            out.append(int(digits, 8) & 0xFF)
            i += 4
        else:
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
    return out.decode("utf-8", "surrogateescape")


def _strip_prefix(field: str, prefix: str) -> str | None:
    # Git ends a `---`/`+++` name that contains a space with a tab, for the benefit of
    # `patch`. A name that really ends in a tab is quoted, so the tab is never the name's.
    path = _unquote(field.removesuffix("\t"))
    if path == "/dev/null":
        return None
    return path.removeprefix(prefix)


def _header_paths(rest: str) -> tuple[str, str] | None:
    """The two paths of `diff --git a/X b/Y`, where nothing more specific names them.

    Only a binary or mode-only change needs this, and for those git prints the same path
    twice unless the file was renamed, in which case `rename from`/`rename to` name it. So an
    unquoted header splits at its middle; a quoted one is two quoted fields.
    """
    if rest.startswith('"'):
        end = rest.find('" ', 1)
        while end != -1 and rest[end - 1] == "\\":
            end = rest.find('" ', end + 1)
        if end == -1:
            return None
        old, new = rest[: end + 1], rest[end + 2 :]
        return _unquote(old).removeprefix("a/"), _unquote(new).removeprefix("b/")
    if (len(rest) - 5) % 2:
        return None
    half = (len(rest) - 5) // 2
    old, new = rest[2 : 2 + half], rest[half + 5 :]
    return (old, new) if old == new else None


def parse_patch(text: str) -> list[FilePatch]:
    """A zero-context patch, from `git diff -U0 --full-index`, into one record per file.

    Content lines are consumed by count, not by their first character: with no context lines a
    hunk is exactly `old_count` lines of `-` and `new_count` of `+`, and a removed line that
    reads `-- x` must not be taken for the next file's `--- a/x`.
    """
    patches: list[FilePatch] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("diff --git "):
            i += 1
            continue
        header = _header_paths(line.removeprefix("diff --git "))
        old_path: str | None = header[0] if header else None
        new_path: str | None = header[1] if header else None
        old_blob = new_blob = None
        binary = False
        hunks: list[LineHunk] = []
        i += 1
        while i < len(lines) and not lines[i].startswith(("diff --git ", "@@ ")):
            meta = lines[i]
            if meta.startswith("new file mode"):
                old_path = None
            elif meta.startswith("deleted file mode"):
                new_path = None
            elif meta.startswith("rename from "):
                old_path = _unquote(meta.removeprefix("rename from "))
            elif meta.startswith("rename to "):
                new_path = _unquote(meta.removeprefix("rename to "))
            elif meta.startswith("--- "):
                old_path = _strip_prefix(meta.removeprefix("--- "), "a/")
            elif meta.startswith("+++ "):
                new_path = _strip_prefix(meta.removeprefix("+++ "), "b/")
            elif meta.startswith("Binary files ") or meta == "GIT binary patch":
                binary = True
            elif match := _INDEX.match(meta):
                old_blob = None if _NULL_BLOB.match(match[1]) else match[1]
                new_blob = None if _NULL_BLOB.match(match[2]) else match[2]
            i += 1
        while i < len(lines) and lines[i].startswith("@@ "):
            match = _HUNK.match(lines[i])
            if match is None:
                raise GitError(["diff", "-U0"], 0, f"unreadable hunk header: {lines[i]!r}")
            hunk = LineHunk(
                old_start=int(match[1]),
                old_count=int(match[2]) if match[2] is not None else 1,
                new_start=int(match[3]),
                new_count=int(match[4]) if match[4] is not None else 1,
            )
            hunks.append(hunk)
            i += 1
            remaining = hunk.old_count + hunk.new_count
            while i < len(lines) and (remaining or lines[i].startswith("\\")):
                if not lines[i].startswith("\\"):  # "\ No newline at end of file"
                    remaining -= 1
                i += 1
        patches.append(
            FilePatch(
                old_path=old_path,
                new_path=new_path,
                old_blob=old_blob,
                new_blob=new_blob,
                hunks=tuple(hunks),
                binary=binary,
            )
        )
    return patches


# -- changed tokens -------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenRun:
    """Consecutive tokens one side of a hunk changed: deleted from base or inserted in head.

    `whole_lines` counts the lines every token of which is in the run -- what tells a block
    that moved from a few tokens that happen to recur (`diff.coverage`).
    """

    side: Side
    file: str
    tokens: tuple[Token, ...]
    whole_lines: int

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(t.text for t in self.tokens)


class _Lexed:
    """One side of one file, lexed once, with what `changed_runs` asks of it."""

    def __init__(self, text: str) -> None:
        self.tokens = lex(text)
        self.starts = [t.line for t in self.tokens]
        self.per_line = Counter(self.starts)
        self.taken: set[int] = set()

    def within(self, first: int, last: int) -> list[Token]:
        """Tokens touching lines `first..last` not already given to an earlier hunk.

        A token belongs to the first hunk it touches. Hunks of a zero-context diff are
        disjoint, so only a token spanning lines -- a raw string literal -- can touch two,
        and counting it twice would count one change twice.
        """
        if first > last:
            return []
        lo = bisect.bisect_left(self.starts, first)
        if lo and self.tokens[lo - 1].end_line >= first:
            lo -= 1
        hi = bisect.bisect_right(self.starts, last)
        picked = [i for i in range(lo, hi) if i not in self.taken]
        self.taken.update(picked)
        return [self.tokens[i] for i in picked]

    def whole_lines(self, tokens: Sequence[Token]) -> int:
        counts = Counter(t.line for t in tokens)
        return sum(1 for line, n in counts.items() if n == self.per_line[line])


def changed_runs(
    old_path: str | None,
    old_text: str,
    new_path: str | None,
    new_text: str,
    hunks: Iterable[LineHunk],
) -> list[TokenRun]:
    """The tokens each hunk changed, as runs.

    Within a hunk the two sides' tokens are aligned (`SequenceMatcher`, `autojunk=False`, so
    that common tokens like `;` are not discarded as noise on large hunks); an equal stretch is
    formatting, and everything else is a change. Comparing within a hunk, rather than across
    the file, keeps the comparison where git found the change: a token that moved between
    hunks is changed in both, which is what move detection then looks for.
    """
    old, new = _Lexed(old_text), _Lexed(new_text)
    runs: list[TokenRun] = []

    def emit(side: Side, path: str | None, lexed: _Lexed, tokens: Sequence[Token]) -> None:
        if tokens and path is not None:
            runs.append(TokenRun(side, path, tuple(tokens), lexed.whole_lines(tokens)))

    for hunk in hunks:
        before = old.within(*hunk.lines(Side.BASE))
        after = new.within(*hunk.lines(Side.HEAD))
        matcher = SequenceMatcher(
            None, [t.text for t in before], [t.text for t in after], autojunk=False
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            emit(Side.BASE, old_path, old, before[i1:i2])
            emit(Side.HEAD, new_path, new, after[j1:j2])
    return runs


# -- asking git -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcludedFile:
    """A changed C++ file whose tokens were not compared, and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class TokenDelta:
    """Every changed token in the C++ files of a comparison, as runs per side and file."""

    runs: tuple[TokenRun, ...] = ()
    excluded_files: tuple[ExcludedFile, ...] = ()


def _pathspecs() -> list[str]:
    # `icase` because `CPP_SUFFIXES` is compared case-insensitively; `**/` matches at the
    # root too.
    return [f":(glob,icase)**/*{suffix}" for suffix in sorted(CPP_SUFFIXES)]


def _patch(repo: Path, revs: RevPair, similarity: int) -> str:
    """`git diff -U0` over the C++ files, independent of whoever's git config it runs under.

    Every flag answers a setting that would otherwise change the output: rename detection is
    on and at the same threshold as `file_changes`, so a renamed file is compared with its
    old self here exactly when the matcher was told it was renamed; external diff drivers
    and textconv filters are off, so the patch is of the bytes in the repository; prefixes
    and quoting are fixed, so paths parse; `--full-index` gives blob ids `cat-file` can read.
    """
    return run_git_raw(
        repo,
        "-c",
        "core.quotePath=false",
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-relative",
        "--full-index",
        f"--find-renames={similarity}%",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        f"{revs.base_sha}..{revs.head_sha}",
        "--",
        *_pathspecs(),
    )


def read_blobs(repo: Path, oids: Iterable[str]) -> dict[str, bytes]:
    """Blob contents by id, in one `git cat-file --batch`.

    One process for every file rather than one per file: a branch can touch hundreds of
    headers, and process start-up would dominate the cost of reading them.
    """
    wanted = sorted(set(oids))
    if not wanted:
        return {}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            input="".join(f"{oid}\n" for oid in wanted).encode(),
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - resolve_revs found git already
        raise GitError(["cat-file", "--batch"], -1, str(exc)) from exc
    if proc.returncode != 0:
        raise GitError(["cat-file", "--batch"], proc.returncode, proc.stderr.decode())
    out = proc.stdout
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in wanted:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode().split()
        if len(header) != 3:
            # `<oid> missing`: the patch named a blob the repository does not have.
            raise GitError(["cat-file", "--batch"], 0, f"cannot read blob {oid}: {header}")
        size = int(header[2])
        blobs[header[0]] = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def token_delta(repo: Path, revs: RevPair, *, rename_similarity: int | None = None) -> TokenDelta:
    """The changed tokens of every C++ file the comparison touched."""
    patches = [
        p
        for p in parse_patch(_patch(repo, revs, resolve_rename_similarity(rename_similarity)))
        if any(path is not None and is_cpp(path) for path in (p.old_path, p.new_path))
    ]
    excluded = tuple(ExcludedFile(p.path, "binary") for p in patches if p.binary)
    compared = [p for p in patches if not p.binary and p.hunks]
    blobs = read_blobs(
        repo, (oid for p in compared for oid in (p.old_blob, p.new_blob) if oid is not None)
    )
    runs: list[TokenRun] = []
    for p in compared:
        old = decode(blobs[p.old_blob]) if p.old_blob is not None else ""
        new = decode(blobs[p.new_blob]) if p.new_blob is not None else ""
        runs.extend(changed_runs(p.old_path, old, p.new_path, new, p.hunks))
    return TokenDelta(runs=tuple(runs), excluded_files=excluded)
