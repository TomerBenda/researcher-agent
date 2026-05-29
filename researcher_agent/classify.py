"""Classifier orchestration: batch items, retry, budget, fall back.

Provider-agnostic (depends only on the ClassifierProvider Protocol) so it is
fully testable with a fake. Behavior enforced here:

- Batching by `batch_size` (spec §5.3).
- Per-item retry, not per-batch (invariant #18): only the items that came back
  missing or with an out-of-taxonomy topic are retried, at temperature 0.
- Token-budget pre-flight before every provider call (invariant #19): when the
  next call would exceed the remaining budget, classification degrades
  gracefully (remaining items are reported as skipped) rather than overrunning.
- No silent swallow: an item that fails twice falls back to (other, 3) and is
  counted in `fallback_ids`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from researcher_agent.llm.base import (
    ClassifierInput,
    ClassifierProvider,
    ProviderError,
    RawClassification,
    render_items_message,
)

FALLBACK_TOPIC = "other"
FALLBACK_SCORE = 3

TokenEstimator = Callable[[str, Sequence[ClassifierInput]], int]


@dataclass(frozen=True)
class ClassifyOutcome:
    classifications: dict[str, RawClassification]  # id -> final result (topic in taxonomy)
    fallback_ids: set[str]  # items that fell back to (FALLBACK_TOPIC, FALLBACK_SCORE)
    skipped_ids: list[str]  # items not attempted because the token budget ran out


def estimate_tokens(system_prompt: str, inputs: Sequence[ClassifierInput]) -> int:
    """Cheap pre-flight token estimate (~4 chars/token) for budget checks."""
    chars = len(system_prompt) + len(render_items_message(inputs))
    return chars // 4 + 16


def _fallback(fallback_topic: str) -> RawClassification:
    return RawClassification(
        topic=fallback_topic,
        score=FALLBACK_SCORE,
        rationale="classifier produced no valid label; default applied",
    )


def _clean_secondaries(rc: RawClassification, valid_topics: set[str]) -> RawClassification:
    """Drop secondary topics that are invalid or duplicate the primary."""
    cleaned: list[str] = []
    for slug in rc.secondary_topics:
        if slug in valid_topics and slug != rc.topic and slug not in cleaned:
            cleaned.append(slug)
    if cleaned == rc.secondary_topics:
        return rc
    return rc.model_copy(update={"secondary_topics": cleaned})


def _safe_classify(
    provider: ClassifierProvider,
    system_prompt: str,
    inputs: Sequence[ClassifierInput],
    temperature: float,
) -> tuple[dict[str, RawClassification], bool]:
    """Return (results, errored). `errored` distinguishes a transient provider
    failure (no response) from a response that merely classified nothing."""
    try:
        return provider.classify(system_prompt, inputs, temperature=temperature), False
    except ProviderError:
        return {}, True


def classify_inputs(
    inputs: Sequence[ClassifierInput],
    *,
    provider: ClassifierProvider,
    system_prompt: str,
    valid_topics: set[str],
    batch_size: int = 10,
    initial_temperature: float = 0.2,
    token_budget: int | None = None,
    estimate: TokenEstimator = estimate_tokens,
    fallback_topic: str = FALLBACK_TOPIC,
    max_consecutive_failures: int = 3,
) -> ClassifyOutcome:
    """Classify `inputs`, returning final labels plus fallback/skipped bookkeeping.

    Failure handling separates two cases so a rate-limit/outage never poisons the
    DB with junk labels:
    - the provider RESPONDED but couldn't validly classify an item -> fall back to
      (fallback_topic, 3) and persist it (a genuine content miss);
    - the provider ERRORED (no response) -> leave the item unclassified (skipped),
      to be re-tried next run. After `max_consecutive_failures` failed provider
      calls in a row the run aborts, skipping the rest rather than hammering a
      down/throttled API.
    """
    items = list(inputs)
    classifications: dict[str, RawClassification] = {}
    fallback_ids: set[str] = set()
    skipped_ids: list[str] = []
    remaining = token_budget
    consecutive_failures = 0

    def afford(cost: int) -> bool:
        nonlocal remaining
        if remaining is None:
            return True
        if cost > remaining:
            return False
        remaining -= cost
        return True

    def note(errored: bool) -> None:
        nonlocal consecutive_failures
        consecutive_failures = consecutive_failures + 1 if errored else 0

    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
    aborted = False
    attempted = False
    for idx, batch in enumerate(batches):
        if aborted:
            skipped_ids.extend(inp.id for later in batches[idx:] for inp in later)
            break
        if not afford(estimate(system_prompt, batch)):
            if attempted:
                # Budget exhausted after real work this run; defer the rest to a
                # later run rather than overrunning.
                skipped_ids.extend(inp.id for later in batches[idx:] for inp in later)
                break
            # First batch alone exceeds the whole run budget (a misconfig, or one
            # huge item). Attempt it anyway so we make forward progress instead of
            # skipping the same items on every run forever; zero the budget so
            # every later batch is still gated.
            remaining = 0
        attempted = True

        raw, initial_err = _safe_classify(provider, system_prompt, batch, initial_temperature)
        note(initial_err)
        got_response = not initial_err

        pending: list[ClassifierInput] = []
        for inp in batch:
            rc = raw.get(inp.id)
            if rc is not None and rc.topic in valid_topics:
                classifications[inp.id] = _clean_secondaries(rc, valid_topics)
            else:
                pending.append(inp)

        if pending:
            if afford(estimate(system_prompt, pending)):
                retry, retry_err = _safe_classify(provider, system_prompt, pending, 0.0)
                note(retry_err)
                got_response = got_response or not retry_err
            else:
                retry = {}  # budget blocked the retry (not a provider failure)

            for inp in pending:
                rc = retry.get(inp.id)
                if rc is not None and rc.topic in valid_topics:
                    classifications[inp.id] = _clean_secondaries(rc, valid_topics)
                elif got_response:
                    classifications[inp.id] = _fallback(fallback_topic)
                    fallback_ids.add(inp.id)
                else:
                    skipped_ids.append(inp.id)  # transient: never got a usable response

        if consecutive_failures >= max_consecutive_failures:
            aborted = True

    return ClassifyOutcome(
        classifications=classifications,
        fallback_ids=fallback_ids,
        skipped_ids=skipped_ids,
    )
