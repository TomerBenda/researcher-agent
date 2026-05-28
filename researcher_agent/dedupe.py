"""Duplicate detection across items with distinct canonical hashes.

Exact-URL duplicates already collapse at storage (one Item per canonical_hash).
This catches *cross-posts*: the same content surfaced at different URLs. Two
items are duplicates when, within a time window, their titles are near-identical
OR they share a strong identifier (a CVE) and have a high title match. Sharing a
CVE/repo alone makes items related, not duplicates (invariant #17) — so the bar
stays high to avoid false merges that would drop an item from the digest.

Pure: returns (loser_hash, winner_hash) pairs; the caller applies supersession.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rapidfuzz import fuzz


@dataclass(frozen=True)
class DedupCandidate:
    canonical_hash: str
    title: str
    published_at: datetime | None
    score: int
    entities: frozenset[tuple[str, str]]


def _title_similarity(a: str, b: str) -> float:
    return fuzz.ratio(a.strip().lower(), b.strip().lower()) / 100.0


def _within_window(a: DedupCandidate, b: DedupCandidate, window_hours: int) -> bool:
    # If either item is undated we cannot apply the time test; don't let that
    # block — the title/entity thresholds still gate the decision.
    if a.published_at is None or b.published_at is None:
        return True
    return abs((a.published_at - b.published_at).total_seconds()) <= window_hours * 3600


def _is_duplicate(
    a: DedupCandidate,
    b: DedupCandidate,
    *,
    title_threshold: float,
    entity_title_threshold: float,
    window_hours: int,
    strong_kinds: tuple[str, ...],
) -> bool:
    if not _within_window(a, b, window_hours):
        return False
    sim = _title_similarity(a.title, b.title)
    if sim >= title_threshold:
        return True
    shared_strong = any(kind in strong_kinds for kind, _ in (a.entities & b.entities))
    return shared_strong and sim >= entity_title_threshold


def _winner_key(c: DedupCandidate) -> tuple[int, float, str]:
    # highest score, then earliest published (undated last), then smallest hash
    pub = c.published_at.timestamp() if c.published_at is not None else float("inf")
    return (-c.score, pub, c.canonical_hash)


def find_duplicates(
    candidates: list[DedupCandidate],
    *,
    title_threshold: float = 0.92,
    entity_title_threshold: float = 0.85,
    window_hours: int = 48,
    strong_kinds: tuple[str, ...] = ("cve",),
) -> list[tuple[str, str]]:
    """Return (loser_hash, winner_hash) pairs for detected duplicate clusters."""
    n = len(candidates)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i in range(n):
        for j in range(i + 1, n):
            if _is_duplicate(
                candidates[i],
                candidates[j],
                title_threshold=title_threshold,
                entity_title_threshold=entity_title_threshold,
                window_hours=window_hours,
                strong_kinds=strong_kinds,
            ):
                union(i, j)

    clusters: dict[int, list[DedupCandidate]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(candidates[i])

    pairs: list[tuple[str, str]] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        winner = min(members, key=_winner_key)
        for member in members:
            if member.canonical_hash != winner.canonical_hash:
                pairs.append((member.canonical_hash, winner.canonical_hash))
    return pairs
