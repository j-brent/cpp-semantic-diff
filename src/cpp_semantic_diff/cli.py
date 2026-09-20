"""Command line entry point.

ARCH-001 §10: JSON is the product and the terminal is a consumer. This module is argument
handling, orchestration and error reporting; it holds no presentation and no analysis.

`cppsd diff` is the shape the rest should converge on: it runs the pipeline, hands the
stage outputs to `render.build_document`, and then either prints that document or hands it
to `render.render`. Not one number on the page is computed here. STATUS §4 flags the older
commands' inline `rich` tables as the wrong shape for exactly this reason; migrating them is
a separate change, and doing it inside the one that introduces `render` would make both
harder to review.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from cpp_semantic_diff import __version__
from cpp_semantic_diff.acquire import diff_line_count, file_changes, resolve_revs
from cpp_semantic_diff.acquire.workspace import checkout
from cpp_semantic_diff.errors import PreconditionError
from cpp_semantic_diff.render import DEFAULT_LIMIT

#: Suffixes counted as C++ for the summary's `cpp_files_changed`. A reporting figure only --
#: what is actually modelled is whatever the compile database lists (`Checkout.cpp_entries`).
CPP_SUFFIXES = frozenset({".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".inl"})

app = typer.Typer(
    name="cppsd",
    help="Meaningful diffs for C++ code review.",
    no_args_is_help=True,
    add_completion=False,
)
cache_app = typer.Typer(
    name="cache",
    help="Inspect and reclaim the model cache.",
    no_args_is_help=True,
)
app.add_typer(cache_app)

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


def _fail_message(detail: str) -> None:
    """The same exit for a condition that eliminates the analysis without an exception.

    ARCH-001 §11 is about the *condition*, not about how it was discovered: an empty
    comparison has no second point, and printing an empty document for it would be degraded
    output dressed as a result.
    """
    err_console.print(f"[bold red]cannot analyze[/]\n{detail}")
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
    # Printed as its own row, and yellow when absent, because an undetermined target is not
    # an error on this host -- it is a reason not to share this host's cache with another
    # (ARCH-001 §12.1). A condition that only matters elsewhere has to be visible here.
    table.add_row("target", tc.target or "[yellow]undetermined -- do not share this cache[/]")
    table.add_row("producer id", tc.id)
    console.print(table)


def _megabytes(size: int) -> str:
    return f"{size / 1_048_576:.0f} MB"


@cache_app.command("status")
def cache_status() -> None:
    """What the cache holds, and how much of it is reclaimable."""
    from cpp_semantic_diff.acquire.workspace import cache_root
    from cpp_semantic_diff.produce.cache import DEFAULT_MAX_AGE_DAYS, collect, store_root

    sweep = collect(dry_run=True)
    worktrees = cache_root() / "worktrees"
    worktree_bytes = sum(p.stat().st_size for p in worktrees.rglob("*") if p.is_file())

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("location", str(cache_root()))
    table.add_row("TU models", f"{sweep.total:,} entries · {_megabytes(sweep.held + sweep.freed)}")
    table.add_row(
        f"unused {DEFAULT_MAX_AGE_DAYS}d+", f"{sweep.removed:,} entries · {_megabytes(sweep.freed)}"
    )
    # Reported but not swept: removing a worktree is `git worktree remove` against the
    # repository that owns it, and this command does not know which that is.
    table.add_row("worktrees", f"{_megabytes(worktree_bytes)} [dim](not collected)[/]")
    table.add_row("store", str(store_root()))
    console.print(table)


@cache_app.command("gc")
def cache_gc(
    days: Annotated[
        int,
        typer.Option("--older-than", help="Collect entries unused for this many days."),
    ] = 30,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be collected and remove nothing."),
    ] = False,
) -> None:
    """Reclaim cache entries nothing has used lately.

    `TuCache._evict` trims within one TU's directory and cannot reach the case that actually
    accumulates: every change to the extraction code gives each TU a new identity directory,
    and nothing ever writes to the old ones again (ARCH-001 §9, STATUS §6).

    Safe by construction -- an entry collected here is reparsed on the next run that wants
    it, which is the same cost as one that was never written.
    """
    from cpp_semantic_diff.produce.cache import collect

    sweep = collect(max_age_days=days, dry_run=dry_run)
    verb = "would collect" if dry_run else "collected"
    console.print(
        f"{verb} [bold]{sweep.removed:,}[/] of {sweep.total:,} entries · "
        f"{_megabytes(sweep.freed)} freed · {_megabytes(sweep.held)} kept"
    )


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

    changes = file_changes(repo, revs)
    files = changes.paths
    lines = diff_line_count(repo, revs)
    cpp = [f for f in files if Path(f).suffix.lower() in CPP_SUFFIXES]

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("comparison", revs.describe())
    table.add_row("base", f"merge-base({revs.base_ref}, {revs.head_ref})")
    table.add_row("files changed", str(len(files)))
    table.add_row("C++ files", str(len(cpp)))
    # Renames are identity input, not trivia (ARCH-001 §5): a renamed file breaks tier-1
    # identity for every internal-linkage entity in it. `plan` answers "what would be
    # analyzed" in a second, and this is now part of the answer rather than something a
    # reviewer discovers two minutes into a `diff`.
    table.add_row("files renamed", str(len(changes.renames)))
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


@app.command()
def diff(
    repo: RepoArg,
    base: BaseOpt = None,
    head: HeadOpt = "HEAD",
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Write the JSON document to stdout instead of the view."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Also write the JSON document here."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Top-level events to show. 0 shows all."),
    ] = DEFAULT_LIMIT,
    expand: Annotated[
        bool,
        typer.Option("--expand", help="Show consequences at top level instead of under causes."),
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Reparse every TU. What a cold measurement needs."),
    ] = False,
    workers: Annotated[
        int | None,
        typer.Option(
            "--workers",
            "-j",
            help=(
                "Parser processes. Default: $CPPSD_WORKERS, else this machine's cores "
                "capped at 8. 1 parses in this process, which is the serial path."
            ),
        ),
    ] = None,
) -> None:
    """Diff the semantic models at two revisions -- the whole pipeline (ARCH-001 §3).

    Produces the versioned JSON document of §10 and, by default, renders the terminal view
    of it. The two are the same document: `--json` changes which consumer runs, never what
    was analyzed.
    """
    from cpp_semantic_diff.classify import classify_events
    from cpp_semantic_diff.diff import diff_models
    from cpp_semantic_diff.match import match_models
    from cpp_semantic_diff.produce import build_model, load
    from cpp_semantic_diff.rank import rank_events
    from cpp_semantic_diff.render import Timings, build_document, render

    # Progress goes to stderr unconditionally, so `--json` gives a clean pipe on stdout and
    # a run that takes two minutes still says what it is doing.
    status = err_console
    timings = Timings()
    started = time.perf_counter()

    try:
        # The parser is a precondition, not a dependency (ARCH-001 §13). Verified before a
        # worktree is materialized, so a host that cannot parse fails in a second rather
        # than after a configure.
        toolchain = load()

        revs = resolve_revs(repo, base_ref=base, head_ref=head)
        status.print(f"[dim]comparing[/] {revs.describe()}")
        if revs.is_empty:
            _fail_message(
                f"{revs.head_ref} is the merge base of {revs.base_ref}; there is no second "
                f"point to analyze.\nCheck out the branch under review, or pass --base."
            )
            return

        changes = file_changes(repo, revs)
        files = changes.paths
        lines = diff_line_count(repo, revs)

        t = time.perf_counter()
        # Both checkouts stay open for the whole run: `Checkout` owns the worktree the
        # compile database's paths are relative to, and the models are built from it.
        with ExitStack() as stack:
            base_co = stack.enter_context(checkout(repo, revs.base_sha))
            head_co = stack.enter_context(checkout(repo, revs.head_sha))
            timings.acquire = time.perf_counter() - t

            cache = not no_cache
            models = {}
            for label, co, slot in (
                ("base", base_co, "produce_base"),
                ("head", head_co, "produce_head"),
            ):
                status.print(f"[dim]building {label} model[/] {len(co.cpp_entries)} TUs")
                t = time.perf_counter()
                # `workers` is passed through as `None` when the flag is absent, never as
                # a count computed here. `produce.parallel.worker_count` is the one place
                # that resolves flag > $CPPSD_WORKERS > what this machine can run, and a
                # default filled in at this layer would shadow the environment variable
                # for every run -- two mechanisms for one knob, disagreeing silently.
                models[label] = build_model(co, toolchain=toolchain, cache=cache, workers=workers)
                setattr(timings, slot, time.perf_counter() - t)
            base_model, head_model = models["base"], models["head"]
    except PreconditionError as error:
        _fail(error)
        return

    # From here the pipeline is pure (ARCH-001 §3): four functions over what is already in
    # memory, each the input of the next, none of them touching the disk.
    t = time.perf_counter()
    # ARCH-001 §5: renames are resolved from git *before* matching, because USRs for
    # internal-linkage and anonymous-namespace entities incorporate the file name and a
    # rename breaks tier 1 for every one of them. Git's answer is passed through unfiltered
    # -- deciding which renames count is `RenameMap`'s job, and inventing or narrowing one
    # here would put an identity decision in the CLI.
    correspondence = match_models(base_model, head_model, renames=changes.renames)
    timings.match = time.perf_counter() - t

    t = time.perf_counter()
    try:
        events = diff_models(base_model, head_model, correspondence)
    except PreconditionError as error:
        # ARCH-001 §12.2's refusal reaches the user as the §11 treatment and not as a
        # traceback. Unreachable from here today -- both models are built from the one
        # `toolchain` resolved above -- but a precondition that is presented differently
        # depending on which stage raised it is a presentation bug waiting for the first
        # caller that can trip it.
        _fail(error)
        return
    timings.diff = time.perf_counter() - t

    t = time.perf_counter()
    events = classify_events(events, base_model, head_model, correspondence)
    timings.classify = time.perf_counter() - t

    t = time.perf_counter()
    ranking = rank_events(events, base_model, head_model)
    timings.rank = time.perf_counter() - t
    timings.total = time.perf_counter() - started

    document = build_document(
        repo=repo.name,
        revs=revs,
        base=base_model,
        head=head_model,
        correspondence=correspondence,
        events=events,
        ranking=ranking,
        lines_changed=lines,
        files_changed=len(files),
        cpp_files_changed=sum(1 for f in files if Path(f).suffix.lower() in CPP_SUFFIXES),
        files_renamed=len(changes.renames),
        timings=timings,
    )

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(document.to_json(), encoding="utf-8")
        status.print(f"[dim]wrote[/] {out}")

    if as_json:
        # `print`, not `console.print`: rich would wrap and highlight the document, and a
        # product piped into `jq` must be bytes rather than a rendering of bytes.
        print(document.to_json())
        return

    render(document, console, collapse_cascades=not expand, limit=limit)


if __name__ == "__main__":
    app()
