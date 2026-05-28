# CLAUDE.md — researcher-agent development guide

---

## 1. Project at a glance

`researcher-agent` is a personal AI security research intelligence tool. Two functions over a shared substrate (SQLite store + Obsidian vault):

- **`researcher collect`** — gather from configured sources (RSS, GitHub, arXiv, HN), classify, dedupe, store; render a collection report to the vault. Default cadence: daily, via GitHub Actions.
- **`researcher synthesize`** — read items in a window, run a tool-using LLM agent, extract entities, queue follow-ups, render a synthesis report. Default cadence: weekly, but ad-hoc runs over arbitrary windows are first-class.

Owner: Tomer Benda. The repo is **public from day one** — every commit and code decision is visible. Treat the README and code as part of the research artifact.

Constraints worth memorizing:
- Free-tier APIs by default. Total spend target ≤$5/mo (synthesis allowed ~$0.20/wk on Anthropic; collection runs at $0).
- Python 3.11+, strict type hints, `from __future__ import annotations`.
- Explicit over clever. No metaclasses, no decorator routing, no magic config loaders.
- Diff-reviewable code: small modules, explicit dependencies.

---

## 2. Where things live

| Thing | Location | Notes |
|---|---|---|
| Build spec | `docs/researcher-agent-spec.md` | The source of truth for scope. Open-questions in §13 are resolved (see §3 of this doc). |
| Models | `researcher_agent/models.py` | Pydantic v2, frozen, tz-aware UTC enforced. |
| Storage | `researcher_agent/state.py` | All SQL lives here. WAL + FK enforced. |
| Migrations | `researcher_agent/migrations/` | Numbered `.sql` files, applied on `Database()` construction. |
| HTTP client | `researcher_agent/http.py` | Shared polite `httpx` wrapper (UA, per-host spacing, Retry-After, conditional GET). |
| Canonicalize | `researcher_agent/canonicalize.py` | URL → canonical string + `canonical_hash`. arXiv/IDN handling. `canonicalization_version = 1`. |
| Entities | `researcher_agent/entities.py` | Regex extraction of CVE / repo / package. Capped at 64/item. |
| Sources | `researcher_agent/sources/` | `base.py` (Protocol + config), `rss.py` (RSS adapter), `__init__.py` (loader + registry). |
| Normalize | `researcher_agent/normalize.py` | `normalize_rss(raw, now) -> (Item, [ItemEntity])`. Pure; HTML-strips before extraction. |
| Config | `researcher_agent/config.py` | Loads `agent.yaml`: config-driven taxonomy, classifier/dedup settings, vault_path. |
| Prompts | `researcher_agent/prompts.py` + `prompts/*.md` | Prompt files hashed at load; `classifier_version` = hash(prompt + model). |
| Classifier | `researcher_agent/classify.py` | Batch + per-item retry + token budget + circuit breaker + fallback. Provider-agnostic. |
| LLM providers | `researcher_agent/llm/` | `base.py` (Protocol + shared JSON parser/framing), `gemini.py`, `ollama.py`, `factory.py`. |
| Dedupe | `researcher_agent/dedupe.py` | `find_duplicates` — entity-overlap + fuzzy title/window. Pure; conservative. |
| Collect | `researcher_agent/collect.py` | `run_collect` (fetch/store) + `post_process` (classify→dedupe→render). Per-source error isolation. |
| Vault writer | `researcher_agent/vault.py` | Renders collection + synthesis reports. Pure functions; atomic writes. |
| CLI | `researcher_agent/__main__.py` | Typer; `collect` (full pipeline as of M3) and `synthesize` (stub through M5). |
| Golden set | `config/golden_set.jsonl` | 25 labeled items; `make test-golden` asserts ≥85% top-1 (opt-in, costs tokens). |
| Tests | `tests/` | Unit + snapshot + golden. 294 after M3 (198 after M2, 52 after M1); never let this drop silently. |
| Snapshots | `tests/snapshots/` | Vault rendering golden files. Regenerate with `UPDATE_SNAPSHOTS=1 pytest`. |
| CI | `.github/workflows/ci.yml` | ruff + format-check + mypy --strict + pytest. |
| state.db | **Not in this repo.** Will live on a dedicated `state` branch (regular commits, no force-push). | Adds full audit history; main stays clean. |
| Vault inbox | **Separate repo** `tbd-research-inbox`. | Collection reports → `collection/{YYYY-MM-DD}.md`; synthesis → `synthesis/{label}.md`. |

