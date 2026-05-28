"""CLI entry point.

Two functions over a shared substrate (SQLite store + vault):
- `collect`  — gather from sources, normalize, store. Default window is "since
  last run" (per-source cursors); explicit `--since`/`--until` windows are
  accepted. For M2 this stops at the storage layer: classification and vault
  rendering land with the classifier in M3.
- `synthesize` — read items in a window, run the synthesis agent, write a
  synthesis report. Still a stub through M5.

Periodicity is an orchestration concern (cron, GitHub Actions, manual). The
commands themselves are window-parameterized and idempotent.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import typer

from researcher_agent.collect import CollectStats, run_collect
from researcher_agent.http import PoliteClient
from researcher_agent.sources import SourceConfigError, load_adapters
from researcher_agent.state import Database

app = typer.Typer(help="researcher-agent CLI")

DEFAULT_SOURCES = Path("config/sources.yaml")
DEFAULT_DB = Path(".researcher/state.db")


def _parse_window_dt(value: str) -> datetime:
    """Parse an ISO date/datetime for a window bound; assume UTC if naive."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _print_summary(stats: CollectStats) -> None:
    for o in stats.outcomes:
        if o.error is not None:
            typer.echo(f"  ERROR  {o.name}: {o.error}")
        elif o.not_modified:
            typer.echo(f"  304    {o.name}: not modified")
        else:
            typer.echo(
                f"  ok     {o.name}: fetched={o.fetched} new={o.stored_new} "
                f"entities={o.entities_new} skipped={o.skipped}"
            )
    typer.echo(
        f"Collected {stats.total_new_items} new item(s) from "
        f"{len(stats.outcomes)} source(s); {stats.error_count} error(s)."
    )


@app.command()
def collect(
    source: list[str] = typer.Option(
        [], "--source", help="Only run these source names (repeatable). Default: all."
    ),
    sources_file: Path = typer.Option(
        DEFAULT_SOURCES, "--sources", help="Path to the sources YAML file."
    ),
    db_path: Path = typer.Option(DEFAULT_DB, "--db", help="Path to the SQLite state DB."),
    since: str | None = typer.Option(
        None, help="Only keep items published on/after this ISO date/datetime."
    ),
    until: str | None = typer.Option(
        None, help="Only keep items published before this ISO date/datetime."
    ),
    fail_on_error: bool = typer.Option(
        False, "--fail-on-error", help="Exit non-zero if any source errors (for smoke tests)."
    ),
) -> None:
    """Gather from sources, normalize, and store. (M2: no classify / vault yet.)"""
    if not sources_file.exists():
        typer.echo(f"sources file not found: {sources_file}", err=True)
        raise typer.Exit(code=2)

    try:
        adapters = load_adapters(sources_file)
    except SourceConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        since_dt = _parse_window_dt(since) if since else None
        until_dt = _parse_window_dt(until) if until else None
    except ValueError as exc:
        typer.echo(f"invalid date: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if source:
        wanted = set(source)
        adapters = [a for a in adapters if a.config.name in wanted]

    if not adapters:
        typer.echo("no sources to collect from.")
        raise typer.Exit(code=0)

    log_mode = os.environ.get("RESEARCHER_LOG_MODE", "dev")
    now = datetime.now(UTC)

    with Database(db_path) as db, PoliteClient() as client:
        stats = run_collect(
            db,
            adapters,
            client,
            now=now,
            since=since_dt,
            until=until_dt,
            log_mode=log_mode,
        )

    _print_summary(stats)
    if fail_on_error and stats.any_errors:
        raise typer.Exit(code=1)


@app.command()
def synthesize(
    since: str | None = typer.Option(
        None, help="Window start (ISO). Default: 7 days before --until."
    ),
    until: str | None = typer.Option(None, help="Window end (ISO). Default: now."),
    days: int | None = typer.Option(
        None, help="Shorthand: N trailing days ending at --until. Overrides --since."
    ),
    label: str | None = typer.Option(None, help="Override window label used in filename/heading."),
    write_vault: bool = typer.Option(
        True,
        "--write-vault/--no-write-vault",
        help="Write a synthesis report to the vault.",
    ),
) -> None:
    """Read items in a window, run the synthesis agent, render a report. Not yet implemented."""
    typer.echo("synthesize: not yet implemented (M5)")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
