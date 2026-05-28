# Researcher Agent — Build Specification

*Clean-slate spec for a personal AI security research intelligence agent. For a coding agent to implement with Tomer reviewing. May 26, 2026.*

---

## 1. Purpose

A personal research intelligence agent that keeps Tomer saturated with high-signal inputs for his agentic AI security research, with current focus on MCP supply chain. It ingests from many sources, classifies items into a research-specific taxonomy, dedupes across sources, surfaces classified items to his Obsidian vault daily, and produces a real synthesis weekly.

**Two-mode design:**
- **Daily pipeline** — deterministic, free-tier-only, ~50 sources, ~hundreds of items/week. Job: classify, score, dedupe, write to vault.
- **Weekly agent** — a tool-using LLM agent run once per week against the past 7 days. Job: read the classified items, fetch full content for the interesting ones, extract entities (CVEs, projects, people, techniques), connect themes, write a roundup, queue follow-ups.

**Operating principles:**
- Free-tier API by default. Optional small Claude API spend for the weekly agent only.
- Diff-reviewable code: small modules, explicit dependencies, no magic.
- Public repo from day one — the codebase is itself a research artifact.

---

## 2. Acceptance Criteria

A reviewer should be able to verify:

1. `researcher daily` ingests from all configured sources, classifies and dedupes, writes a daily digest markdown file to the configured Obsidian path.
2. `researcher weekly` produces a weekly roundup file synthesizing the past 7 days, with extracted entities and follow-up queue.
3. GitHub Actions workflows run `daily` daily and `weekly` on Sundays, committing outputs.
4. Total API spend for one month with ~50 configured sources is **≤ $5 USD** (target $0 for daily, small spend acceptable for weekly).
5. Classification accuracy on a labeled 50-item golden set is ≥ 85% top-1 topic match.
6. Adding a new source of a supported type requires only a `config/sources.yaml` edit, no code.
7. The weekly roundup successfully fetches and quotes at least one full-text URL it didn't see in the daily digest summaries (proves the agent's tool use works end-to-end).
8. Test suite passes; coverage ≥ 70% on non-IO modules.

---

## 3. Data Model

This is the spine. Everything else is mechanics around these shapes.

### 3.1 Taxonomy (single-label, top-1)

| Slug | Description |
|---|---|
| `mcp-ecosystem` | New MCP servers, releases, ecosystem news, MCP-related tooling |
| `mcp-security` | MCP-specific vulnerabilities, advisories, security analyses |
| `agent-security` | Agent security broadly: tool use, sandbox escapes, multi-agent issues |
| `prompt-injection` | Prompt injection findings, defenses, taxonomies |
| `coding-agents` | Claude Code, Cursor, Codex, Cline, OpenHands — security-relevant content |
| `browser-agents` | Agentic browsers — security-relevant content |
| `ai-safety-research` | Alignment, evals, capability research with security implications |
| `tooling` | Useful tools/libraries for AI security work |
| `other` | Worth keeping but doesn't fit cleanly |
| `noise` | Skip — marketing, off-topic, low quality |

**Score** (0–10): per-item importance for Tomer's current research focus. Anchored:
- 9–10: drop everything and read
- 7–8: read this week, take notes
- 5–6: skim
- 3–4: aware-of
- 0–2: filter out

### 3.2 Pydantic models

```python
class RawItem(BaseModel):
    """Source-specific shape, before normalization."""
    source_name: str
    payload: dict

class Item(BaseModel):
    """Normalized, canonical shape. The unit of currency."""
    source_name: str
    source_type: SourceType
    external_id: str
    canonical_hash: str            # for dedup
    url: HttpUrl
    title: str
    summary: str | None
    body: str | None               # full body if source provided
    published_at: datetime
    ingested_at: datetime

class ClassifiedItem(BaseModel):
    item: Item
    topic: Topic                   # enum from §3.1
    score: int                     # 0–10
    rationale: str
    classified_at: datetime
    classifier_model: str

class WeeklyEntity(BaseModel):
    """Extracted by the weekly agent."""
    kind: Literal["cve", "project", "person", "technique", "venue"]
    value: str
    context: str                   # one sentence
    related_item_ids: list[str]
```

### 3.3 Source types

```python
SourceType = Literal[
    "rss",
    "github_releases",
    "github_topic",
    "arxiv",
    "hn_search",
]
```

(Twitter/X, Reddit, generic web scraping: out of scope for now.)

---

## 4. Architecture