---

## 3. Design invariants — do not change without explicit discussion

These were resolved during M0/M1 design review. Each is load-bearing for some downstream concern. If you find yourself wanting to change one, **stop and surface it** before writing code.

### Data model
1. **`Item` is source-agnostic.** Source attribution lives in `ItemSource` (a join table). One canonical item, many `ItemSource` rows for cross-posted content.
2. **`Classification` is append-only history.** Active classification = the row referenced by `items.current_classification_id`. Reclassification is INSERT + UPDATE, never destructive. Use `db.classify_and_activate()` for the common case.
3. **`classifier_version`** is a hash of (system prompt + model id + relevant config). Bumps automatically when prompts change. `classifications.classifier_version` is the audit trail; backfill queries old versions.
4. **`ItemEntity` is extracted at normalize time** (regex / deterministic patterns), not by the synthesis agent. The agent **queries** entities via a tool. Kinds: `cve`, `repo`, `package`, `project`, `person`, `technique`, `venue`.
5. **`Followup` snapshots title/url at creation.** FK to `items.canonical_hash` is `ON DELETE SET NULL`. The snapshot keeps followups useful if the item is later pruned.
6. **All datetimes are tz-aware UTC.** Naive datetimes raise. Storage: ISO-8601 strings with `+00:00`.
7. **`Item.metadata: dict[str, Any]`** carries source-specific structured fields (CVSS scores, GitHub release versions, arXiv authors, etc.). Opaque to the rest of the system. Each source's normalizer is the authority on its shape.
8. **`canonicalization_version` on `Item`.** Bump when URL canonicalization rules change; lets us re-canonicalize selectively without recomputing every hash.

### Topics
9. **Topic slugs are config-driven**, not a closed Python enum. The taxonomy will evolve (today MCP-heavy; tomorrow browser-agents) without migrations.
10. **Primary topic + `secondary_topics: list[str]`.** Top-1 for sorting/digest grouping; secondaries for honest filtering. An MCP-specific CVE is `mcp-security` primary, with `mcp-ecosystem` and `agent-security` as secondaries.

### Vault contract
11. **`schema_version: 2`** in frontmatter. Field NAMES and VALUES are the Dataview contract. Bump on any breaking change to frontmatter.
12. **`renderer_version: 1`** is separate. Body markdown changes bump this, not `schema_version`.
13. **Rendering is pure.** No `now()`, no random ordering, no env reads. Re-rendering identical inputs → byte-identical output (verified by snapshot tests). Critical for the git-inbox workflow.
14. **Atomic writes only.** Tempfile + fsync + os.replace. LF line endings forced.
15. **Vault layout:** `collection/{YYYY-MM-DD}.md` and `synthesis/{label}.md` where `label` is constructed by `SynthesisWindow` (`W2026-W22`, `D2026-05-27-30d`, `R2026-05-01-to-2026-05-27`).

### Operations
16. **HTTP politeness is mandatory** when collection sources land. All adapters use a shared client with: descriptive `User-Agent`, per-host concurrency cap, `Retry-After` honored, `If-Modified-Since` / `If-None-Match` for RSS.
17. **Cross-posting dedup uses entity-overlap, not just URL hash.** Same arXiv ID, CVE, or `owner/repo` across multiple items → cluster, not duplicates.
18. **Per-item retry for the classifier**, not per-batch. One bad item in a batch of 10 doesn't waste tokens on the other 9.
19. **Token budget pre-flight** before every LLM call. Free-tier limits will bite during backfills.

