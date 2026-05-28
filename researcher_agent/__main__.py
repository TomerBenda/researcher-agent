"""CLI entry point.

Two functions over a shared substrate (SQLite store + vault):
- `collect`  — gather from sources, classify, dedupe, store, optionally write a
  collection report. Default window is "since last run", but explicit windows
  are accepted.
- `synthesize` — read items in a window, run the synthesis agent, write a
  synthesis report. Default window is the past 7 days, but any window works.

Periodicity is an orchestration concern (cron, GitHub Actions, manual). The
commands themselves are window-parameterized and idempotent.

Both commands are stubs in M1 — implementations land in later milestones.
"""

from __future__ import annotations

import typer

app = typer.Typer(help="researcher-agent CLI")


@app.command()
def collect(
    since: str | None = typer.Option(
        None, help="Only fetch items after this ISO date/datetime. Default: per-source cursor."
    ),
    until: str | None = typer.Option(
        None, help="Only fetch items before this ISO date/datetime. Default: now."
    ),
    write_vault: bool = typer.Option(
        True,
        "--write-vault/--no-write-vault",
        help="Write a collection report to the vault.",
    ),
) -> None:
    """Gather, classify, dedupe, store. Not yet implemented."""
    typer.echo("collect: not yet implemented (M2-M4)")
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
