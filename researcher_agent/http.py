"""Shared, polite HTTP client for all source adapters.

Politeness is mandatory (CLAUDE.md invariant #16): one descriptive User-Agent,
serialized requests per host with a configurable minimum interval, `Retry-After`
honored on 429, and caller-supplied conditional-GET headers passed through so
adapters can do 304-cached fetches.

The clock and sleep functions are injectable so rate-limit timing is testable
without real waits.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from types import TracebackType
from urllib.parse import urlsplit

import httpx

DEFAULT_USER_AGENT = "researcher-agent/0.1 (+https://github.com/TomerBenda/researcher-agent)"


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header into seconds-to-wait, or None if absent/invalid.

    Accepts both the integer-seconds form and the HTTP-date form. The date form
    is compared against the wall clock (`time.time`) and floored at 0; callers
    clamp the result to a sane maximum.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


class PoliteClient:
    """A thin, rate-limited wrapper around httpx.Client."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval_seconds: float = 1.0,
        max_retries: int = 2,
        max_retry_after_seconds: float = 60.0,
        default_retry_after_seconds: float = 2.0,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            transport=transport,
            limits=httpx.Limits(max_keepalive_connections=1),
            follow_redirects=True,
        )
        self._min_interval = min_interval_seconds
        self._max_retries = max_retries
        self._max_retry_after = max_retry_after_seconds
        self._default_retry_after = default_retry_after_seconds
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def _respect_interval(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is None:
            return
        remaining = self._min_interval - (self._clock() - last)
        if remaining > 0:
            self._sleep(remaining)

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
        """GET `url`, honoring per-host spacing and Retry-After. Returns the response.

        Non-2xx responses (including a 429 that outlived its retries) are returned
        as-is; the caller decides how to react.
        """
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"refusing non-http(s) url: {url!r}")
        host = urlsplit(url).hostname or ""

        with self._lock_for(host):
            attempts = 0
            while True:
                self._respect_interval(host)
                # Record the start time so a slow request counts against the
                # interval (rate-limit on request starts, not a fixed cooldown).
                self._last_request_at[host] = self._clock()
                response = self._client.get(url, headers=dict(headers) if headers else None)

                if response.status_code == 429 and attempts < self._max_retries:
                    wait = _parse_retry_after(response.headers.get("Retry-After"))
                    # No Retry-After header still means "slow down" — back off a
                    # bounded default (some APIs, e.g. arXiv, 429 without one).
                    if wait is None:
                        wait = self._default_retry_after
                    # But never honor an unreasonably long wait — a broken/hostile
                    # server must not stall the whole serial run.
                    if wait <= self._max_retry_after:
                        self._sleep(wait)
                        attempts += 1
                        continue
                return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
