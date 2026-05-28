"""Tests for URL canonicalization.

Canonicalization is the foundation of dedup: two URLs that point at the same
resource must collapse to one canonical string (and thus one hash); two URLs
that point at different resources must not.
"""

from __future__ import annotations

import hashlib

import pytest

from researcher_agent.canonicalize import (
    CANONICALIZATION_VERSION,
    canonical_hash,
    canonicalize_url,
    extract_arxiv_id,
)

# --- scheme / host normalization ----------------------------------------------


def test_lowercases_scheme_and_host() -> None:
    assert canonicalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_preserves_path_case() -> None:
    # paths are case-sensitive; only scheme+host lowercase
    assert canonicalize_url("https://example.com/AbC") == "https://example.com/AbC"


# --- default ports -------------------------------------------------------------


def test_strips_default_https_port() -> None:
    assert canonicalize_url("https://example.com:443/x") == "https://example.com/x"


def test_strips_default_http_port() -> None:
    assert canonicalize_url("http://example.com:80/x") == "http://example.com/x"


def test_keeps_nondefault_port() -> None:
    assert canonicalize_url("https://example.com:8443/x") == "https://example.com:8443/x"


# --- trailing slash / empty path ----------------------------------------------


def test_strips_trailing_slash() -> None:
    assert canonicalize_url("https://example.com/foo/") == "https://example.com/foo"


def test_root_path_keeps_single_slash() -> None:
    assert canonicalize_url("https://example.com/") == "https://example.com/"


def test_empty_path_becomes_root_slash() -> None:
    # bare host and host-with-slash must collapse to the same canonical form
    assert canonicalize_url("https://example.com") == "https://example.com/"
    assert canonicalize_url("https://example.com") == canonicalize_url("https://example.com/")


# --- tracking params -----------------------------------------------------------


def test_strips_utm_params() -> None:
    assert (
        canonicalize_url("https://example.com/p?utm_source=x&utm_medium=y&id=7")
        == "https://example.com/p?id=7"
    )


def test_strips_mc_prefix_params() -> None:
    assert (
        canonicalize_url("https://example.com/p?mc_cid=abc&mc_eid=def") == "https://example.com/p"
    )


def test_strips_known_exact_tracking_params() -> None:
    assert (
        canonicalize_url("https://example.com/p?fbclid=z&gclid=q&ref=hn&keep=1")
        == "https://example.com/p?keep=1"
    )


def test_dropping_all_params_removes_query_string() -> None:
    assert canonicalize_url("https://example.com/p?utm_source=x") == "https://example.com/p"


def test_preserves_order_of_surviving_params() -> None:
    assert (
        canonicalize_url("https://example.com/p?b=2&utm_source=x&a=1")
        == "https://example.com/p?b=2&a=1"
    )


def test_extra_tracking_params_from_config() -> None:
    assert (
        canonicalize_url("https://example.com/p?sid=1&keep=2", extra_tracking_params=["sid"])
        == "https://example.com/p?keep=2"
    )


def test_non_tracking_params_preserved() -> None:
    assert (
        canonicalize_url("https://example.com/p?page=3&q=mcp")
        == "https://example.com/p?page=3&q=mcp"
    )


# --- fragments -----------------------------------------------------------------


def test_fragment_preserved() -> None:
    # distinct fragments are kept so we never falsely merge distinct anchors
    assert canonicalize_url("https://example.com/p#section") == "https://example.com/p#section"


# --- arXiv special-casing ------------------------------------------------------


def test_arxiv_abs_canonical() -> None:
    assert (
        canonicalize_url("https://arxiv.org/abs/2305.12345") == "https://arxiv.org/abs/2305.12345"
    )


def test_arxiv_pdf_to_abs() -> None:
    assert (
        canonicalize_url("https://arxiv.org/pdf/2305.12345") == "https://arxiv.org/abs/2305.12345"
    )


def test_arxiv_pdf_with_extension_to_abs() -> None:
    assert (
        canonicalize_url("https://arxiv.org/pdf/2305.12345v2.pdf")
        == "https://arxiv.org/abs/2305.12345"
    )


def test_arxiv_version_stripped() -> None:
    assert (
        canonicalize_url("https://arxiv.org/abs/2305.12345v3") == "https://arxiv.org/abs/2305.12345"
    )
    # v1 and v2 must dedupe to the same canonical item
    assert canonicalize_url("https://arxiv.org/abs/2305.12345v1") == canonicalize_url(
        "https://arxiv.org/abs/2305.12345v2"
    )


def test_arxiv_forces_https_and_bare_host() -> None:
    assert (
        canonicalize_url("http://www.arxiv.org/abs/2305.12345")
        == "https://arxiv.org/abs/2305.12345"
    )


def test_arxiv_old_style_id() -> None:
    assert (
        canonicalize_url("https://arxiv.org/abs/cs.CR/0501001")
        == "https://arxiv.org/abs/cs.CR/0501001"
    )


def test_non_arxiv_host_not_special_cased() -> None:
    # an /abs/ path on another host is just a normal URL
    assert (
        canonicalize_url("https://example.com/abs/2305.12345")
        == "https://example.com/abs/2305.12345"
    )


# --- internationalized domain names -------------------------------------------


def test_idn_host_normalized_to_punycode() -> None:
    # the same site published as a unicode host and as its punycode form must
    # collapse to one canonical item, not two
    uni = canonicalize_url("https://bücher.example.com/p")
    puny = canonicalize_url("https://xn--bcher-kva.example.com/p")
    assert uni == puny == "https://xn--bcher-kva.example.com/p"


def test_idn_host_dedupes_by_hash() -> None:
    assert canonical_hash("https://bücher.example.com/p") == canonical_hash(
        "https://xn--bcher-kva.example.com/p"
    )


# --- extract_arxiv_id ----------------------------------------------------------


def test_extract_arxiv_id_from_abs() -> None:
    assert extract_arxiv_id("/abs/2305.12345") == "2305.12345"


def test_extract_arxiv_id_strips_version() -> None:
    assert extract_arxiv_id("/pdf/2305.12345v7.pdf") == "2305.12345"


def test_extract_arxiv_id_none_when_absent() -> None:
    assert extract_arxiv_id("/blog/some-post") is None


# --- hashing -------------------------------------------------------------------


def test_canonical_hash_is_sha256_of_canonical_url() -> None:
    url = "https://Example.com/p?utm_source=x"
    expected = hashlib.sha256(b"https://example.com/p").hexdigest()
    assert canonical_hash(url) == expected


def test_canonical_hash_is_64_hex_chars() -> None:
    h = canonical_hash("https://example.com/")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_equivalent_urls_hash_identically() -> None:
    a = canonical_hash("HTTPS://Example.com:443/post/?utm_source=newsletter")
    b = canonical_hash("https://example.com/post")
    assert a == b


def test_distinct_urls_hash_differently() -> None:
    assert canonical_hash("https://example.com/a") != canonical_hash("https://example.com/b")


def test_canonicalization_version_is_one() -> None:
    assert CANONICALIZATION_VERSION == 1


# --- robustness ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/",
        "http://example.com:80",
        "https://例え.example.com/パス",
    ],
)
def test_canonicalize_is_idempotent(url: str) -> None:
    once = canonicalize_url(url)
    assert canonicalize_url(once) == once
