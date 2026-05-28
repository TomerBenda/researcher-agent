"""Tests for duplicate detection.

Conservative by design: a false merge silently drops an item from the digest,
so two items must be strongly the same (near-identical title in a time window, or
a shared strong identifier + a high title match) before one supersedes the other.
Merely sharing a CVE/repo makes items *related*, not duplicates (invariant #17).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from researcher_agent.dedupe import DedupCandidate, find_duplicates

BASE = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _c(
    h: str,
    title: str,
    *,
    score: int = 5,
    published_at: datetime | None = BASE,
    entities: set[tuple[str, str]] | None = None,
) -> DedupCandidate:
    return DedupCandidate(
        canonical_hash=h * 64 if len(h) == 1 else h,
        title=title,
        published_at=published_at,
        score=score,
        entities=frozenset(entities or set()),
    )


def test_identical_title_within_window_is_duplicate() -> None:
    a = _c("a", "Same Title Here", score=5)
    b = _c("b", "Same Title Here", score=8)
    pairs = find_duplicates([a, b])
    assert pairs == [(a.canonical_hash, b.canonical_hash)]  # lower score supersedes higher


def test_title_match_outside_window_not_duplicate() -> None:
    a = _c("a", "Same Title Here", published_at=BASE)
    b = _c("b", "Same Title Here", published_at=BASE + timedelta(hours=72))
    assert find_duplicates([a, b]) == []


def test_high_but_subthreshold_title_alone_not_duplicate() -> None:
    # 0.857 similarity, below the 0.92 title threshold, with no shared entity
    a = _c("a", "MCP server leaks secrets")
    b = _c("b", "MCP server leaks secrets via env")
    assert find_duplicates([a, b]) == []


def test_shared_cve_with_high_title_is_duplicate() -> None:
    cve = {("cve", "CVE-2025-1")}
    a = _c("a", "MCP server leaks secrets", score=4, entities=cve)
    b = _c("b", "MCP server leaks secrets via env", score=9, entities=cve)
    pairs = find_duplicates([a, b])
    assert pairs == [(a.canonical_hash, b.canonical_hash)]


def test_shared_cve_with_low_title_not_duplicate() -> None:
    # same CVE but clearly different articles -> related, not duplicate
    cve = {("cve", "CVE-2025-1")}
    a = _c("a", "Weekly security roundup", entities=cve)
    b = _c("b", "Deep dive into kernel internals", entities=cve)
    assert find_duplicates([a, b]) == []


def test_shared_repo_is_not_a_strong_signal() -> None:
    # repo overlap is weak; sub-threshold title must not dedupe on it
    repo = {("repo", "owner/proj")}
    a = _c("a", "MCP server leaks secrets", entities=repo)
    b = _c("b", "MCP server leaks secrets via env", entities=repo)
    assert find_duplicates([a, b]) == []


def test_winner_is_highest_score() -> None:
    a = _c("a", "Dup", score=2)
    b = _c("b", "Dup", score=9)
    c = _c("c", "Dup", score=5)
    pairs = dict(find_duplicates([a, b, c]))
    assert set(pairs) == {a.canonical_hash, c.canonical_hash}
    assert all(winner == b.canonical_hash for winner in pairs.values())


def test_transitive_cluster_collapses_to_one_winner() -> None:
    a = _c("a", "Shared headline", score=1)
    b = _c("b", "Shared headline", score=7)
    c = _c("c", "Shared headline", score=3)
    pairs = dict(find_duplicates([a, b, c]))
    assert set(pairs) == {a.canonical_hash, c.canonical_hash}
    assert set(pairs.values()) == {b.canonical_hash}


def test_undated_identical_titles_are_duplicates() -> None:
    a = _c("a", "No Date Title", score=3, published_at=None)
    b = _c("b", "No Date Title", score=6, published_at=None)
    pairs = find_duplicates([a, b])
    assert pairs == [(a.canonical_hash, b.canonical_hash)]


def test_window_is_configurable() -> None:
    a = _c("a", "Same Title Here", published_at=BASE)
    b = _c("b", "Same Title Here", published_at=BASE + timedelta(hours=10))
    # default 48h -> dup; tighten to 6h -> not dup
    assert len(find_duplicates([a, b])) == 1
    assert find_duplicates([a, b], window_hours=6) == []


def test_tiebreak_prefers_earlier_published() -> None:
    a = _c("a", "Tie", score=5, published_at=BASE + timedelta(hours=1))
    b = _c("b", "Tie", score=5, published_at=BASE)  # earlier -> winner
    pairs = dict(find_duplicates([a, b]))
    assert pairs == {a.canonical_hash: b.canonical_hash}


def test_empty_and_single() -> None:
    assert find_duplicates([]) == []
    assert find_duplicates([_c("a", "solo")]) == []
