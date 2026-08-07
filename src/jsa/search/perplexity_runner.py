"""B-day search: Perplexity Pro Search over the streaming chat completions API.

Pro Search (``search_type: "pro"``) supplies the model-orchestrated multi-step
``web_search`` + ``fetch_url_content`` the liveness checks depend on, and it
**requires ``stream: true``** — a non-streaming request silently falls back to
standard single-step search — so the runner streams and reassembles the response.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

log = logging.getLogger(__name__)

# Emit a heartbeat at most this often while the response streams in, so a long
# Pro Search shows evidence of progress rather than sitting silent.
_HEARTBEAT_SECONDS = 5.0

_API_URL = "https://api.perplexity.ai/chat/completions"
MODEL = "sonar-pro"

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


def run_perplexity_search(prompt: str, api_key: str) -> str:
    """Run the B-day Perplexity Pro Search and return the reassembled raw output."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "web_search_options": {"search_type": "pro"},
        "stream": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": _RESPONSE_SCHEMA},
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    chunks: list[str] = []
    search_results_seen = 0
    last_heartbeat = time.monotonic()
    log.info("Perplexity Pro Search starting (model=%s, streaming)", MODEL)
    with httpx.stream(
        "POST", _API_URL, json=payload, headers=headers, timeout=httpx.Timeout(300.0)
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            delta = event.get("choices", [{}])[0].get("delta", {}).get("content")
            if delta:
                chunks.append(delta)

            # Pro Search reports the pages it consulted; surface the running
            # count as it grows so we can see the multi-step search working.
            results = event.get("search_results") or event.get("citations")
            if results and len(results) > search_results_seen:
                search_results_seen = len(results)
                log.info("  \U0001f50e consulted %d source(s) so far", search_results_seen)

            now = time.monotonic()
            if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                log.info("  …streaming (%d chars assembled)", sum(len(c) for c in chunks))
                last_heartbeat = now

    raw = "".join(chunks)
    log.info(
        "Perplexity Pro Search finished: %d chars, %d source(s) consulted",
        len(raw),
        search_results_seen,
    )
    return raw
