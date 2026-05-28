"""URL canonicalization for dedup.

A canonical URL is a normalized string such that two URLs pointing at the same
resource produce the same canonical form (and thus the same `canonical_hash`),
while distinct resources stay distinct.

Rules (canonicalization_version 1):
- Lowercase scheme and host (paths stay case-sensitive).
- Internationalized hosts are normalized to punycode (ASCII xn-- form).
- Any userinfo (`user:pass@`) is dropped — it never identifies a distinct
  article and would be a credential leak if hashed/stored.
- Strip default ports (80 for http, 443 for https).
- Strip tracking query params: the `utm_*` and `mc_*` prefixes plus a curated
  set of exact names. The caller may extend the exact set via config.
- Empty path becomes `/`; trailing slash stripped from all other paths.
- Fragments are preserved (distinct anchors must not be merged).
- arXiv URLs collapse to `https://arxiv.org/abs/{id}` regardless of input form
  (`/abs/`, `/pdf/`, with or without version, with or without `.pdf`).

Bump `CANONICALIZATION_VERSION` when these rules change so stored items can be
re-canonicalized selectively (see Item.canonicalization_version).
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CANONICALIZATION_VERSION = 1

# Query-param name prefixes to strip wholesale.
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "mc_")

# Exact query-param names to strip (lowercased).
_TRACKING_EXACT: frozenset[str] = frozenset(
    {
        "ref",
        "ref_src",
        "ref_url",
        "fbclid",
        "gclid",
        "gclsrc",
        "dclid",
        "msclkid",
        "igshid",
        "mkt_tok",
        "_hsenc",
        "_hsmi",
        "vero_id",
        "yclid",
    }
)

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

# Modern arXiv IDs: YYMM.NNNNN (4-5 trailing digits), optional version suffix.
_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
# Legacy arXiv IDs: archive(.subject)?/YYMMNNN, optional version suffix.
_ARXIV_OLD = re.compile(r"([a-z-]+(?:\.[A-Za-z]{2})?/\d{7})(v\d+)?", re.IGNORECASE)


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    if lowered in _TRACKING_EXACT:
        return True
    return any(lowered.startswith(p) for p in _TRACKING_PREFIXES)


def extract_arxiv_id(path: str) -> str | None:
    """Extract a version-stripped arXiv ID from a URL path, or None.

    Handles both modern (`2305.12345`) and legacy (`cs.CR/0501001`) forms and
    strips any version suffix and `.pdf` extension.
    """
    candidate = path
    if candidate.lower().endswith(".pdf"):
        candidate = candidate[:-4]
    m = _ARXIV_NEW.search(candidate)
    if m:
        return m.group(1)
    m = _ARXIV_OLD.search(candidate)
    if m:
        return m.group(1)
    return None


def canonicalize_url(url: str, *, extra_tracking_params: Iterable[str] = ()) -> str:
    """Return the canonical form of `url`."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    # Normalize internationalized hosts to punycode so a unicode host and its
    # ASCII (xn--) form dedupe to one item. ASCII hosts are left untouched.
    if host and not host.isascii():
        # leave the unicode host as-is if it can't be IDNA-encoded
        with contextlib.suppress(UnicodeError, ValueError):
            host = host.encode("idna").decode("ascii")

    # arXiv collapses to a single canonical abs URL regardless of input shape.
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        arxiv_id = extract_arxiv_id(parts.path)
        if arxiv_id is not None:
            return f"https://arxiv.org/abs/{arxiv_id}"

    netloc = host
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{host}:{port}"

    path = parts.path
    if path == "":
        path = "/"
    elif path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    extra = {p.lower() for p in extra_tracking_params}
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k) and k.lower() not in extra
    ]
    query = urlencode(pairs)

    return urlunsplit((scheme, netloc, path, query, parts.fragment))


def canonical_hash(url: str, *, extra_tracking_params: Iterable[str] = ()) -> str:
    """SHA-256 hex digest of the canonical form of `url` (64 lowercase chars)."""
    canonical = canonicalize_url(url, extra_tracking_params=extra_tracking_params)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
