# researcher-agent

A personal AI security research intelligence agent. Ingests from configured sources (RSS, GitHub, arXiv, Hacker News), classifies items into a research-specific taxonomy, dedupes across sources, writes daily digests to an Obsidian vault, and produces a real synthesis weekly via a tool-using LLM agent.

Built and operated by [tbd](https://github.com/tbd). Public from day one as a research artifact; fork it for your own research focus.

## Two flows

Two functions over a shared substrate (SQLite store + Obsidian vault), each invokable independently with explicit window parameters. Periodicity is an orchestration concern, not part of the function.

- **Collection** (`researcher collect`) — gather from configured sources, classify, dedupe, store; optionally render a collection report to the vault. Default schedule: daily via GitHub Actions, but you can run it ad-hoc with any window.
- **Synthesis** (`researcher synthesize`) — read stored items over a window, run the tool-using LLM agent, extract entities, queue follow-ups, render a synthesis report. Default schedule: weekly, but ad-hoc runs over custom windows are first-class.

## Quick start

```bash
uv 