```
                          DAILY PIPELINE
┌─────────┐   ┌────────┐   ┌──────────┐   ┌──────────┐   ┌────────┐   ┌──────┐   ┌────────────┐
│ Sources │──▶│ Fetch  │──▶│Normalize │──▶│ Classify │──▶│ Dedupe │──▶│Store │──▶│Vault Writer│
└─────────┘   └────────┘   └──────────┘   └──────────┘   └────────┘   └──┬───┘   └────────────┘
                                                                          │
                                                                          ▼
                                                                ┌─────────────────────┐
                                                                │ SQLite state.db     │
                                                                └─────────────────────┘
                                                                          │
                          WEEKLY AGENT (Sundays)                          │
                                                                          ▼
                          ┌────────────────────────────────────────────────────────┐
                          │ Agent loop:                                            │
                          │   - Read past 7 days of ClassifiedItems                │
                          │   - Tool: fetch_url(url) → full text                   │
                          │   - Tool: hn_search(query) → items                     │
                          │   - Tool: extract_entities(text) → WeeklyEntity[]      │
                          │   - Write synthesis markdown to vault                  │
                          │   - Append items to follow-up queue                    │
                          └────────────────────────────────────────────────────────┘
```

The daily pipeline is a function. The weekly agent is an agent (LLM loop with tools, bounded turn count).

---

## 5. Module Specifications

### 5.1 Sources (`researcher_agent/sources/`)

Each adapter:

```python
class SourceAdapter(Protocol):
    type: SourceType

    def fetch(self, config: dict, since: datetime | None) -> list[RawItem]:
        """Return items published after `since` (or all recent if None)."""
```

Implementations:

