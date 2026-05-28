"""Tests for deterministic entity extraction.

Entities are a cheap, regex-driven signal extracted at normalize time. Precision
matters more than recall: a false entity pollutes the trend signal, while a
missed one is merely a gap. The fixtures below are real-world-shaped strings.
"""

from __future__ import annotations

from researcher_agent.entities import MAX_ENTITIES_PER_ITEM, extract_entities

HASH = "a" * 64


def _values(text: str, kind: str) -> set[str]:
    return {e.value for e in extract_entities(HASH, text) if e.kind == kind}


# --- CVEs ----------------------------------------------------------------------


def test_extracts_cve_uppercased() -> None:
    assert _values("Patched in cve-2025-1234 yesterday", "cve") == {"CVE-2025-1234"}


def test_extracts_long_cve_number() -> None:
    assert _values("See CVE-2026-123456 advisory", "cve") == {"CVE-2026-123456"}


def test_extracts_multiple_cves() -> None:
    assert _values("CVE-2025-0001 and CVE-2025-0002", "cve") == {
        "CVE-2025-0001",
        "CVE-2025-0002",
    }


def test_rejects_malformed_cve() -> None:
    # too few digits in the sequence component
    assert _values("CVE-2025-12 is not valid", "cve") == set()


def test_entity_carries_hash() -> None:
    entities = extract_entities(HASH, "CVE-2025-1234")
    assert entities[0].canonical_hash == HASH


# --- GitHub repos from URLs ----------------------------------------------------


def test_extracts_repo_from_github_url() -> None:
    assert _values("https://github.com/anthropics/claude-code", "repo") == {
        "anthropics/claude-code"
    }


def test_extracts_repo_from_deep_github_url() -> None:
    text = "release at https://github.com/modelcontextprotocol/servers/releases/tag/v1.2.0"
    assert _values(text, "repo") == {"modelcontextprotocol/servers"}


def test_github_url_repo_lowercased() -> None:
    assert _values("https://github.com/Anthropics/Claude-Code", "repo") == {
        "anthropics/claude-code"
    }


def test_github_url_strips_dot_git() -> None:
    assert _values("clone https://github.com/owner/repo.git", "repo") == {"owner/repo"}


def test_github_reserved_path_not_a_repo() -> None:
    assert _values("https://github.com/sponsors/someone", "repo") == set()


# --- GitHub repos from bare mentions ------------------------------------------


def test_extracts_bare_repo_mention() -> None:
    assert _values("the modelcontextprotocol/servers repo is great", "repo") == {
        "modelcontextprotocol/servers"
    }


def test_does_not_extract_repo_from_foreign_url_path() -> None:
    # slashes inside a non-github URL path must not become a repo
    assert _values("see https://example.com/foo/bar for details", "repo") == set()


def test_does_not_extract_common_english_bigrams() -> None:
    for text in ("this and/or that", "tcp/ip stack", "read/write access", "input/output"):
        assert _values(text, "repo") == set(), text


def test_does_not_extract_dates_or_fractions() -> None:
    assert _values("on 5/27 we shipped, a 24/7 service", "repo") == set()


def test_does_not_extract_file_paths() -> None:
    assert _values("edit src/main.py and docs/readme.md", "repo") == set()


# --- packages ------------------------------------------------------------------


def test_extracts_npm_package() -> None:
    assert _values("install npm:left-pad now", "package") == {"npm:left-pad"}


def test_extracts_pypi_package() -> None:
    assert _values("pip uses pypi:requests", "package") == {"pypi:requests"}


def test_extracts_scoped_npm_package() -> None:
    assert _values("npm:@modelcontextprotocol/sdk is published", "package") == {
        "npm:@modelcontextprotocol/sdk"
    }


def test_package_lowercased() -> None:
    assert _values("NPM:Express", "package") == {"npm:express"}


# --- combined / dedup ----------------------------------------------------------


def test_dedupes_repeated_entities() -> None:
    text = (
        "CVE-2025-1234 again CVE-2025-1234 and anthropics/claude-code twice anthropics/claude-code"
    )
    entities = extract_entities(HASH, text)
    pairs = [(e.kind, e.value) for e in entities]
    assert pairs.count(("cve", "CVE-2025-1234")) == 1
    assert pairs.count(("repo", "anthropics/claude-code")) == 1


def test_mixed_extraction() -> None:
    text = "Advisory CVE-2026-9999 affects https://github.com/foo/bar and the npm:evil-pkg package."
    got = {(e.kind, e.value) for e in extract_entities(HASH, text)}
    assert got == {
        ("cve", "CVE-2026-9999"),
        ("repo", "foo/bar"),
        ("package", "npm:evil-pkg"),
    }


def test_empty_text_yields_nothing() -> None:
    assert extract_entities(HASH, "") == []


def test_entities_are_capped_per_item() -> None:
    # a content-heavy post can yield thousands of repo-shaped tokens; bound it
    many_repos = " ".join(f"owner{i}/repo{i}" for i in range(500))
    entities = extract_entities(HASH, many_repos)
    assert len(entities) == MAX_ENTITIES_PER_ITEM


def test_cap_preserves_rare_high_value_kinds() -> None:
    # cve/package sort before repo, so the cap truncates noisy repos, not CVEs
    text = "CVE-2030-1234 npm:rare-pkg " + " ".join(f"owner{i}/repo{i}" for i in range(500))
    entities = extract_entities(HASH, text)
    values = {(e.kind, e.value) for e in entities}
    assert ("cve", "CVE-2030-1234") in values
    assert ("package", "npm:rare-pkg") in values
    assert len(entities) == MAX_ENTITIES_PER_ITEM


def test_result_is_deterministically_ordered() -> None:
    text = "npm:z-pkg CVE-2025-0001 zeta/repo alpha/repo CVE-2025-0002"
    first = [(e.kind, e.value) for e in extract_entities(HASH, text)]
    second = [(e.kind, e.value) for e in extract_entities(HASH, text)]
    assert first == second
    assert first == sorted(first)
