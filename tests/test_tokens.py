"""Changed tokens: the text diff, compared the way the model compares code (ADR-042).

A change the model cannot explain is counted against it, so a change the model rightly
ignores -- whitespace, a comment, a line ending -- must not count as one. Most of these are
on strings; the last class asks a real git.
"""

from __future__ import annotations

from pathlib import Path

from conftest import commit, git, move
from cpp_semantic_diff.acquire.git import RevPair
from cpp_semantic_diff.acquire.third_party import ThirdPartyEntry, ThirdPartySet, ThirdPartySource
from cpp_semantic_diff.acquire.tokens import (
    LineHunk,
    TokenRun,
    changed_runs,
    decode,
    is_cpp,
    lex,
    parse_patch,
    token_delta,
)
from cpp_semantic_diff.model import Side


def index(old: str, new: str, mode: str = "") -> str:
    """A `--full-index` index line between two made-up blob ids."""
    return f"index {old * 40}..{new * 40}{' ' + mode if mode else ''}\n"


def texts(source: str) -> list[str]:
    return [t.text for t in lex(source)]


def whole(old: str, new: str) -> LineHunk:
    """One hunk spanning both texts: the comparison with no help from a line diff."""
    return LineHunk(1, old.count("\n") + 1, 1, new.count("\n") + 1)


def runs(old: str, new: str) -> list[TokenRun]:
    return changed_runs("a.cpp", old, "a.cpp", new, [whole(old, new)])


def changed(old: str, new: str) -> dict[Side, list[str]]:
    out: dict[Side, list[str]] = {Side.BASE: [], Side.HEAD: []}
    for run in runs(old, new):
        out[run.side].extend(run.texts)
    return out


class TestLexing:
    def test_comments_whitespace_and_splices_are_not_tokens(self) -> None:
        source = "int /* a */ x = 1; // one\n#define M \\\n  2\n"
        assert texts(source) == ["int", "x", "=", "1", ";", "#", "define", "M", "2"]

    def test_a_raw_string_is_one_token_whatever_it_holds(self) -> None:
        """Its body may contain what looks like a comment, a quote or a closing paren."""
        source = 'auto s = R"x(a )" // b\n)x";\nint y;\n'
        tokens = lex(source)
        raw = next(t for t in tokens if t.text.startswith("R"))
        assert raw.text == 'R"x(a )" // b\n)x"'
        assert (raw.line, raw.end_line) == (1, 2)
        assert [t.text for t in tokens][-3:] == ["int", "y", ";"]

    def test_digit_separators_and_prefixed_literals_stay_whole(self) -> None:
        assert texts("x = 1'000'000; c = u8'a'; s = L\"w\";") == [
            "x",
            "=",
            "1'000'000",
            ";",
            "c",
            "=",
            "u8'a'",
            ";",
            "s",
            "=",
            'L"w"',
            ";",
        ]

    def test_lines_are_counted_through_comments(self) -> None:
        tokens = lex("/* one\ntwo */ int\n// three\nx;")
        assert [(t.text, t.line) for t in tokens] == [("int", 2), ("x", 4), (";", 4)]

    def test_malformed_code_still_lexes(self) -> None:
        assert texts('char* s = "unterminated\nint x;') == [
            "char",
            "*",
            "s",
            "=",
            '"',
            "unterminated",
            "int",
            "x",
            ";",
        ]

    def test_a_lone_carriage_return_is_whitespace(self) -> None:
        assert texts("int x;\rint y;") == ["int", "x", ";", "int", "y", ";"]

    def test_crlf_is_not_a_change(self) -> None:
        assert decode(b"int x;\r\nint y;\r\n") == "int x;\nint y;\n"

    def test_a_non_utf8_byte_survives(self) -> None:
        assert decode(b'char* s = "\xe9";').encode("utf-8", "surrogateescape") == (
            b'char* s = "\xe9";'
        )