- **`rss.py`** — `feedparser`-based. Config: `{url: str}`.
- **`github_releases.py`** — GitHub API releases. Config: `{repo: "owner/name"}`.
- **`github_topic.py`** — searches repos by topic, returns those updated since last run. Config: `{topic: str, min_stars: int}`.
- **`arxiv.py`** — arXiv API query. Config: `{query: str}` (uses arXiv's native query syntax).
- **`hn_search.py`** — Algolia HN search API, no auth required. Config: `{query: str}`.

Adapters return `RawItem`s. They do not normalize, classify, or dedupe.

### 5.2 Normalization (`researcher_agent/normalize.py`)

Pure function: `normalize(raw: RawItem) -> Item`. Dispatches on `source_type`. Each source has its own normalizer. `canonical_hash` is computed here.

Canonicalization rules for URLs:
- Lowercase scheme and host
- Strip default ports
- Strip tracking params: `utm_*`, `ref`, `fbclid`, `gclid`, `mc_*`, etc. (configurable list)
- Strip trailing slash from path unless path is `/`
- SHA-256 hex of the resulting URL string

### 5.3 Classifier (`researcher_agent/classify.py`)

Pure function with side effect: `classify(items: list[Item]) -> list[ClassifiedItem]`. Side effect = LLM API call.

**Model selection** (from `config/agent.yaml` → `classifier.provider`):
- `gemini` (default): `gemini-2.5-flash` via `google-genai`, free tier
- `ollama`: local model via Ollama HTTP, fully offline
- `anthropic`: optional, paid

**Batching:** classify 10 items per call. Reduces overhead and fits well under any context window.

**Prompt template** (refine during build):

```
SYSTEM:
You classify items for an AI security research feed. The researcher is currently
focused on MCP (Model Context Protocol) supply chain attacks. Weight ecosystem
and security items in that area higher than generic AI safety content.

Taxonomy: {taxonomy_with_descriptions_and_examples}

For each item, output JSON: 
{"topic": "<slug>", "score": <0-10 int>, "rationale": "<≤15 word sentence>"}

USER:
Item 1:
  Title: {title}
  Source: {source_name}
  Summary: {summary or "(none)"}
  URL: {url}

Item 2: ...
```

Provider must return structured output (Gemini and Anthropic support JSON mode; for Ollama use the `format: json` flag). Validate with Pydantic. On parse failure, retry once with `temperature=0`. On second failure, default to `topic="other", score=3`.

### 5.4 Dedupe (`researcher_agent/dedupe.py`)

Two-stage:
1. **Canonical hash match.** Items with identical `canonical_hash` are the same.
2. **Fuzzy title match.** For items with different hashes, Levenshtein ratio ≥ 0.92 on titles AND published within 48h are duplicates.

When duplicates exist, keep the highest-scored copy and merge `source_names` into one list. Lower-scored copies are recorded but not surfaced.

### 5.5 Storage (`researcher_agent/state.py`)

SQLite at `.researcher/state.db`. Schema:

```sql
CREATE TABLE items (
    canonical_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    body TEXT,
    published_at TIMESTAMP NOT NULL,
    ingested_at TIMESTAMP NOT NULL,
    topic TEXT,
    score INTEGER,
    rationale TEXT,
    classifier_model TEXT,
    classified_at TIMESTAMP,
    sources TEXT  -- JSON array of source_names
);
CREATE INDEX idx_items_published ON items(published_at);
CREATE INDEX idx_items_topic_score ON items(topic, score DESC);

CREATE TABLE source_runs (
    source_name TEXT PRIMARY KEY,
    last_run_at TIMESTAMP,
    last_success_at TIMESTAMP,
    last_error TEXT
);

CREATE TABLE weekly_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_starting DATE NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    context TEXT,
    related_item_hashes TEXT  -- JSON array
);

CREATE TABLE followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP NOT NULL,
    item_hash TEXT,
    action TEXT NOT NULL,        -- "read-deep" | "audit" | "track" | "reach-out"
    note TEXT,
    completed BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (item_hash) REFERENCES items(canonical_hash)
);
```

### 5.6 Vault writer (`researcher_agent/vault.py`)

**Daily digest** at `{vault_path}/inbox/research-intel/{YYYY-MM-DD}.md`:

```markdown
---
date: 2026-05-26
type: daily-digest
schema_version: 1
counts:
  mcp-ecosystem: 4
  mcp-security: 1
  agent-security: 2
  total: 12
---

# Research Intel — 2026-05-26

## mcp-security (1)

### [Title here](https://...) — score 8
> One-sentence rationale from classifier.
- **Source:** rss:embracetheRed
- **Published:** 2026-05-25

## mcp-ecosystem (4)

(per-item structure as above)
```

**Weekly roundup** at `{vault_path}/inbox/research-intel/weekly/{YYYY-Www}.md`. Frontmatter `type: weekly-roundup`. Body is whatever the weekly agent (§5.7) produces, with appended sections for extracted entities and follow-up queue.

Frontmatter is the contract for Obsidian Dataview queries. Don't change field names without bumping `schema_version`.

### 5.7 Weekly agent (`researcher_agent/weekly.py`)

Runs once on Sundays. An LLM-driven agent loop, not a pipeline.

**Provider:** prefers Anthropic (Claude) for the weekly run because synthesis quality matters here. Falls back to Gemini 2.5 Flash if `ANTHROPIC_API_KEY` not set. Expected weekly cost on Anthropic: < $0.20.

**Input:** all `ClassifiedItems` from the past 7 days where `score >= 5`. Loaded as JSON, passed in the initial user message.

**Tools the agent has:**

```python
def fetch_url(url: str) -> str:
    """Fetch full text content of a URL. Returns readable text or error."""

def search_hn(query: str, max_results: int = 5) -> list[dict]:
    """Search Hacker News for related discussion."""

def extract_entities(text: str) -> list[WeeklyEntity]:
    """Extract CVEs, projects, people, techniques from text. Uses regex
    + small LLM call."""

def add_followup(item_hash: str, action: str, note: str) -> None:
    """Queue a follow-up for Tomer to act on."""

def finish(roundup_markdown: str) -> None:
    """Terminate the agent loop with the final roundup."""
```

**Stopping criteria:** agent calls `finish()`, OR 20 tool-call turns elapsed, OR token budget exhausted. Whichever first.

**System prompt** (sketch — iterate during build):

```
You are Tomer's weekly research intelligence assistant. He is an AI security
researcher focused on MCP supply chain. Your job: given the past week's
classified items, produce a markdown roundup that:

1. Identifies 2-4 themes that connect multiple items
2. Highlights items that deserve deep reading (use fetch_url to verify your
   assessment when uncertain)
3. Extracts entities — CVEs, projects, people, techniques — using
   extract_entities on the most important items
4. Queues follow-ups (add_followup) for items requiring action: deep read,
   audit, track, reach out
5. Calls finish() with the complete roundup markdown

Be concrete. Be useful. Skip items Tomer can ignore. Quote ≤15 words
verbatim from any source; otherwise paraphrase.
```

Agent output is appended with extracted entity table and follow-up queue (rendered from `weekly_entities` and `followups` SQLite tables) before writing to vault.

### 5.8 CLI (`researcher_agent/__main__.py`)

Typer-based:

```
researcher daily            # run daily pipeline
researcher weekly           # run weekly agent
researcher fetch <source>   # debug: fetch one source, print to stdout
researcher classify-file <path>  # classify a JSONL file, print results
researcher backfill --days 30    # re-fetch and reclassify past N days
researcher followups [--open|--all]  # list follow-up queue
researcher mark-done <id>   # mark a follow-up done
```

---

## 6. Configuration

`config/agent.yaml`:

```yaml
vault_path: /mnt/c/Users/tomer/Obsidian/Vault
research_focus: |
  AI security research, currently emphasizing MCP supply chain attacks,
  agentic coding/browser tools, and prompt-injection-adjacent vulnerabilities.
classifier:
  provider: gemini       # gemini | ollama | anthropic
  model: gemini-2.5-flash
  batch_size: 10
weekly:
  provider: anthropic    # anthropic | gemini
  model: claude-sonnet-4-5
  max_turns: 20
  token_budget: 200000
dedup:
  fuzzy_title_threshold: 0.92
  fuzzy_window_hours: 48
tracking_params_to_strip:
  - utm_source
  - utm_medium
  - utm_campaign
  - ref
  - fbclid
  - gclid
log_level: INFO
```

`config/sources.yaml`:

```yaml
sources:
  - name: rss:embracetheRed
    type: rss
    url: https://embracethered.com/blog/index.xml
  - name: rss:simon-willison
    type: rss
    url: https://simonwillison.net/atom/everything/
  - name: gh-releases:modelcontextprotocol/servers
    type: github_releases
    repo: modelcontextprotocol/servers
  - name: gh-topic:mcp-server
    type: github_topic
    topic: mcp-server
    min_stars: 5
  - name: arxiv:agent-security
    type: arxiv
    query: 'cat:cs.CR AND (abs:"agent" OR abs:"prompt injection")'
  - name: hn:mcp
    type: hn_search
    query: '"model context protocol" OR mcp-server'
  # Add more — Tomer's existing list goes here
```

---

## 7. Tech Stack

- **Python 3.11+**. Strict type hints. `from __future__ import annotations`.
- **uv** for deps and virtual env.
- **Pydantic v2** for all data models.
- **httpx** (sync) for HTTP.
- **feedparser**, **PyGithub** for source adapters.
- **google-genai**, **anthropic**, **ollama** Python SDKs.
- **Typer** for CLI.
- **SQLite** via `sqlite3` stdlib.
- **PyYAML** for config.
- **ruff** lint, **mypy --strict** types, **pytest** tests, **pytest-recording** HTTP fixtures.
- **Makefile** entrypoints: `make daily`, `make weekly`, `make test`, `make lint`.

Style: explicit over clever. No metaclasses, no decorator routing, no magic config loaders.

---

## 8. File Structure

```
researcher-agent/
├── pyproject.toml
├── Makefile
├── README.md                   # public — this is a research artifact
├── LICENSE                     # MIT or Apache-2.0, Tomer to pick
├── config/
│   ├── agent.yaml
│   ├── sources.yaml
│   └── golden_set.jsonl        # labeled classification eval set
├── researcher_agent/
│   ├── __init__.py
│   ├── __main__.py             # CLI
│   ├── models.py               # Pydantic models, taxonomy enums
│   ├── normalize.py
│   ├── classify.py
│   ├── dedupe.py
│   ├── state.py                # SQLite layer
│   ├── vault.py                # markdown rendering, file writes
│   ├── pipeline.py             # daily pipeline orchestration
│   ├── weekly.py               # weekly agent loop
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py             # SourceAdapter Protocol
│   │   ├── rss.py
│   │   ├── github_releases.py
│   │   ├── github_topic.py
│   │   ├── arxiv.py
│   │   └── hn_search.py
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py             # LLM provider Protocol
│   │   ├── gemini.py
│   │   ├── ollama.py
│   │   └── anthropic.py
│   └── tools/                  # tools available to the weekly agent
│       ├── __init__.py
│       ├── fetch_url.py
│       ├── search_hn.py
│       └── extract_entities.py
├── tests/
│   ├── test_normalize.py
│   ├── test_dedupe.py
│   ├── test_classify.py
│   ├── test_vault.py
│   ├── test_sources/
│   │   └── ... (one per source, with recorded fixtures)
│   ├── test_weekly_agent.py    # with mocked LLM + tools
│   └── test_golden.py          # classifier accuracy eval
└── .github/workflows/
    ├── daily.yml
    ├── weekly.yml
    └── ci.yml
```

---

## 9. Testing Strategy

- **Unit tests** for `normalize`, `dedupe`, `vault`, `state` (no network, no LLM).
- **Source tests** use `pytest-recording` cassettes. One happy-path test per source minimum.
- **Classifier tests:**
  - Mock provider for behavioral tests (batching, retry on parse failure).
  - Golden test (`tests/test_golden.py`) runs the *real* configured classifier on `config/golden_set.jsonl` (50+ labeled items). Asserts ≥85% top-1 accuracy. Not in CI (costs tokens), runs on demand via `make test-golden`.
- **Weekly agent tests:** mock the LLM client and the tool implementations. Assert the agent loop hits `finish()`, calls expected tool patterns, produces parseable markdown. No real LLM calls in CI.
- **E2E test:** run the daily pipeline against fixture sources, write to a tempdir vault, snapshot-test the markdown output.

CI: lint + typecheck + unit + source + weekly-mocked + e2e. Golden + real-API weekly: manual.

---

## 10. Build Order

For the coding agent — one phase per session is the right cadence. Don't try to do everything in one shot.

1. **Skeleton.** Project layout, pyproject.toml, Makefile, ruff/mypy/pytest configs, empty modules with type stubs, CI workflow.
2. **Models + state + vault.** All pure-Python: Pydantic models, SQLite layer, markdown rendering. Unit-tested. Should feel rock-solid before moving on.
3. **One source end-to-end.** Implement `rss.py` only. Wire through normalize → store → vault. Smoke test on real feeds.
4. **Classifier.** Build the Gemini provider first, then Ollama. Build a starter golden set inline (20 items, grow to 50+).
5. **Dedupe.** Canonical hash first, fuzzy match second. Tests with real-looking duplicate pairs.
6. **Remaining sources.** GitHub releases, GitHub topic, arXiv, HN search. One PR per source ideally.
7. **CLI + daily pipeline integration.** `researcher daily` works end-to-end.
8. **GitHub Actions daily.yml.** Run on schedule for a week to validate.
9. **Tools for the weekly agent.** `fetch_url`, `search_hn`, `extract_entities`. Each one independently testable.
10. **Weekly agent loop.** Anthropic provider as primary, Gemini fallback. Heavy on prompt iteration here.
11. **GitHub Actions weekly.yml.** Run for two cycles to validate.

After step 11, the project is operational. Iteration from there is config + prompt tuning, not new code.

---

## 11. Out of Scope (do not build)

- Web UI / dashboard
- Real-time alerts (Slack, email)
- Multi-user support
- Twitter/X / Reddit ingestion
- The MCP server scanner (separate project)
- Direct MCP server source-code analysis
- Authentication beyond environment-variable API keys
- A plugin system for sources (the Protocol is enough)
- LLM synthesis in the daily pipeline (only the weekly agent synthesizes)

---

## 12. Public Repo Considerations

This repo is public from day one. Implications:

- README is the project's first business card. Write it like a research artifact: what the agent is, why it exists, the design choices and tradeoffs, how someone else might fork it for their own focus. This README is its own credibility marker — make it good.
- Do not commit API keys, the vault path (it leaks file system structure), or the SQLite state file. `.gitignore` strictly.
- Do not commit Tomer's actual feed list with personal source picks if any are private/identifying. Provide a `sources.example.yaml`; Tomer keeps `sources.yaml` gitignored.
- License: MIT or Apache-2.0, Tomer's call.
- Encourage contributions on `sources/` and the taxonomy. Discourage them on weekly-agent prompts (those are personal).

---

## 13. Open Questions

Resolve before the coding agent starts:

1. **Vault commit destination.** Does the agent commit directly to the vault Git repo, or write to a separate "research-inbox" repo that Obsidian syncs from? Recommend the latter — keeps vault history clean.
2. **API key setup.** Tomer to confirm:
   - `GEMINI_API_KEY` from Google AI Studio (free tier)
   - `ANTHROPIC_API_KEY` for optional weekly synthesis (small spend)
   - `GITHUB_TOKEN` (the default Actions one suffices for read-only API access)
3. **Initial source list.** Tomer to provide his preferred sources as `sources.yaml`. Spec provides a starting set; add to it.
4. **License.** MIT or Apache-2.0?
5. **Repo name.** Spec uses `researcher-agent` (the obvious one). Confirm or pick a different name — the name will appear in his public output for a long time.