"""Synthesis agent (M5): a tool-using LLM loop over a window of classified items.

`tools` holds the agent's tool implementations (each returns a structured
`{ok, ...}` dict and never raises into the loop); `agent` holds the
provider-agnostic loop with its turn/budget bounds and degraded-finish path.
"""
