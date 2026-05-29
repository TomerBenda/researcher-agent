"""Tests for the CLI's top-level error-redaction guard (`main`).

An unhandled exception's traceback can embed a private feed URL or hostname; in
production log mode that would leak into the public Actions log. `main` must
swallow the message body there (keeping only the exception class + a hash) while
re-raising normally in dev so the developer still sees the full error.
"""

from __future__ import annotations

import pytest

from researcher_agent import __main__ as cli

SECRET = "https://secret-feed.example/private-list.xml"


def test_main_redacts_unexpected_error_in_production(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> None:
        raise RuntimeError(f"connect failed to {SECRET}")

    monkeypatch.setattr(cli, "app", boom)
    monkeypatch.setenv("RESEARCHER_LOG_MODE", "production")

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert SECRET not in err  # the leaking URL must not reach the log
    assert "secret-feed" not in err
    assert "RuntimeError" in err  # class name is fine to keep
    assert "redacted" in err


def test_main_reraises_in_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("verbose dev detail")

    monkeypatch.setattr(cli, "app", boom)
    monkeypatch.setenv("RESEARCHER_LOG_MODE", "dev")

    with pytest.raises(RuntimeError, match="verbose dev detail"):
        cli.main()


def test_main_passes_through_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    # a normal typer.Exit(0) (SystemExit) must not be swallowed/redacted
    def clean() -> None:
        raise SystemExit(0)

    monkeypatch.setattr(cli, "app", clean)
    monkeypatch.setenv("RESEARCHER_LOG_MODE", "production")

    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
