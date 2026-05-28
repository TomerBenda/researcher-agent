# Changelog

All notable changes to this project will be documented in this file.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added (M2 — first source vertical slice + polite HTTP + entity extraction)
- `researcher collect` now works end-to-end for RSS sources (fetch → normalize → store):
  - **Polite HTTP client** (`http.py`): descriptive User-Agent, per-host request serialization with a configurable minimum interval, `Retry-After` honored on 429 (clamped to a max), conditional-GET (`If-None-Match` / `If-Modified-Since`) passthrough.
  - **URL canonicalizer** (`canonicalize.py`): scheme/host lowercasing, IDN→punycode, default-port stripping, tracking-param stripping (config-extendable), trailing-slash/empty-path normalization, and arXiv abs/pdf/version collapsing. `canonicalization_version = 1`.
  - **Deterministic entity extractor** (`entities.py`): CVEs, GitHub `owner/repo`, and `npm:` / `pypi:` packages; bounded to 64 entities per item.
  - **RSS adapter** (`sources/rss.py`) + **config-driven loader** (`sources/__init__.py`, `sources/base.py`) using an explicit `type`→adapter registry with startup validation (unknown types / bad config fail before any fetch).
  - **RSS normalizer** (`normalize.py`): `RawItem` → `Item` + extracted entities.
  - **Collect orchestration** (`collect.py`): per-source error isolation, cursor advancement, and health counters; CLI `collect` wired with `--source / --sources / --db / --since / --until / --fail-on-error`.
  - `config/sources.example.yaml` — public starter source list (full curation is an M4 task).
  - Manual `smoke.yml` workflow for live-feed validation (not in default CI; runs in production log mode).
- Test suite grew from 52 to 198 (no new dependencies; ruff/mypy-strict/pytest all green).

### Changed
- M2 `collect` stops at the storage layer — **no classification and no vault rendering yet** (both land with the classifier in M3). Items are stored with `current_classification_id = NULL` until then. (Decision per CLAUDE.md §6: keeps M2 small and avoids inventing a fake "unclassified" classification that would pollute the append-only classification history.)
- ruff: added `typer.Option` / `typer.Argument` to flake8-bugbear `extend-immutable-calls` (Typer's intended default-argument idiom; not the mutable-default footgun B008 targets).

### Fixed
- Out-of-range / garbage feed publish dates no longer crash a run (e.g. `OSError` from `datetime.fromtimestamp` on Windows); such dates are treated as undated. Any per-item normalization failure is now contained (skipped + counted), never fatal — important for unattended runs over untrusted feeds. (Found via a live run during the M2 cycle.)

### Security / robustness (from M2 security + operations reviews)
- **Error-message redaction hardened.** Production log mode (`RESEARCHER_LOG_MODE=production`) now drops the exception message body entirely, keeping only the exception class name plus a short correlation hash. DNS/connect/SSL errors embed the bare private-feed *hostname* (not just full URLs), so the previous URL-only redaction leaked the source list into public CI logs (invariant #21).
- **`Retry-After` clamped** (default 60s): a hostile/broken server can no longer make the client sleep "forever" while holding the per-host lock and starving every later source in the serial run.
- **IDN hosts normalized to punycode** so a unicode host and its `xn--` form dedupe to one item rather than splitting.

### Deferred to M4 (captured from the M2 ops/security reviews)
- Total wall-time budgeting / parallelism across hosts for the unattended cron (serial × 20s timeout × dozens of feeds can exceed the Actions budget).
- A real source-health signal distinct from "quiet feed": separate `consecutive_error_runs`, exponential backoff for repeatedly-failing sources, and bozo-feed detection (HTTP 200 returning a non-feed page currently looks like an empty feed).
- `PRAGMA wal_checkpoint(TRUNCATE)` before committing `state.db` to the `state` branch; keep `-wal`/`-shm` sidecars out of git.
- A `status` / health-summary command and the daily cron's single-dead-feed failure policy.
- arXiv IDs as `ItemEntity`: deferred because `EntityKind` (data-model invariant #4) has no `arxiv` member; arXiv identity is captured by URL canonicalization instead. Revisit if a paper-reference entity kind is wanted.
- Percent-encoding case normalization in URL paths (low real-world dedup impact).

### Added (M1)
- Project scaffold, Pydantic data model, SQLite schema, storage layer, vault renderer with snapshot tests, CI workflow.

### Changed (M1)
- Reframed `daily` / `weekly` flows as `collect` / `synthesize` to decouple function from cadence. Schedule is now an orchestration concern; both commands are window-parameterized.
- Vault frontmatter `type:` values now `collection-report` and `synthesis-report` (was `daily-digest` and `weekly-roundup`). `schema_version` bumped to 2.
- Vault layout: collection reports at `{vault}/collection/{YYYY-MM-DD}.md`, synthesis reports at `{vault}/synthesis/{label}.md` where `label` encodes the window (e.g. `W2026-W22`, `D2026-05-27-30d`).