class TestChangedTokens:
    def test_reindenting_and_rewrapping_change_nothing(self) -> None:
        old = "int f(int a, int b) {\n  return a + b;\n}\n"
        new = "int f(int a,\n      int b)\n{\n        return a+b;\n}\n"
        assert runs(old, new) == []

    def test_a_comment_edit_changes_nothing(self) -> None:
        assert runs("int x = 1; // one\n", "int x = 1; /* uno */\n") == []

    def test_closing_angle_brackets_are_formatting(self) -> None:
        assert runs("vector<vector<int> > v;\n", "vector<vector<int>> v;\n") == []

    def test_a_value_change_is_one_token_on_each_side(self) -> None:
        assert changed("int x = 1;\nint y = 2;\n", "int x = 1;\nint y = 3;\n") == {
            Side.BASE: ["2"],
            Side.HEAD: ["3"],
        }

    def test_each_run_knows_its_side_file_and_lines(self) -> None:
        (removed,) = changed_runs(
            "old.cpp", "int a;\nint b;\n", "new.cpp", "int a;\n", [LineHunk(2, 1, 1, 0)]
        )
        assert (removed.side, removed.file, [t.line for t in removed.tokens]) == (
            Side.BASE,
            "old.cpp",
            [2, 2, 2],
        )

    def test_whole_lines_counts_lines_the_run_covers_entirely(self) -> None:
        (run,) = changed_runs("a.cpp", "", "a.cpp", "int a;\nint b;\n", [LineHunk(0, 0, 1, 2)])
        assert run.whole_lines == 2
        partial = [r for r in runs("int x = 1;\n", "int x = 2;\n") if r.side is Side.HEAD]
        assert partial[0].whole_lines == 0

    def test_only_the_hunks_lines_are_compared(self) -> None:
        """Outside a hunk git saw no change, and the comparison stays where git found one."""
        old = "int a = 1;\nint b = 2;\n"
        new = "int a = 9;\nint b = 2;\n"
        assert changed_runs("a.cpp", old, "a.cpp", new, [LineHunk(2, 1, 2, 1)]) == []

    def test_an_added_file_is_all_insertion(self) -> None:
        (run,) = changed_runs(None, "", "n.h", "int f();\n", [LineHunk(0, 0, 1, 1)])
        assert (run.side, run.file, run.texts) == (Side.HEAD, "n.h", ("int", "f", "(", ")", ";"))


