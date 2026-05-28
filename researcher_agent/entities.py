"""Deterministic entity extraction (no LLM).

Extracted at normalize time from an item's title/summary text. The synthesis
agent later *queries* these via a tool rather than re-deriving them, and the
same entity recurring across items is the trend signal.

Extracted kinds (a subset of EntityKind):
- `cve`     — `CVE-YYYY-NNNN+`, normalized uppercase.
- `repo`    — GitHub `owner/repo`, from github.com URLs and bare mentions,
              normalized lowercase. Slashes inside non-github URL paths and
              common English/tech bigrams are deliberately not matched.
- `package` — `npm:name` / `pypi:name` (incl. scoped `npm:@scope/name`),
              normalized lowercase.

arXiv IDs are intentionally not emitted here: they are captured by URL
canonicalization (canonicalize.extract_arxiv_id), and EntityKind has no `arxiv`
member (data-model invariant). See CLAUDE.md §6 resolution notes.
"""

from __future__ import annotations

import re

from researcher_agent.models import EntityKind, ItemEntity

# Bound per-item output: content-rich feeds (full-HTML posts, code blocks) can
# produce thousands of repo-shaped tokens. Entities sort cve < package < repo,
# so capping after the sort keeps the rare high-value kinds and truncates only
# the noisy repo tail.
MAX_ENTITIES_PER_ITEM = 64

_CVE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)

# owner/repo inside a github.com URL (scheme optional). Stops at /?#.
_GITHUB_URL = re.compile(
    r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_][A-Za-z0-9._-]{0,99})",
    re.IGNORECASE,
)

# Bare owner/repo mention. The lookbehind keeps us out of URLs, paths, emails
# and longer tokens; the lookahead rejects a further path segment (a/b/c).
_BARE_REPO = re.compile(
    r"(?<![\w/.@:-])"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)"  # owner
    r"/"
    r"([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)"  # repo, no leading/trailing dot
    r"(?![\w/-])"
)

# Package name component: no leading/trailing dot or hyphen.
_PKG_NAME = r"[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*"
_PACKAGE = re.compile(
    rf"\b(npm|pypi):(@{_PKG_NAME}/{_PKG_NAME}|{_PKG_NAME})",
    re.IGNORECASE,
)

# GitHub first-path segments that are site routes, not usernames.
_RESERVED_OWNERS: frozenset[str] = frozenset(
    {
        "sponsors",
        "settings",
        "marketplace",
        "features",
        "about",
        "pricing",
        "explore",
        "topics",
        "collections",
        "trending",
        "notifications",
        "new",
        "login",
        "logout",
        "join",
        "organizations",
        "orgs",
        "users",
        "search",
        "apps",
        "dashboard",
        "pulls",
        "issues",
        "stars",
        "watching",
        "security",
        "site",
        "contact",
        "git",
        "blog",
        "support",
        "readme",
    }
)

# Common English / tech "x/y" pairs that look like repos but are not.
_NOT_REPOS: frozenset[str] = frozenset(
    {
        "and/or",
        "either/or",
        "he/she",
        "she/he",
        "s/he",
        "yes/no",
        "on/off",
        "input/output",
        "read/write",
        "client/server",
        "tcp/ip",
        "ca/browser",
        "km/h",
        "w/o",
        "n/a",
        "i/o",
    }
)

# Repo names ending in these are almost always file paths, not repositories.
_SOURCE_EXTENSIONS: tuple[str, ...] = (
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".md",
    ".rst",
    ".go",
    ".rs",
    ".rb",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".html",
    ".css",
    ".txt",
    ".sh",
)


def _has_letter(s: str) -> bool:
    return any(c.isalpha() for c in s)


def _looks_like_repo(owner: str, repo: str) -> bool:
    token = f"{owner}/{repo}".lower()
    if not _has_letter(token):
        return False
    if owner.lower() in _RESERVED_OWNERS:
        return False
    if token in _NOT_REPOS:
        return False
    return not repo.lower().endswith(_SOURCE_EXTENSIONS)


def extract_entities(canonical_hash: str, text: str) -> list[ItemEntity]:
    """Extract entities from `text`, deduped and deterministically ordered."""
    found: set[tuple[EntityKind, str]] = set()

    for m in _CVE.finditer(text):
        found.add(("cve", m.group(0).upper()))

    for m in _GITHUB_URL.finditer(text):
        owner, repo = m.group(1), m.group(2)
        repo = repo[:-4] if repo.lower().endswith(".git") else repo
        if owner.lower() not in _RESERVED_OWNERS:
            found.add(("repo", f"{owner}/{repo}".lower()))

    for m in _BARE_REPO.finditer(text):
        owner, repo = m.group(1), m.group(2)
        if _looks_like_repo(owner, repo):
            found.add(("repo", f"{owner}/{repo}".lower()))

    for m in _PACKAGE.finditer(text):
        found.add(("package", f"{m.group(1)}:{m.group(2)}".lower()))

    return [
        ItemEntity(canonical_hash=canonical_hash, kind=kind, value=value)
        for kind, value in sorted(found)[:MAX_ENTITIES_PER_ITEM]
    ]
