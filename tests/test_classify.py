"""Tests for the classifier orchestration (batching, retry, budget, fallback)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from researcher_agent.classify import (
    FALLBACK_SCORE,
    FALLBACK_TOPIC,
    classify_inputs,
)
from researcher_agent.llm.base import ClassifierInput, ProviderError, RawClassification

TAXONOMY = {"mcp-security", "prompt-injection", "tooling", "other"}

Responder = Callable[[list[ClassifierInput], float], dict[str, RawClassification]]


class FakeProvider:
    model_id = "fake:test"

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.calls: list[tuple[list[str], float]] = []

    def classify(
        self, system_prompt: str, inputs: Sequence[ClassifierInput], *, temperature: float
    ) -> dict[str, RawClassification]:
        inputs = list(inputs)
        self.calls.append(([i.id for i in inputs], temperature))
        return self._responder(inputs, temperature)


def _inputs(n: int) -> list[ClassifierInput]:
    return [ClassifierInput(id=str(i), title=f"t{i}", url=f"https://e.com/{i}") for i in range(n)]


def _rc(topic: str = "tooling", score: int = 5, **kw: object) -> RawClassification:
    return RawClassification(topic=topic, score=score, rationale="r", **kw)  # type: ignore[arg-type]


def _all_valid(inputs: list[ClassifierInput], temperature: float) -> dict[str, RawClassification]:
    return {i.id: _rc() for i in inputs}


def _run(provider: FakeProvider, inputs: list[ClassifierInput], **kw: object):  # type: ignore[no-untyped-def]
    return classify_inputs(
        inputs,
        provider=provider,
        system_prompt="SYS",
        valid_topics=TAXONOMY,
        **kw,  # type: ignore[arg-type]
    )


# --- happy path ----------------------------------------------------------------


def test_classifies_all_items() -> None:
    p = FakeProvider(_all_valid)
    out = _run(p, _inputs(3))
    assert set(out.classifications) == {"0", "1", "2"}
    assert out.fallback_ids == set()
    assert out.skipped_ids == []


def test_batches_by_batch_size() -> None:
    p = FakeProvider(_all_valid)
    _run(p, _inputs(25), batch_size=10)
    # 3 batches, all valid first try -> exactly 3 calls (no retries)
    assert len(p.calls) == 3
    assert [len(ids) for ids, _ in p.calls] == [10, 10, 5]


# --- retry ---------------------------------------------------------------------


def test_invalid_topic_triggers_retry_then_accepts() -> None:
    def responder(inputs: list[ClassifierInput], temp: float) -> dict[str, RawClassification]:
        if temp > 0:  # first pass: item "1" gets a bogus topic
            return {i.id: _rc(topic="bogus" if i.id == "1" else "tooling") for i in inputs}
        return {i.id: _rc(topic="mcp-security") for i in inputs}  # retry fixes it

    p = FakeProvider(responder)
    out = _run(p, _inputs(3))
    assert out.fallback_ids == set()
    assert out.classifications["1"].topic == "mcp-security"


def test_retry_is_per_item_not_per_batch() -> None:
    def responder(inputs: list[ClassifierInput], temp: float) -> dict[str, RawClassification]:
        if temp > 0:
            return {i.id: _rc() for i in inputs if i.id != "2"}  # "2" missing first try
        return {i.id: _rc(topic="other") for i in inputs}

    p = FakeProvider(responder)
    _run(p, _inputs(5))
    # second call (the retry) must contain ONLY the one pending item
    assert p.calls[1][0] == ["2"]
    assert p.calls[1][1] == 0.0


def test_retry_at_temperature_zero() -> None:
    calls_temps: list[float] = []

    def responder(inputs: list[ClassifierInput], temp: float) -> dict[str, RawClassification]:
        calls_temps.append(temp)
        return {} if temp > 0 else {i.id: _rc() for i in inputs}

    p = FakeProvider(responder)
    _run(p, _inputs(1), initial_temperature=0.3)
    assert calls_temps[0] == 0.3
    assert calls_temps[1] == 0.0


# --- fallback ------------------------------------------------------------------


def test_persistent_failure_falls_back() -> None:
    p = FakeProvider(lambda inputs, temp: {})  # never returns anything
    out = _run(p, _inputs(2))
    assert out.fallback_ids == {"0", "1"}
    assert out.classifications["0"].topic == FALLBACK_TOPIC
    assert out.classifications["0"].score == FALLBACK_SCORE


def test_provider_error_skips_not_fallback() -> None:
    # a transient provider failure must NOT persist junk (other,3) labels; leave
    # the items unclassified so the next run re-tries them
    def boom(inputs: list[ClassifierInput], temp: float) -> dict[str, RawClassification]:
        raise ProviderError("api down")

    p = FakeProvider(boom)
    out = _run(p, _inputs(2))
    assert out.fallback_ids == set()
    assert set(out.skipped_ids) == {"0", "1"}


def test_circuit_breaker_aborts_after_consecutive_failures() -> None:
    # a rate-limit storm must not burn the whole backlog into the API
    def boom(inputs: list[ClassifierInput], temp: float) -> dict[str, RawClassification]:
        raise ProviderError("rate limited")

    p = FakeProvider(boom)
    out = _run(p, _inputs(50), batch_size=10, max_consecutive_failures=3)
    assert len(p.calls) <= 4  # stopped early, did not call for all 5 batches
    assert set(out.skipped_ids) == {str(i) for i in range(50)}
    assert out.fallback_ids == set()


# --- token budget --------------------------------------------------------------


def test_budget_skips_remaining_batches() -> None:
    p = FakeProvider(_all_valid)
    # tiny budget: afford the first batch's estimate but not the second
    out = _run(p, _inputs(20), batch_size=10, token_budget=1, estimate=_const_estimate(1))
    # first batch costs 1 (budget exactly 1), second batch can't be afforded
    assert set(out.classifications) == {str(i) for i in range(10)}
    assert out.skipped_ids == [str(i) for i in range(10, 20)]


def test_budget_too_small_for_retry_falls_back() -> None:
    # first call costs the whole budget; the retry can't be afforded -> fallback
    def responder(inputs: list[ClassifierInput], temp: float) -> dict[str, RawClassification]:
        return {}  # everything pending after first call

    p = FakeProvider(responder)
    out = _run(p, _inputs(2), token_budget=10, estimate=_const_estimate(10))
    assert out.fallback_ids == {"0", "1"}
    assert len(p.calls) == 1  # retry never attempted (no budget)


def test_first_batch_runs_even_if_over_budget() -> None:
    # A single batch whose estimate exceeds the WHOLE budget must still be
    # attempted; otherwise the same items skip on every run and the pipeline
    # wedges forever. Forward progress beats a permanent stall.
    p = FakeProvider(_all_valid)
    out = _run(p, _inputs(3), batch_size=10, token_budget=1, estimate=_const_estimate(100))
    assert set(out.classifications) == {"0", "1", "2"}
    assert out.skipped_ids == []


def test_over_budget_first_batch_still_defers_later_batches() -> None:
    # The forced first batch runs, but the budget still gates every later batch so
    # one run can't blow far past the budget.
    p = FakeProvider(_all_valid)
    out = _run(p, _inputs(20), batch_size=10, token_budget=1, estimate=_const_estimate(100))
    assert set(out.classifications) == {str(i) for i in range(10)}
    assert out.skipped_ids == [str(i) for i in range(10, 20)]


def test_no_budget_means_unlimited() -> None:
    p = FakeProvider(_all_valid)
    out = _run(p, _inputs(50), batch_size=10, token_budget=None)
    assert len(out.classifications) == 50
    assert out.skipped_ids == []


# --- secondary topic cleaning --------------------------------------------------


def test_invalid_secondary_topics_filtered() -> None:
    def responder(inputs: list[ClassifierInput], temp: float) -> dict[str, RawClassification]:
        return {
            i.id: _rc(topic="tooling", secondary_topics=["mcp-security", "bogus", "tooling"])
            for i in inputs
        }

    p = FakeProvider(responder)
    out = _run(p, _inputs(1))
    sec = out.classifications["0"].secondary_topics
    assert "bogus" not in sec
    assert "tooling" not in sec  # primary not duplicated into secondaries
    assert sec == ["mcp-security"]


def _const_estimate(value: int) -> Callable[[str, Sequence[ClassifierInput]], int]:
    def estimate(system_prompt: str, inputs: Sequence[ClassifierInput]) -> int:
        return value

    return estimate


def test_empty_inputs() -> None:
    p = FakeProvider(_all_valid)
    out = _run(p, [])
    assert out.classifications == {}
    assert p.calls == []
