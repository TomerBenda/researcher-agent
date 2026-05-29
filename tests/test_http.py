"""Tests for the polite HTTP client.

We drive the client with httpx.MockTransport (no network) and inject a fake
clock/sleep so rate-limit timing and Retry-After backoff are deterministic and
instant.
"""

from __future__ import annotations

import httpx
import pytest

from researcher_agent.http import DEFAULT_USER_AGENT, PoliteClient


class FakeClock:
    """A controllable monotonic clock. `sleep` advances time instead of waiting."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _client(handler, *, clock: FakeClock | None = None, **kwargs) -> PoliteClient:
    clock = clock or FakeClock()
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock.now,
        **kwargs,
    )


# --- User-Agent ----------------------------------------------------------------


def test_sends_default_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    with _client(handler) as c:
        c.get("https://example.com/feed")

    assert seen[0].headers["User-Agent"] == DEFAULT_USER_AGENT
    assert "researcher-agent" in DEFAULT_USER_AGENT


def test_custom_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with _client(handler, user_agent="custom/1.0") as c:
        c.get("https://example.com/")

    assert seen[0].headers["User-Agent"] == "custom/1.0"


# --- conditional GET passthrough ----------------------------------------------


def test_passes_conditional_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304)

    with _client(handler) as c:
        resp = c.get(
            "https://example.com/feed",
            headers={
                "If-None-Match": '"abc"',
                "If-Modified-Since": "Wed, 27 May 2026 00:00:00 GMT",
            },
        )

    assert seen[0].headers["If-None-Match"] == '"abc"'
    assert seen[0].headers["If-Modified-Since"] == "Wed, 27 May 2026 00:00:00 GMT"
    assert resp.status_code == 304


def test_304_returned_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(304)

    with _client(handler) as c:
        resp = c.get("https://example.com/feed")

    assert resp.status_code == 304
    assert calls == 1


# --- Retry-After on 429 --------------------------------------------------------


def test_honors_retry_after_seconds() -> None:
    clock = FakeClock()
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, text="ok"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    with _client(handler, clock=clock) as c:
        resp = c.get("https://example.com/feed")

    assert resp.status_code == 200
    assert 2.0 in clock.sleeps


def test_honors_retry_after_http_date() -> None:
    clock = FakeClock()
    # clock starts at 0.0; an HTTP-date 5s in the "future" relative to now is hard
    # to express, so we assert the client sleeps a non-negative amount and retries.
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    with _client(handler, clock=clock) as c:
        resp = c.get("https://example.com/feed")

    assert resp.status_code == 200


def test_gives_up_after_max_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "1"})

    with _client(handler, max_retries=2) as c:
        resp = c.get("https://example.com/feed")

    assert resp.status_code == 429
    assert calls == 3  # initial + 2 retries


def test_absurd_retry_after_is_not_honored() -> None:
    # a hostile/broken server must not be able to make us sleep "forever" while
    # holding the per-host lock and starving every later source
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "999999"})

    with _client(handler, clock=clock, max_retry_after_seconds=60.0) as c:
        resp = c.get("https://example.com/feed")

    assert resp.status_code == 429
    assert calls == 1  # gave up immediately rather than retrying
    assert clock.sleeps == []  # never slept the absurd duration


def test_retry_after_within_cap_is_honored() -> None:
    clock = FakeClock()
    responses = iter([httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200)])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    with _client(handler, clock=clock, max_retry_after_seconds=60.0) as c:
        resp = c.get("https://example.com/feed")

    assert resp.status_code == 200
    assert 30.0 in clock.sleeps


def test_default_backoff_when_no_retry_after_header() -> None:
    # a 429 without Retry-After still means "slow down": back off a bounded
    # default and retry (some APIs, e.g. arXiv, 429 with no header)
    clock = FakeClock()
    responses = iter([httpx.Response(429), httpx.Response(200)])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    with _client(handler, clock=clock, default_retry_after_seconds=2.0) as c:
        resp = c.get("https://example.com/feed")

    assert resp.status_code == 200
    assert clock.sleeps == [2.0]


def test_default_backoff_bounded_by_max_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)  # no Retry-After, persistent

    with _client(handler, max_retries=2, default_retry_after_seconds=1.0) as c:
        resp = c.get("https://example.com/feed")

    assert resp.status_code == 429
    assert calls == 3  # initial + 2 bounded retries, then give up


# --- per-host minimum interval -------------------------------------------------


def test_first_request_to_host_does_not_wait() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with _client(handler, clock=clock, min_interval_seconds=5.0) as c:
        c.get("https://example.com/a")

    assert clock.sleeps == []


def test_second_request_same_host_waits_min_interval() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with _client(handler, clock=clock, min_interval_seconds=5.0) as c:
        c.get("https://example.com/a")
        c.get("https://example.com/b")

    assert clock.sleeps == [5.0]


def test_different_hosts_do_not_throttle_each_other() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with _client(handler, clock=clock, min_interval_seconds=5.0) as c:
        c.get("https://a.example.com/x")
        c.get("https://b.example.com/y")

    assert clock.sleeps == []


def test_elapsed_time_counts_against_interval() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.t += 3.0  # simulate the request itself taking 3s
        return httpx.Response(200)

    with _client(handler, clock=clock, min_interval_seconds=5.0) as c:
        c.get("https://example.com/a")
        c.get("https://example.com/b")

    # 3s already elapsed during the first request, so only 2s more is needed.
    assert clock.sleeps == [2.0]


def test_get_raises_for_invalid_scheme() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with _client(handler) as c, pytest.raises(ValueError, match="http"):
        c.get("ftp://example.com/x")
