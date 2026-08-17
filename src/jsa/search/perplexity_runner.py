"""Perplexity **Agent API** deep research (``xhigh`` preset).

The Agent API (``POST /v1/agent``) runs multi-step *agentic* research — chaining
``web_search`` calls with reasoning and aggregating across sources — which is the
deep-research counterpart to the Claude Deep Research runner. This replaces
the former Sonar ``sonar-pro`` chat-completions call, which was a single-pass
search with no reasoning phase (fast, but not comparable in depth).

The ``xhigh`` preset is Perplexity's highest research tier ("state-of-the-art
deep research"); it runs for minutes and consults many sources, so the runner
streams the typed SSE events and surfaces progress plus the final USD cost.
Output is still constrained to the ``postings`` JSON contract from prd.md via
``response_format`` json_schema (the same schema both agents honor).

Key shape differences from the old Sonar call (verified live before shipping):
- endpoint ``/v1/agent``; body uses ``preset`` + ``input`` (not ``model`` + ``messages``)
- web search is an explicit ``tools: [{type: web_search}]`` entry
- streamed SSE events are typed: text arrives as ``response.output_text.delta``
  (``.delta``) with an authoritative ``response.output_text.done`` (``.text``),
  and ``response.completed`` carries ``response.usage.cost``. The *progress*
  event vocabulary is preset-dependent — see ``_StreamState``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

# Emit a heartbeat at most this often while the response streams in, so a long
# xhigh run shows evidence of progress rather than sitting silent for minutes.
_HEARTBEAT_SECONDS = 5.0

_API_URL = "https://api.perplexity.ai/v1/agent"

# Perplexity's highest research tier. "high" is the willing-to-consider fallback
# (cheaper/faster, still a real multi-step research preset) — a one-line change.
PRESET = "xhigh"

# xhigh deep research runs for minutes; give it plenty of headroom. Streaming
# keeps the connection alive, so this is a ceiling, not an expected duration.
_TIMEOUT = httpx.Timeout(1800.0, connect=30.0)

# The postings JSON contract, enforced at the API layer in addition to the
# in-prompt contract both agents read. Mirrors the schema in prd.md.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "postings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "date_posted": {"type": "string"},
                },
                "required": ["company", "title", "url"],
            },
        }
    },
    "required": ["postings"],
}


@dataclass
class _StreamState:
    """Folds the Agent API's typed SSE events into text + progress counters.

    Pure and I/O-free (so a pytest can drive it off recorded events). The Agent
    API's *progress* vocabulary is preset-dependent, verified live: the reasoning
    tiers (fast/medium/high) narrate web work as ``response.reasoning.*`` events
    (search_queries / search_results / fetch_url_queries), whereas ``xhigh``
    loads a skill and does its searching inside sandboxed code, surfacing only
    ``response.sandbox.results`` steps and no reasoning.search events. Counting
    both keeps the heartbeat honest across presets instead of reporting a
    misleading zero for xhigh (which is exactly what the first cut did).
    """

    chunks: list[str] = field(default_factory=list)
    final_text: str | None = None
    searches: int = 0
    sources: int = 0
    fetches: int = 0
    sandbox_steps: int = 0
    cost: dict | None = None

    def update(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "response.output_text.delta":
            self.chunks.append(event.get("delta", ""))
        elif etype == "response.output_text.done":
            # Authoritative final text — preferred over reassembled deltas.
            self.final_text = event.get("text")
        elif etype == "response.reasoning.search_queries":
            self.searches += len(event.get("queries") or [])
        elif etype == "response.reasoning.search_results":
            self.sources += len(event.get("results") or [])
        elif etype == "response.reasoning.fetch_url_queries":
            self.fetches += len(event.get("urls") or [])
        elif etype == "response.sandbox.results":
            self.sandbox_steps += 1
        elif etype == "response.completed":
            usage = event.get("response", {}).get("usage") or {}
            self.cost = usage.get("cost")

    @property
    def text(self) -> str:
        return self.final_text if self.final_text is not None else "".join(self.chunks)

    @property
    def assembled_chars(self) -> int:
        return sum(len(c) for c in self.chunks)

    def activity(self) -> str:
        """Human-readable summary of the tool work seen so far (nonzero only)."""
        bits = []
        if self.sandbox_steps:
            bits.append(f"{self.sandbox_steps} sandbox steps")
        if self.searches:
            bits.append(f"{self.searches} searches")
        if self.sources:
            bits.append(f"{self.sources} sources")
        if self.fetches:
            bits.append(f"{self.fetches} url fetches")
        return ", ".join(bits) or "no tool activity yet"


def run_perplexity_search(prompt: str, api_key: str) -> str:
    """Run the Perplexity Agent API deep research; return the raw JSON output."""
    payload = {
        "preset": PRESET,
        "input": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search"}],
        "stream": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "postings", "schema": _RESPONSE_SCHEMA},
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    state = _StreamState()
    last_heartbeat = time.monotonic()

    log.info("Perplexity Agent API deep research starting (preset=%s, streaming)", PRESET)
    with httpx.stream(
        "POST", _API_URL, json=payload, headers=headers, timeout=_TIMEOUT
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue

            state.update(event)

            now = time.monotonic()
            if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                log.info("  …working (%s, %d chars)", state.activity(), state.assembled_chars)
                last_heartbeat = now

    raw = state.text
    total = (state.cost or {}).get("total_cost")
    log.info(
        "Perplexity Agent API finished: %d chars, %s%s",
        len(raw),
        state.activity(),
        f", ${total:.4f}" if isinstance(total, (int, float)) else "",
    )
    return raw
