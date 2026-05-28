"""Tests for the Pydantic models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from researcher_agent.models import (
    Classification,
    Followup,
    Item,
    ItemBody,
    ItemEntity,
    ItemSource,
    RawItem,
    SourceRun,
    WeeklyEntity,
)

HASH = "a" * 64


def _now() -> datetime:
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


# --- Item ---------------------------------------------------------------------


def test_item_accepts_minimal() -> None:
    item = Item(
        canonical_hash=HASH,
        url="https://example.com/post",
        title="hello",
        ingested_at=_now(),
    )
    assert item.summary is None
    assert item.published_at is None
    assert item.metadata == {}
    assert item.canonicalization_version == 1


def test_item_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        Item(
            canonical_hash=HASH,
            url="https://example.com",
            title="t",
            ingested_at=datetime(2026, 5, 27, 12, 0, 0),  # naive
        )


def test_item_rejects_bad_url_scheme() -> None:
    with pytest.raises(ValidationError):
        Item(
            canonical_hash=HASH,
            url="ftp://example.com",
            title="t",
            ingested_at=_now(),
        )


def test_item_rejects_wrong_hash_length() -> None:
    with pytest.raises(ValidationError):
        Item(
            canonical_hash="short",
            url="https://example.com",
            title="t",
            ingested_at=_now(),
        )


def test_item_coerces_to_utc() -> None:
    from datetime import timedelta
    from datetime import timezone as tz

    plus_two = tz(timedelta(hours=2))
    dt = datetime(2026, 5, 27, 14, 0, 0, tzinfo=plus_two)
    item = Item(
        canonical_hash=HASH,
        url="https://example.com",
        title="t",
        ingested_at=dt,
    )
    assert item.ingested_at.tzinfo == UTC
    assert item.ingested_at.hour == 12


# --- Classification -----------------------------------------------------------


def test_classification_score_bounds() -> None:
    with pytest.raises(ValidationError):
        Classification(
            canonical_hash=HASH,
            topic="t",
            score=11,
            rationale="r",
            classifier_version="v",
            classifier_model="m",
            classified_at=_now(),
        )
    with pytest.raises(ValidationError):
        Classification(
            canonical_hash=HASH,
            topic="t",
            score=-1,
            rationale="r",
            classifier_version="v",
            classifier_model="m",
            classified_at=_now(),
        )


def test_classification_defaults_empty_secondary() -> None:
    c = Classification(
        canonical_hash=HASH,
        topic="mcp-security",
        score=7,
        rationale="r",
        classifier_version="v1",
        classifier_model="gemini:flash",
        classified_at=_now(),
    )
    assert c.secondary_topics == []


# --- WeeklyEntity -------------------------------------------------------------


def test_weekly_entity_requires_monday_midnight() -> None:
    # 2026-05-25 is a Monday
    monday = datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC)
    e = WeeklyEntity(week_starting=monday, kind="cve", value="CVE-2026-1", context="x")
    assert e.week_starting == monday

    # Wednesday — should reject
    wed = datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        WeeklyEntity(week_starting=wed, kind="cve", value="x", context="x")

    # Monday but not midnight — should reject
    not_midnight = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        WeeklyEntity(week_starting=not_midnight, kind="cve", value="x", context="x")


# --- Followup -----------------------------------------------------------------


def test_followup_requires_snapshots() -> None:
    f = Followup(
        created_at=_now(),
        item_hash=HASH,
        item_title_snapshot="hello",
        item_url_snapshot="https://example.com",
        action="read-deep",
    )
    assert f.note == ""
    assert f.completed is False
    assert f.id is None


def test_followup_action_validation() -> None:
    with pytest.raises(ValidationError):
        Followup(
            created_at=_now(),
            item_hash=HASH,
            item_title_snapshot="t",
            item_url_snapshot="https://example.com",
            action="unknown-action",  # type: ignore[arg-type]
        )


# --- ItemEntity / ItemSource / ItemBody / RawItem ----------------------------


def test_item_entity_kind_validation() -> None:
    e = ItemEntity(canonical_hash=HASH, kind="cve", value="CVE-2026-1")
    assert e.context is None
    with pytest.raises(ValidationError):
        ItemEntity(canonical_hash=HASH, kind="unknown", value="x")  # type: ignore[arg-type]


def test_item_source_roundtrip() -> None:
    s = ItemSource(
        canonical_hash=HASH,
        source_name="rss:foo",
        source_type="rss",
        external_id="guid-1",
        first_seen_at=_now(),
    )
    assert s.first_seen_at.tzinfo == UTC


def test_item_body_roundtrip() -> None:
    b = ItemBody(canonical_hash=HASH, body="hello", fetched_at=_now())
    assert b.fetched_from_url is None


def test_raw_item_roundtrip() -> None:
    r = RawItem(
        source_name="rss:foo",
        source_type="rss",
        external_id="guid-1",
        payload={"title": "t"},
        fetched_at=_now(),
    )
    assert r.payload == {"title": "t"}


# --- SourceRun ---------------------------------------------------------------


def test_source_run_defaults() -> None:
    r = SourceRun(source_name="rss:foo")
    assert r.cursor == {}
    assert r.consecutive_empty_runs == 0
    assert r.last_run_at is None
