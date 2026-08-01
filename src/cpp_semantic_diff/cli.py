"""Command line entry point.

ARCH-001 §10: JSON is the product and the terminal is a consumer. Commands here render;
they do not analyze.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from cpp_semantic_diff import __version__
from cpp_semantic_diff.acquire import changed_files, diff_line_count, resolve_revs
from cpp_semantic_diff.acquire.workspace import checkout
from cpp_semantic_diff.errors import PreconditionError

app = typer.Typer(
    name="cppsd",
    help="Meaningful diffs for C++ code review.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

RepoArg = Annotated[Path, typer.Argument(help="Path to the C++ repository.")]
BaseOpt = Annotated[
    str | None,
    typer.Option("--base", "-b", help="Base ref. Defaults to the repository's main branch."),
]
HeadOpt = Annotated[str, typer.Option("--head", "-h", help="Head ref.")]


def _fail(error: PreconditionError) -> None:
    """Precondition failures are terminal and must be actionable (ARCH-001 §11)."""
    err_console.print(f"[bold red]cannot analyze[/]\n{error}")
    raise typer.Exit(code=2)


@app.command()
def version() -> None:
    console.print(f"cppsd {__version__}")


@app.command()
def doctor() -> None:
    """Check that this host can produce trustworthy models.

    Worth its own command because the failure it catches is invisible at runtime: a
    libclang without builtin headers returns truncated ASTs rather than raising, and two
    truncated models agree with each other perfectly (ARCH-001 §11).
    """
    from cpp_semantic_diff.produce import candidate_libraries, load

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()

    try:
        tc = load()
    except PreconditionError as error:
        found = candidate_libraries()
        table.add_row("libclang", "[red]unusable[/]")
        table.add_row("candidates", str(len(found)))
        console.print(table)
        _fail(error)
        return

    table.add_row("libclang", str(tc.library))
    table.add_row("version", tc.version)
    if tc.resource_dir:
        table.add_row("resource dir", f"{tc.resource_dir} [dim](CPPSD_RESOURCE_DIR)[/]")
    table.add_row("builtin headers", "[green]parsed clean[/]")
    table.add_row("producer id", tc.id)
    console.print(table)


@app.command()
def plan(repo: RepoArg, base: BaseOpt = None, head: HeadOpt = "HEAD") -> None:
    """Resolve what would be analyzed, without analyzing it.

    Reports the comparison the tool would make and the size of the text diff it has to
    beat -- the denominator of the compression metric (ARCH-001 §10).
    """
    try:
        revs = resolve_revs(repo, base_ref=base, head_ref=head)
    except PreconditionError as error:
        _fail(error)
        return

    files = changed_files(repo, revs)
    lines = diff_line_count(repo, revs)
    cpp = [f for f in files if Path(f).suffix.lower() in {".cpp", ".cc", ".cxx", ".h", ".hpp"}]

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("comparison", revs.describe())
    table.add_row("base", f"merge-base({revs.base_ref}, {revs.head_ref})")
    table.add_row("files changed", str(len(files)))
    table.add_row("C++ files", str(len(cpp)))
    table.add_row("lines changed", str(lines))
    console.print(table)

    if revs.is_empty:
        console.print("\n[yellow]head is the merge base -- nothing to review.[/]")


@app.command()
def acquire(repo: RepoArg, base: BaseOpt = None, head: HeadOpt = "HEAD") -> None:
    """Materialize both revisions and report their compile databases.

    Configure, not build. The base worktree is cached by SHA and shared across every
    review against it (ARCH-001 §9).
    """
    try:
        revs = resolve_revs(repo, base_ref=base, head_ref=head)
        console.print(f"[dim]resolving[/] {revs.describe()}")

        for label, rev in (("base", revs.base_sha), ("head", revs.head_sha)):
            with checkout(repo, rev) as co:
                total = len(co.compile_db)
                cpp = len(co.cpp_entries)
                console.print(
                    f"  [green]{label:4}[/] {rev[:12]}  "
                    f"{cpp} C++ TUs of {total} entries  [dim]{co.path}[/]"
                )
    except PreconditionError as error:
        _fail(error)


if __name__ == "__main__":
    app()
