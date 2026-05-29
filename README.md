# researcher-agent

A personal AI security research intelligence agent. Ingests from configured sources (RSS, GitHub, arXiv, Hacker News), classifies items into a research-specific taxonomy, dedupes across sources, writes daily digests to an Obsidian vault, and produces a real synthesis weekly via a tool-using LLM agent.

Built and operated by [tbd](https://github.com/TomerBenda). Public from day one as a research artifact; fork it for your own research focus.

## Two flows

Two functions over a shared substrate (SQLite store + Obsidian vault), each invokable independently with explicit window parameters. Periodicity is an orchestration concern, not part of the function.

- **Collection** (`researcher collect`) — gather from configured sources, classify, dedupe, store; optionally render a collection report to the vault. Default schedule: daily via GitHub Actions, but you can run it ad-hoc with any window.
- **Synthesis** (`researcher synthesize`) — read stored items over a window, run the tool-using LLM agent, extract entities, queue follow-ups, render a synthesis report. Default schedule: weekly, but ad-hoc runs over custom windows are first-class.

## Quick start

```bash
uv sync
cp config/sources.example.yaml config/sources.yaml   # your feeds
cp config/agent.example.yaml   config/agent.yaml      # taxonomy + classifier + vault_path
export GEMINI_API_KEY=...                             # free tier from Google AI Studio

# gather -> classify -> dedupe -> render a collection report
uv run researcher collect

# collect without the LLM step (just fetch + store)
uv run researcher collect --no-classify

# target a single source, or constrain the window
uv run researcher collect --source rss:simon-willison
uv run researcher collect --since 2026-05-01 --until 2026-05-28
```

`collect` is idempotent and polite: it honors per-source ETag / Last-Modified
caching (a re-run against unchanged feeds does no work) and stores into
`.researcher/state.db` by default (`--db` to override). Classification is skipped
gracefully if no `GEMINI_API_KEY` (or `config/agent.yaml`) is present — items are
still stored, just left unclassified for a later run.

```bash
uv run researcher validate-config   # check sources.yaml + agent.yaml (offline)
uv run researcher status             # per-source health + store totals
make test          # unit + integration suite (no network, no tokens)
make test-golden   # opt-in: real classifier vs config/golden_set.jsonl (costs tokens)
```

See [`docs/researcher-agent-spec.md`](docs/researcher-agent-spec.md) for the full design.

## Status

Early build — see `CHANGELOG.md`. As of **M4**, `collect` ingests **all five
source types** (RSS, arXiv, Hacker News, GitHub releases, GitHub topic search),
runs the full pipeline (canonicalize → entities → classify → dedupe → render),
and runs **daily via GitHub Actions**, persisting `state.db` to a dedicated
`state` branch. `status` / `validate-config` aid unattended operation. The
`synthesize` agent lands in M5.

## License

MIT — see [LICENSE](LICENSE).