class TestParsingThePatch:
    def test_a_removed_line_that_looks_like_a_header_is_content(self) -> None:
        patch = (
            "diff --git a/a.cpp b/a.cpp\n" + index("1", "2", "100644") + "--- a/a.cpp\n"
            "+++ b/a.cpp\n"
            "@@ -3,2 +3 @@\n"
            "--- x\n"
            "-+++ y\n"
            "+z\n"
            "diff --git a/b.cpp b/b.cpp\n" + index("3", "4", "100644") + "--- a/b.cpp\n"
            "+++ b/b.cpp\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "\\ No newline at end of file\n"
            "+b\n"
        )
        first, second = parse_patch(patch)
        assert (first.old_path, first.new_path, first.hunks) == (
            "a.cpp",
            "a.cpp",
            (LineHunk(3, 2, 3, 1),),
        )
        assert first.old_blob == "1" * 40
        assert (second.path, second.hunks) == ("b.cpp", (LineHunk(1, 1, 1, 1),))

    def test_added_deleted_and_renamed_files(self) -> None:
        patch = (
            "diff --git a/n.h b/n.h\n"
            "new file mode 100644\n" + index("0", "5") + "--- /dev/null\n"
            "+++ b/n.h\n"
            "@@ -0,0 +1 @@\n"
            "+int f();\n"
            "diff --git a/gone.cpp b/gone.cpp\n"
            "deleted file mode 100644\n" + index("6", "0") + "--- a/gone.cpp\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-int g();\n"
            "diff --git a/old.cpp b/new.cpp\n"
            "similarity index 90%\n"
            "rename from old.cpp\n"
            "rename to new.cpp\n"
        )
        added, deleted, renamed = parse_patch(patch)
        assert (added.old_path, added.new_path, added.old_blob) == (None, "n.h", None)
        assert (deleted.old_path, deleted.new_path, deleted.new_blob) == ("gone.cpp", None, None)
        assert (renamed.old_path, renamed.new_path, renamed.hunks) == ("old.cpp", "new.cpp", ())

    def test_binary_and_unusual_names(self) -> None:
        patch = (
            "diff --git a/x y.h b/x y.h\n"
            + index("1", "2")
            + "Binary files a/x y.h and b/x y.h differ\n"
            'diff --git "a/q\\"t.h" "b/q\\"t.h"\n' + index("1", "2") + '--- "a/q\\"t.h"\n'
            '+++ "b/q\\"t.h"\n'
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "diff --git a/s p.h b/s p.h\n" + index("1", "2") + "--- a/s p.h\t\n"
            "+++ b/s p.h\t\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        binary, quoted, spaced = parse_patch(patch)
        assert (binary.path, binary.binary) == ("x y.h", True)
        assert (quoted.old_path, quoted.new_path) == ('q"t.h', 'q"t.h')
        assert (spaced.old_path, spaced.new_path) == ("s p.h", "s p.h")


def test_headers_count_as_cpp() -> None:
    assert is_cpp("include/a.H") and is_cpp("src/x.inl") and not is_cpp("CMakeLists.txt")


class TestAskingGit:
    """What `token_delta` gets from a real repository, against git's own rename detection."""

    @staticmethod
    def revs(repo: Path, base: str) -> RevPair:
        return RevPair("main", "HEAD", base, git(repo, "rev-parse", "HEAD"))

    def test_a_header_no_translation_unit_includes_is_compared(self, bare_repo: Path) -> None:
        """It is in no model, so only the text diff can say it changed."""
        base = commit(bare_repo, "orphan.h", "int limit = 1;\n", "A")
        commit(bare_repo, "orphan.h", "int limit = 2;\n", "B")
        delta = token_delta(bare_repo, self.revs(bare_repo, base))
        assert {(r.side, r.file, r.texts) for r in delta.runs} == {
            (Side.BASE, "orphan.h", ("1",)),
            (Side.HEAD, "orphan.h", ("2",)),
        }

    def test_a_renamed_file_is_compared_with_its_old_self(self, bare_repo: Path) -> None:
        body = "".join(f"int f{i}() {{ return {i}; }}\n" for i in range(20))
        base = commit(bare_repo, "old.cpp", body, "A")
        move(bare_repo, "old.cpp", "new.cpp", "rename")
        commit(bare_repo, "new.cpp", body.replace("return 3;", "return 33;"), "edit")
        delta = token_delta(bare_repo, self.revs(bare_repo, base))
        assert {(r.side, r.file, r.texts) for r in delta.runs} == {
            (Side.BASE, "old.cpp", ("3",)),
            (Side.HEAD, "new.cpp", ("33",)),
        }

    def test_formatting_only_commits_and_other_files_produce_nothing(self, bare_repo: Path) -> None:
        base = commit(bare_repo, "a.cpp", "int f() { return 1; }\n", "A")
        commit(bare_repo, "a.cpp", "int f()\r\n{\r\n    return 1; // one\r\n}\r\n", "B")
        commit(bare_repo, "notes.txt", "x = 1\n", "C")
        assert token_delta(bare_repo, self.revs(bare_repo, base)).runs == ()

    def test_a_truncated_rename_map_is_recorded(self, bare_repo: Path) -> None:
        """Git says so only on stderr; the count must not quietly differ from the rename map
        the matcher was given."""
        bodies = {
            name: "".join(f"int {name}{i}() {{ return {i}; }}\n" for i in range(20))
            for name in ("p", "q")
        }
        for name, body in bodies.items():
            commit(bare_repo, f"{name}.cpp", body, name)
        base = git(bare_repo, "rev-parse", "HEAD")
        for name, body in bodies.items():
            move(bare_repo, f"{name}.cpp", f"{name}2.cpp", "rename")
            commit(bare_repo, f"{name}2.cpp", body + "int extra();\n", "edit")
        revs = self.revs(bare_repo, base)
        assert token_delta(bare_repo, revs).rename_limit is None
        git(bare_repo, "config", "diff.renameLimit", "1")
        assert token_delta(bare_repo, revs).rename_limit is not None

    def test_third_party_files_are_excluded_at_either_end_of_a_rename(
        self, bare_repo: Path
    ) -> None:
        """Only the repository's own code counts (ADR-025). A file moved out of or into a
        vendored directory was third-party at one end, so neither end is counted."""
        body = "".join(f"int v{i}() {{ return {i}; }}\n" for i in range(20))
        commit(bare_repo, "vendor/lib.h", "int lib = 1;\n", "A")
        commit(bare_repo, "vendor/out.h", body, "B")
        commit(bare_repo, "src/in.h", body.replace("v", "w"), "C")
        base = commit(bare_repo, "src/own.h", "int own = 1;\n", "D")
        commit(bare_repo, "vendor/lib.h", "int lib = 2;\n", "edit vendored")
        move(bare_repo, "vendor/out.h", "src/out.h", "move out of vendor")
        commit(bare_repo, "src/out.h", body + "int x;\n", "edit")
        move(bare_repo, "src/in.h", "vendor/in.h", "move into vendor")
        commit(bare_repo, "vendor/in.h", body.replace("v", "w") + "int y;\n", "edit")
        commit(bare_repo, "src/own.h", "int own = 2;\n", "edit own")
        vendor = ThirdPartySet(
            entries=(ThirdPartyEntry("vendor", frozenset({ThirdPartySource.DECLARED})),)
        )
        delta = token_delta(bare_repo, self.revs(bare_repo, base), third_party=vendor)
        assert {r.file for r in delta.runs} == {"src/own.h"}
        assert sorted((e.path, e.reason) for e in delta.excluded_files) == [
            ("src/out.h", "third-party"),
            ("vendor/in.h", "third-party"),
            ("vendor/lib.h", "third-party"),
        ]

    def test_a_binary_file_is_excluded_by_name(self, bare_repo: Path) -> None:
        base = commit(bare_repo, "a.cpp", "int f();\n", "A")
        (bare_repo / "blob.h").write_bytes(b"\x00\x01\x02")
        git(bare_repo, "add", "blob.h")
        git(bare_repo, "commit", "-q", "-m", "binary")
        delta = token_delta(bare_repo, self.revs(bare_repo, base))
        assert [(e.path, e.reason) for e in delta.excluded_files] == [("blob.h", "binary")]
