# researcher-agent

A personal AI security research intelligence agent. Ingests from configured sources (RSS, GitHub, arXiv, Hacker News), classifies items into a research-specific taxonomy, dedupes across sources, writes daily digests to an Obsidian vault, and produces a real synthesis weekly via a tool-using LLM agent.

Built and operated by [tbd](https://github.com/tbd). Public from day one as a research artifact; fork it for your own research focus.

## Two flows

Two functions over a shared substrate (SQLite store + Obsidian vault), each invokable independently with explicit window parameters. Periodicity is an orchestration concern, not part of the function.

- **Collection** (`researcher collect`) — gather from configured sources, classify, dedupe, store; optionally render a collection report to the vault. Default schedule: daily via GitHub Actions, but you can run it ad-hoc with any window.
- **Synthesis** (`researcher synthesize`) — read stored items over a window, run the tool-using LLM agent, extract entities, queue follow-ups, render a synthesis report. Default schedule: weekly, but ad-hoc runs over custom windows are first-class.

## Quick start

```bash
uv sync
cp config/sources.example.yaml config/sources.yaml
# edit config/sources.yaml to taste (RSS sources work today)

# gather from your sources and store to the local SQLite state db
uv run researcher collect

# target a single source, or constrain the window
uv run researcher collect --source rss:simon-willison
uv run researcher collect --since 2026-05-01 --until 2026-05-28
```

`collect` is idempotent and polite: it honors per-source ETag / Last-Modified
caching (a re-run against unchanged feeds does no work) and stores into
`.researcher/state.db` by default (`--db` to override).

See [`docs/researcher-agent-spec.md`](docs/researcher-agent-spec.md) for the full design.

## Status

Early build — see `CHANGELOG.md`. As of **M2**, `collect` ingests RSS sources
end-to-end (fetch → canonicalize → extract entities → store). Classification
(`GEMINI_API_KEY`), cross-source dedupe, the remaining source types, vault
rendering, and the `synthesize` agent land in M3–M5.

## License

MIT — see [LICENSE](LICENSE).