### Public-repo
20. **Never commit:** API keys, the real `config/sources.yaml`, `state.db`, the vault path. `.env.example` and `sources.example.yaml` are the public artifacts.
21. **Production logging mode hashes URLs/titles** in error messages. GitHub Actions logs are public for public repos.

---

## 4. Workflow discipline

### Default loop per change
1. **Read first.** Spec for the milestone you're on. Current code in the relevant modules. Existing tests.
2. **Test architect first.** Before writing code, sketch the test surface — fixtures, edge cases, what cassettes to record. Optionally spawn a `test-architect` sub-agent (see §5). This prevents "tests that conform to the implementation."
3. **Implement.** Smallest module that makes new tests pass.
4. **Local gate:** `ruff check . && ruff format --check . && mypy researcher_agent --strict && pytest -q`. All four green.
5. **Cross-cutting review.** For any PR that touches more than one module or changes a design invariant, spawn a `code-reviewer` sub-agent on the diff.
6. **Domain-specific review.** Spawn additional reviewers per the matrix in §5.
7. **Commit in logical units** with messages that explain *why* (not just what). Reference the spec section or design invariant when relevant.

### Quality bars
- **Test count never drops** without an explicit "removed because X" line in the commit message.
- **No silent fallbacks.** Classifier parse failure → log + count + fall back; never just swallow.
- **No `# type: ignore` without a comment** explaining why. mypy --strict means strict.
- **No new dependencies without a one-line justification** in CHANGELOG.
- **No prompts in code.** Prompts live in `researcher_agent/prompts/*.md` and are hashed at load. Iterating on prompts becomes git-trackable separately from code.

### Definition of done for a milestone
- [ ] All planned modules implemented, with tests at unit + integration level where applicable.
- [ ] Snapshot tests added/updated for any new vault output shape.
- [ ] Type-check and lint green; coverage ≥70% on non-IO modules.
- [ ] CHANGELOG entry describing user-visible changes and any schema bumps.
- [ ] If new dependency: README/install instructions updated.
- [ ] If new public-surface command/flag: README quick-start updated.
- [ ] Cross-cutting code review completed (sub-agent).
- [ ] Domain-specific reviews completed per §5 matrix.
- [ ] Commits squashed/grouped into logically-coherent units before push.

---

## 5. Sub-agent roles

Spawn via the Task tool with a focused brief. Do not try to make one mega-agent do everything — small agents with narrow remits produce sharper output. Each role below has a **when** (trigger), **brief** (what to tell it), and **output** (what it returns).

### 5.1 `test-architect` — design tests before code
- **When:** Before implementing any module larger than ~50 lines, OR any module that touches the data model / vault contract / network.
- **Brief:** "Read the spec for module X and the existing tests in `tests/`. Without writing the implementation, produce a test plan: what fixtures, what happy-path tests, what edge cases, what failure modes, what should be a cassette vs. a unit mock. List by file/test name. Don't write the tests yet; just plan."
- **Output:** A numbered test plan you can hand to the implementer.

### 5.2 `code-reviewer` — second pair of eyes on diffs
- **When:** Before committing any PR-sized change (multiple files, >100 lines, or any change to invariants).
- **Brief:** Provide the diff. "Review for: correctness, idiomatic Python 3.11, type-hint accuracy, consistency with the existing module style, mismatch with design invariants in CLAUDE.md §3. Flag anything surprising. Don't repeat what the tests already prove."
- **Output:** Punch list of concerns ranked by severity. You triage before committing.

