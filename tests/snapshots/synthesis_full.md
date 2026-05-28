---
entity_counts:
  cve: 2
  person: 1
  repo: 2
  technique: 1
followup_count: 4
renderer_version: 1
schema_version: 2
type: synthesis-report
window:
  end: '2026-06-01T00:00:00+00:00'
  label: W2026-W22
  start: '2026-05-25T00:00:00+00:00'
---


# Synthesis — W2026-W22

## Themes

Two themes dominated this window:

1. **MCP supply chain.** Multiple advisories about MCP server packaging.
2. **Browser agent attacks.** A new family of indirect prompt injections.


## Entities

### CVEs

| Value | Context |
|---|---|
| CVE-2026-12345 | MCP server RCE via crafted manifest |
| CVE-2026-99999 | Browser agent sandbox escape |

### People

| Value | Context |
|---|---|
| Simon Willison | Published a taxonomy of browser-agent attacks |

### Repos

| Value | Context |
|---|---|
| anthropic/claude-code | Patched in 0.42.1 |
| modelcontextprotocol/servers | Affected by CVE-2026-12345 |

### Techniques

| Value | Context |
|---|---|
| indirect prompt injection | Re-popularized as the dominant agent attack vector |


## Followups

- **[audit]** [Claude Code sandbox escape PoC](https://example.com/c1) — check our own MCP server
- **[read-deep]** [MCP RCE via crafted manifest](https://example.com/s1) — full analysis + patch review
- **[track]** [Indirect prompt injection in browser agents](https://example.com/p1)

