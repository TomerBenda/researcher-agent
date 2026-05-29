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

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import typer

from researcher_agent.collect import CollectStats, PostStats, post_process, run_collect
from researcher_agent.config import AgentConfig, ConfigError, load_agent_config
from researcher_agent.http import PoliteClient
from researcher_agent.llm.base import ProviderError
from researcher_agent.llm.factory import build_classifier_provider
from researcher_agent.sources import SourceConfigError, load_adapters
from researcher_agent.state import Database

app = typer.Typer(help="researcher-agent CLI")

DEFAULT_SOURCES = Path("config/sources.yaml")
DEFAULT_AGENT = Path("config/agent.yaml")
DEFAULT_DB = Path(".researcher/state.db")


def _parse_window_dt(value: str) -> datetime:
    """Parse an ISO date/datetime for a window bound; assume UTC if naive."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _safe_source_name(name: str, log_mode: str) -> str:
    """Source slugs reveal the (private) source list, so hash them in public CI logs."""
    if log_mode != "production":
        return name
    return "src#" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def _print_summary(stats: CollectStats, *, log_mode: str = "dev") -> None:
    for o in stats.outcomes:
        name = _safe_source_name(o.name, log_mode)
        if o.error is not None:
            typer.echo(f"  ERROR  {name}: {o.error}")
        elif o.not_modified:
            typer.echo(f"  304    {name}: not modified")
        else:
            typer.echo(
                f"  ok     {name}: fetched={o.fetched} new={o.stored_new} "
                f"entities={o.entities_new} skipped={o.skipped}"
            )
    typer.echo(
        f"Collected {stats.total_new_items} new item(s) from "
        f"{len(stats.outcomes)} source(s); {stats.error_count} error(s)."
    )


def _load_agent_config(agent_file: Path, *, warn_if_missing: bool) -> AgentConfig | None:
    """Best-effort load of agent.yaml. Returns None (with a notice) if absent/invalid."""
    if not agent_file.exists():
        if warn_if_missing:
            typer.echo(
                f"classification off: {agent_file} not found "
                "(copy config/agent.example.yaml, or pass --no-classify)"
            )
        return None
    try:
        return load_agent_config(agent_file)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        return None


def _maybe_post_process(
    db: Database,
    *,
    config: AgentConfig | None,
    now: datetime,
    vault: str | None,
) -> PostStats | None:
    """Build the provider and run classify/dedupe/render — or skip cleanly."""
    if config is None:
        return None
    try:
        provider = build_classifier_provider(config.classifier)
    except ProviderError as exc:
        typer.echo(f"skipping classification: {exc}")
        return None
    return post_process(
        db, config, provider, now=now, vault_override=Path(vault) if vault else None
    )


def _print_post_summary(post: PostStats) -> None:
    if post.classify is not None:
        typer.echo(
            f"Classified {post.classify.classified} item(s) "
            f"({post.classify.fallbacks} fallback, {post.classify.skipped} over-budget); "
            f"deduped {post.deduped}."
        )
    if post.report_path is not None:
        typer.echo(f"Wrote {post.report_path}")


@app.command()
def collect(
    source: list[str] = typer.Option(
        [], "--source", help="Only run these source names (repeatable). Default: all."
    ),
    sources_file: Path = typer.Option(
        DEFAULT_SOURCES, "--sources", help="Path to the sources YAML file."
    ),
    agent_file: Path = typer.Option(
        DEFAULT_AGENT, "--agent", help="Path to the agent config (taxonomy + classifier)."
    ),
    db_path: Path = typer.Option(DEFAULT_DB, "--db", help="Path to the SQLite state DB."),
    since: str | None = typer.Option(
        None, help="Only keep items published on/after this ISO date/datetime."
    ),
    until: str | None = typer.Option(
        None, help="Only keep items published before this ISO date/datetime."
    ),
    classify: bool = typer.Option(
        True, "--classify/--no-classify", help="Classify + dedupe + render after collecting."
    ),
    vault: str | None = typer.Option(
        None, "--vault", help="Override the vault path for the collection report."
    ),
    fail_on_error: bool = typer.Option(
        False, "--fail-on-error", help="Exit non-zero if any source errors (for smoke tests)."
    ),
) -> None:
    """Gather from sources, classify, dedupe, and render a collection report."""
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

    # Load agent config up front: its tracking params affect canonicalization
    # during collection (before post-processing), and it drives classification.
    config = _load_agent_config(agent_file, warn_if_missing=classify)
    extra_tracking = config.tracking_params_to_strip if config else []

    post: PostStats | None = None
    with Database(db_path) as db, PoliteClient() as client:
        stats = run_collect(
            db,
            adapters,
            client,
            now=now,
            since=since_dt,
            until=until_dt,
            extra_tracking_params=extra_tracking,
            log_mode=log_mode,
        )
        if classify:
            post = _maybe_post_process(db, config=config, now=now, vault=vault)

    _print_summary(stats, log_mode=log_mode)
    if post is not None:
        _print_post_summary(post)
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