### 5.3 `security-auditor` — focused review on attack surface
- **When:** Any change that touches: network input parsing (RSS feeds, GitHub API responses, HN payloads), URL canonicalization, prompt construction (synthesis agent), API key handling, logging in CI mode.
- **Brief:** "This is itself a security tool. The author cares about prompt injection, supply-chain attacks, and not leaking source-list URLs in public CI logs. Review this diff for: (1) any path where untrusted input reaches an LLM prompt without escaping/quoting boundaries, (2) URL parsing edge cases that could collapse distinct items into one or vice versa, (3) information leakage in error messages or logs, (4) API key handling. Don't be precious; be specific."
- **Output:** Concrete findings with file:line refs and recommended fixes.

### 5.4 `operations-reviewer` — production reality check
- **When:** Any change to CI workflows, GitHub Actions cron, vault inbox git flow, the `state` branch strategy, rate limit handling, or source health monitoring.
- **Brief:** "This will run unattended for months. Review for: (1) what happens on first run (cold state), (2) what happens after a 2-week outage, (3) what happens when a source moves or 404s for a week, (4) rate-limit budget assumptions vs. reality, (5) where a runaway loop could burn tokens, (6) state.db persistence and recovery, (7) git push failures and retries."
- **Output:** Failure-mode list with severity and suggested mitigations.

### 5.5 `devil's-advocate` — situational, when stuck or before locking in
- **When:** Before locking in a non-trivial design decision; when a milestone passes all tests but you have a nagging "feels off" sense; when debugging the same bug twice.
- **Brief:** "The current design / fix / module is below. Find the worst-case input that breaks it. Find the most likely production failure I haven't tested. Find the easiest way to subvert the system from the outside. Be specific and concrete."
- **Output:** Ordered list of attacks/failures, each with a reproducer sketch.

### 5.6 `source-list-researcher` — populate / refresh sources.yaml
- **When:** Start of M4 (initial source list); periodically thereafter (every few months) to refresh.
- **Brief:** "Compile a `sources.example.yaml` for an AI-security researcher focused on MCP supply chain, agentic coding, and prompt injection. Categories: RSS blogs, GitHub repos (releases + topics), arXiv categories with query strings, HN searches. For each candidate source, give: name slug, type, config block, one-line justification, signal/noise estimate. Aim for ~50 entries. Verify each feed/URL is live."
- **Output:** A `sources.example.yaml` file + a notes section flagging anything uncertain.

### 5.7 `golden-set-curator` — labeled classification eval set
- **When:** During M3 (build initial set) and periodically as the taxonomy or focus shifts.
- **Brief:** "Read `config/golden_set.jsonl` if it exists. Read the current taxonomy in `config/agent.yaml`. From the past month of stored items (query SQLite), produce 20-50 hand-labeled `(item, topic, score)` rows. Avoid items where the classification is obvious (no signal); pick the ambiguous ones. Include rationale per item."
- **Output:** Updated `config/golden_set.jsonl`.

### 5.8 `docs-updater` — keep README/CHANGELOG/docstrings in sync
- **When:** Final step of every milestone; after any change to a public-facing interface (CLI flag, env var, config field, vault frontmatter).
- **Brief:** "Compare the current README quick-start, CHANGELOG, and module docstrings against the actual current behavior. Identify drift. Produce a unified diff."
- **Output:** Diff for you to apply.

### 5.9 `spec-enforcer` — anti-drift check
- **When:** Quarterly, or at the end of each milestone, or when a sub-agent suggests something that feels out of scope.
- **Brief:** "Read `docs/researcher-agent-spec.md` and CLAUDE.md §3. Read the current code. Identify drift: where has the implementation diverged from the spec/invariants? Some drift is intentional and improvements; flag both kinds and distinguish them."
- **Output:** Drift report with severity and recommendation (codify drift in CLAUDE.md vs. revert to spec).

### Spawn rules of thumb
- Don't spawn for trivial changes (single-function fixes, typos, test additions).
- Sequential by default; parallel only when the reviews are genuinely independent (e.g., code-reviewer + security-auditor on the same diff can run in parallel).
- Each spawn includes a self-contained brief — the sub-agent has no memory of the conversation.
- Trust but verify: read the sub-agent's output and don't apply its suggestions blindly. They're advisory.

