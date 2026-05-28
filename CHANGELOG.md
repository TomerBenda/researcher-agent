# Changelog

All notable changes to this project will be documented in this file.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Project scaffold, Pydantic data model, SQLite schema, storage layer, vault renderer with snapshot tests, CI workflow (M1 complete).

### Changed
- Reframed `daily` / `weekly` flows as `collect` / `synthesize` to decouple function from cadence. Schedule is now an orchestration concern; both commands are window-parameterized.
- Vault frontmatter `type:` values now `collection-report` and `synthesis-report` (was `daily-digest` and `weekly-roundup`). `schema_version` bumped to 2.
- Vault layout: collection reports at `{vault}/collection/{YYYY-MM-DD}.md`, synthesis reports at `{vault}/synthesis/{label}.md` where `label` encodes the window (e.g. `W2026-W22`, `D2026-05-27-30d`).
