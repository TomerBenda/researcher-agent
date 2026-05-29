# Changelog

All notable changes to this project will be documented in this file.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed / hardened (post-M4 review remediation)
- **Dedupe no longer false-merges via transitivity.** `find_duplicates` was union-find clustering over a *non-transitive* relation (fuzzy title + time window + entity overlap): A~B and B~C do not imply A~C, so a bridge item could collapse two genuinely-distinct items into one cluster and silently drop one from the digest (the failure invariant #17 warns against). Replaced with conservative best-first star-assignment — only directly-judged duplicate pairs merge, and a loser is never also made a winner (no within-pass chains).
- **Dedupe supersession stays a flat star across runs.** Dedupe runs every collect over a pool that excludes already-superseded items, so a former winner can itself be superseded by a newer, higher-scored duplicate on a later run — leaving its prior losers pointing at a now-superseded item (a `B→A→D` chain). Added `Database.resolve_supersession_root` + `repoint_supersessions`; `dedupe_recent` now resolves the winner to its surviving root and re-points any pre-existing dependents, so `superseded_by` is always one hop to the survivor.

### Added (M4 — remaining source types + daily cron + ops commands)
- Three new source types beyond RSS, all via the shared polite client (raw httpx / feedparser; no PyGithub), each with cursor handling and MockTransport tests:
  - **arXiv** (`sources/arxiv.py`): queries the export Atom API; reuses the shared feedparser helpers (`sources/feed.py`).
  - **Hacker News** (`sources/hn_search.py`): Algolia search-by-date with a `created_at_i` high-watermark cursor.
  - **GitHub releases** (`sources/github_releases.py`): REST with an ETag/304 cursor; skips drafts.
  - **GitHub topic search** (`sources/github_topic.py`): `/search/repositories` with a `pushed_at` watermark cursor.
- Per-type normalizers (`normalize_arxiv` / `normalize_hn` / `normalize_github_release` / `normalize_github_repo`), all built on a shared `build_item` helper; the collect pipeline dispatches by `source_type`. All five types are registered in the loader and demonstrated in `config/sources.example.yaml`.
- **CLI:** `researcher status` (per-source health — STALE / FAILING / NEVER RUN — plus item/backlog totals, source names redacted in production, optional `--max-consecutive-errors` exit gate) and `researcher validate-config` (checks both YAML files offline).
- **Daily cron** (`.github/workflows/collect.yml`): restores `state.db` from a dedicated `state` branch, collects, integrity-checks + WAL-checkpoints, and commits `state.db` back (no force-push; orphan branch on cold start; token via `http.extraheader`; serialized via a concurrency group). `Database.checkpoint()` / `integrity_check()` support it.
- Gemini provider now backs off on 429 / RESOURCE_EXHAUSTED, and the polite client backs off a bounded default on a 429 with no `Retry-After` (arXiv does this).
- Test suite grew from 294 to 345 (no new dependencies).

### Security / robustness (from M4 reviews)
- The GitHub / HN adapters now tolerate malformed-but-200 responses (non-list/non-dict shapes, a string `author`) instead of letting one tampered/erroring upstream silently kill a source.
- `build_item` truncates a stored summary (4k chars) and the entity-extraction text (20k) so a hostile/huge release body can't bloat storage or the classifier prompt.
- The cron commits `state.db` only after the integrity check + WAL checkpoint, and only `state.db` (never `git add -A`); a corrupt DB fails the run without overwriting good history.

### Deferred (captured from M4 reviews)
- A response-size streaming cap in the polite client (today only timeout + the truncations above bound input size).
- Per-host minimum intervals (arXiv asks ~3s; the global default is 1s) and GitHub Search secondary-rate-limit (403) backoff. arXiv's export API is also externally flaky (429/timeout); collect isolates it per-source and retries next run.

### Fixed / hardened (post-M3 review remediation)
- **Dead config wired up:** `classifier`-adjacent `tracking_params_to_strip` from `agent.yaml` is now actually threaded into URL canonicalization during `collect` (the CLI previously loaded it but never passed it to normalization).
- **Honest source health:** added `source_runs.consecutive_error_runs` (migration `002`) so a failing/moved feed is distinguishable from a merely quiet one — `consecutive_empty_runs` no longer conflates a 304/quiet run with a dead source. (Feeds the M4 `status` command.)
- **Public-log hygiene:** in production log mode the collect summary now hashes source names (the slugs reveal the private source list — invariant #20), matching the existing error-message redaction.
- **State-branch prep:** `Database.checkpoint()` (`PRAGMA wal_checkpoint(TRUNCATE)`) and `integrity_check()` added, so M4's cron can flush the WAL and verify the DB before committing `state.db` to git.
- Removed a misleading unused `now` parameter from `http._parse_retry_after`.

### Added (M3 — classifier + dedupe + golden set)
- `researcher collect` now classifies, dedupes, and renders after storing — the full collect pipeline:
  - **LLM classifier** with provider abstraction: **Gemini** (`gemini-2.5-flash`, free tier) default, **Ollama** offline fallback. Providers are thin; the risky JSON-reply parsing is shared and unit-tested (`llm/base.py`, `llm/gemini.py`, `llm/ollama.py`, `llm/factory.py`).
  - **Orchestration** (`classify.py`): batched, **per-item retry** at temperature 0 (not per-batch), **token-budget pre-flight** before every call, taxonomy validation, fallback to `(other, 3)` on genuine content failure.
  - **Prompt-as-file** (`prompts/classify.md`, loaded + hashed via `prompts.py`); `classifier_version` = hash of the rendered system prompt + model id, recorded on every `Classification`.
  - **Config-driven taxonomy + settings** (`config.py`, `config/agent.example.yaml`): the 10-slug taxonomy, classifier provider/model/batch/budget, dedup thresholds, `vault_path`, and tracking params all live in `agent.yaml` (gitignored — has the personal vault path).
  - **Dedupe** (`dedupe.py`): cross-post detection via near-identical title in a time window OR a shared strong entity (CVE) with a high title match; winner = highest score, losers superseded with merged sources. Conservative to avoid false merges.
  - **Vault rendering returns** (deferred from M2): collect writes the day's classified, non-superseded items to `{vault}/collection/{YYYY-MM-DD}.md` (deterministic).
  - **Golden set** (`config/golden_set.jsonl`, 25 labeled items across all topics) + `tests/test_golden.py` (`make test-golden`): runs the real classifier, asserts ≥85% top-1; opt-in only (deselected from the default suite, skipped without an API key).
  - New state queries `list_unclassified` / `list_recent_items`; CLI flags `--classify/--no-classify`, `--agent`, `--vault`.
- Test suite grew from 198 to 294 (no new dependencies; `google-genai` / `ollama` / `rapidfuzz` were already declared).

### Changed
- `collect` no longer stops at storage — it runs classify → dedupe → render, skipping that stage cleanly (items stored, left unclassified) when no provider/API key is available (offline collection).

### Security / robustness (from M3 security + operations reviews)
- **Prompt-injection hardening:** the classifier user message is now a JSON array (`render_items_message`), so untrusted feed title/summary content cannot forge item boundaries or inject fake schema/instruction lines. Item ids are the canonical hash (not feed-controlled); the parser keeps the **first** result per id (no duplicate-id overwrite).
- **Untrusted-output bounds:** the response parser caps input size and entry count, catches `RecursionError` on deeply-nested JSON, rejects absurd `topic`s, and cleans + truncates `rationale` (it is rendered into the public vault).
- **No fallback poisoning:** a transient provider error (e.g. a rate-limit storm) leaves items **unclassified** (re-tried next run) instead of persisting junk `(other, 3)` labels; only a real response that can't classify an item falls back. A **circuit breaker** aborts the run after repeated consecutive provider failures rather than hammering a throttled API.
- **Cold-start cost cap:** `classifier.max_items_per_run` (default 200) drains a large backlog over several runs; the O(n²) dedupe pool is skipped above a size guard.
- No provider path logs the API key, the prompt (which embeds the source list), or feed URLs; the key is read only from the environment.

### Deferred to M4 (captured from the M3 reviews)
- Provider-level `Retry-After` / exponential backoff on 429 (the circuit breaker prevents poisoning; in-provider backoff would also avoid the storm).
- More accurate token accounting (output tokens + the system prompt re-sent on each retry); the current `~chars/4` estimate is a coarse guardrail only.
- A real dedupe blocking strategy (window/block before the pairwise compare) for large first imports, instead of the size-guard skip.

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