---

## 6. Milestone M2 — COMPLETE (M3 also complete — see §7; next: M4)

**Goal:** First source vertical slice + polite HTTP foundation + entity extraction. After M2, `researcher collect --since X --until Y` produces a real collection report (containing only RSS items, no classifier yet — classification lands in M3, so the reports show un-classified items with `topic="unclassified"` as a placeholder, OR M2 skips vault rendering and stops at the storage layer; pick whichever keeps M2 small).

> **M2 resolution notes (decisions made during the build):**
> - **Vault rendering: SKIPPED in M2; collect stops at the storage layer.** No classifier exists yet, and rendering a "collection report" requires a `Classification`. Inventing a fake `topic="unclassified"` row would pollute the append-only classification history (invariant #2) with a topic outside the taxonomy. Items are stored with `current_classification_id = NULL`; vault rendering returns in M3 with real classifications. (CLAUDE.md offered either choice; this keeps M2 small.)
> - **arXiv IDs are NOT emitted as `ItemEntity`.** `EntityKind` (invariant #4) has no `arxiv` member, and per §10 invariants win over the spec/step-6 wording. arXiv identity is captured by URL canonicalization (`canonicalize.extract_arxiv_id`) instead. Revisit only if a paper-reference entity kind is explicitly wanted.
> - **Item.url stores the canonical URL** (tracking-stripped, arXiv-collapsed); the original is kept in `metadata["original_url"]` only when it differs.
> - **Entity output is capped at 64/item** and extraction runs over HTML-stripped text — a live run showed a full-HTML blogspot feed producing 36k+ repo-shaped false positives otherwise.
> - **Security/ops review fixes applied:** production log mode drops exception message bodies (host leak via DNS/SSL errors, not just URLs); `Retry-After` is clamped; IDN hosts are punycode-normalized. Other findings deferred to M4 — see CHANGELOG "Deferred to M4".

**Sub-steps** (each is a logical commit):

1. **HTTP client foundation (`researcher_agent/http.py`).** Shared `httpx.Client` wrapper with:
   - Descriptive `User-Agent` (`researcher-agent/0.1 (+https://github.com/TomerBenda/researcher-agent)`)
   - Per-host concurrency limit (`max_keepalive_connections=1` per host, semaphore at call sites)
   - `Retry-After` on 429 honored
   - Conditional GET support (`If-Modified-Since`, `If-None-Match`) — caller supplies, client passes through
   - Configurable per-host `min_interval_seconds`
   - Tests: real or recorded fixtures via `pytest-recording`.

2. **Source adapter Protocol + Pydantic config (`researcher_agent/sources/base.py`).** Each adapter declares its own config model. Loader instantiates the right model per `type:` in `sources.yaml`. Errors at startup, not mid-run.

3. **RSS adapter (`researcher_agent/sources/rss.py`).** `feedparser`-based. Uses the shared HTTP client. Returns `list[RawItem]` with the feed entry as `payload`. Honors per-source ETag/Last-Modified cursors stored in `source_runs.cursor_json`.

4. **Normalize for RSS (`researcher_agent/normalize.py`).** `normalize_rss(raw) -> tuple[Item, list[ItemEntity]]`. Computes `canonical_hash` per the URL rules (see invariant #4 and the spec's §5.2). Extracts entities (see step 5).

5. **URL canonicalizer (`researcher_agent/canonicalize.py`).** Lowercased scheme + host, default port stripped, tracking params stripped from the default list (extend in config), trailing slash normalized. arXiv-specific normalizer: extract arxiv ID, canonicalize to `https://arxiv.org/abs/{id}` regardless of input form (`/abs/`, `/pdf/`, with/without version). `canonicalization_version = 1`.

6. **Regex entity extractor (`researcher_agent/entities.py`).** Compiled patterns for: CVE-YYYY-NNNNN+, `owner/repo` (GitHub-shaped, ignoring slashes inside URL paths), `npm:name` / `pypi:name`, arXiv IDs. Returns `list[ItemEntity]`. Called from normalize. Test with a fixture of real-world strings.

7. **Wire RSS through the collect command.** `researcher collect` reads `config/sources.yaml`, instantiates each adapter, fetches new items, normalizes, stores via `Database`. For M2: skip classifier and vault rendering OR write a minimal "unclassified" report — pick one and document. Update CLI to accept `--source NAME` for targeted runs.

8. **Live smoke test workflow (`.github/workflows/smoke.yml`).** Manual `workflow_dispatch` only. Runs against real sources, reports any source that errored or returned zero items. **Not** in default CI; tokens-burner.

**M2 done definition** (in addition to §4 generic criteria):
- [x] Collection from at least 3 real RSS feeds works end-to-end on a local machine. *(verified: 274 items from 4 live feeds.)*
- [x] Re-running collect against the same feeds is idempotent: `source_runs.cursor_json` advances, no duplicate items inserted. *(verified: second live run reported 0 new items.)*
- [x] HTTP politeness verified: collect against a single source twice; second call uses cached response via 304. *(verified live: all feeds returned 304 on the second run; also covered deterministically in `tests/test_sources_rss.py` / `tests/test_collect.py`.)*
- [x] `security-auditor` reviewed the normalize + canonicalize + entities code. *(findings: error-host-leak HIGH, IDN false-split MEDIUM — both fixed; ReDoS clear.)*
- [x] `operations-reviewer` reviewed the HTTP client + source_runs cursor handling. *(finding: unbounded Retry-After HIGH — fixed; rest deferred to M4, see CHANGELOG.)*

---

## 7. Future milestones (summary)

### M3 — Classifier + dedupe + golden set — COMPLETE
- Gemini provider via `google-genai` SDK; Ollama provider as fallback.
- Prompt-as-file (`researcher_agent/prompts/classify.md`), hashed at load, hash → `classifier_version`.
- Per-item retry (not per-batch). Token budget pre-flight.
- Two-pass dedupe: within-batch (cheap) + against-DB (joined with entity overlap).
- Golden-set curator spawned to produce `config/golden_set.jsonl` (≥20 to start, ≥50 by end of M3).
- `make test-golden` runs the classifier against the golden set, asserts ≥85% top-1.

> **M3 resolution notes (decisions made during the build):**
> - **Provider seam:** orchestration depends only on a `ClassifierProvider` Protocol; real providers (Gemini/Ollama) are thin and share `render_items_message` + `parse_classifications`. The real-API path is exercised only by the opt-in golden eval — all other tests use a fake provider, so the suite is network/token-free.
> - **Classifier user message is a JSON array, not concatenated text** (security review): untrusted feed content is JSON-escaped so it can't forge item boundaries / inject instructions. Item ids are the `canonical_hash` (not feed-controlled); the parser keeps the first result per id.
> - **Transient vs content failure** (ops review, load-bearing): a provider *error* (rate-limit/outage) leaves items **unclassified** (skipped → re-tried next run), never persisted as `(other,3)`. Only a real response that can't classify an item falls back. A circuit breaker aborts after `max_consecutive_failures` (default 3). Cold-start cost is bounded by `classifier.max_items_per_run` (default 200) and the dedupe pool size guard.
> - **Golden set is 25 items** (spec said grow to ≥50 by end of M3) — a solid ambiguity-favoring starter set covering all 10 slugs; grow it as the taxonomy/focus shifts. The ≥85% top-1 assertion has NOT been run here (no `GEMINI_API_KEY`); run `make test-golden` with a key to validate before relying on the classifier.
> - **`config/agent.yaml` is gitignored** (it carries the personal `vault_path`, invariant #20); `config/agent.example.yaml` is the public artifact. The remaining review items (in-provider 429 backoff, token-estimate accuracy, dedupe blocking) are deferred to M4 — see CHANGELOG.

### M4 — Remaining sources + sources.yaml + collect cron
- GitHub releases adapter (via raw `httpx`, **not** PyGithub).
- GitHub topic-search adapter.
- arXiv adapter.
- HN search adapter (Algolia, no auth).
- `source-list-researcher` produces draft `sources.yaml` for review.
- `.github/workflows/collect.yml` runs `researcher collect` daily; commits state.db to `state` branch.
- `validate-config` and `status` CLI commands.

### M5 — Synthesis agent + tools
- Tools (each independently testable): `query_items`, `fetch_url` (with body cache check), `get_entities`, `add_followup`, `finish`. Tools return structured `{ok, result|error}` shapes — never raise into the agent loop.
- Agent loop: Anthropic primary (via `anthropic` SDK), Gemini fallback. Token-budget accumulator, degraded-finish path on turn-limit/budget-exhaustion.
- Prompt-as-file (`researcher_agent/prompts/synthesize.md`).
- LLM response cache for development iteration (off in production).
- `.github/workflows/synthesize.yml` runs weekly; commits synthesis report to `tbd-research-inbox`.

After M5: project is operational. Iteration is config + prompt tuning, not new code.

---

## 8. Common pitfalls (observed during M1)

- **File truncation by the writer.** Long files have been truncated mid-content multiple times. After any large file write, **verify** with `wc -l` and `tail` that the file ends as expected. If truncated, splice the missing tail via `bash` heredoc rather than re-running Write.
- **Sandbox `rmdir` restrictions.** The Cowork bash sandbox can't `rmdir` host-mounted directories. Empty `migrations/` at repo root is one example; the user must clean these locally. Use `mcp__cowork__allow_cowork_file_delete` if needed for files; directories may require local cleanup.
- **pytest's `tmp_path` on the mount.** Default basetemp may hit permission issues. Pass `--basetemp=/tmp/pytest-rb` when running tests from the sandbox.
- **Sandbox Python version (3.10) vs. project target (3.11+).** The sandbox uses 3.10 which lacks `datetime.UTC` (3.11+). Tests can be run via a one-line monkey-patch: `python3 -c "import datetime; datetime.UTC = datetime.timezone.utc; import pytest, sys; sys.exit(pytest.main(...))"`.
- **Ruff `UP017`** auto-fix converts `timezone.utc` → `datetime.UTC`. That's the 3.11+ idiom and is correct for the project; the sandbox monkey-patch above is the workaround.
- **mypy cache I/O errors.** mypy's SQLite cache can fail on the mount. Pass `--cache-dir=/tmp/mypy-cache`.
- **YAML frontmatter is alpha-sorted** by `sort_keys=True`. That's the contract — don't try to make it visually "nicer" at the cost of reproducibility.
- **Snapshots are committed.** Don't regenerate them silently. Intentional rendering changes get an `UPDATE_SNAPSHOTS=1 pytest` run AND a CHANGELOG entry explaining the change.
- **The `daily`/`weekly` framing is dead.** If you see those words in code, comments, or filenames, that's a regression. The framing is `collect`/`synthesize`, periodicity-agnostic.

---

## 9. Quick-reference commands

```bash
# Local dev
make install          # uv sync
make check            # lint + typecheck + test
make test             # pytest only
make format           # ruff format + auto-fix

# Targeted runs (M2+)
uv run researcher collect --source rss:simon-willison
uv run researcher synthesize --days 30 --label D2026-05-27-30d

# Snapshot regen
UPDATE_SNAPSHOTS=1 uv run pytest tests/test_vault.py

# Golden-set eval (M3+, costs tokens)
make test-golden
```

---

## 10. When in doubt

Read `docs/researcher-agent-spec.md` §1–3 and §10–13. Then re-read this CLAUDE.md §3. If the spec and the invariants conflict, the invariants win (they reflect the design-review pushback that happened after the spec was written). If you'd want to change an invariant, open a discussion with the user before any code.
