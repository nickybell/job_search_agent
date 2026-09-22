"""Shared scaffolding for the headless Claude Agent SDK runs.

Three local commands drive an agent the same way — ``jsa generate``,
``jsa bullets``, ``jsa refine`` — each with its own model, effort, tools, and
error class. What they share lives here: locating a prompt template beside the
repo, and the message loop that collects the agent's final text and surfaces
an error result as a typed exception.
"""

from __future__ import annotations

import logging
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

log = logging.getLogger(__name__)

# src/jsa/agent.py -> the repo root is two parents up from src/jsa.
REPO_ROOT = Path(__file__).resolve().parents[2]


def prompt_path(filename: str) -> Path:
    """Locate a prompt template: the working directory first, then the repo root.

    The working-directory check is what lets the container (which copies the
    search prompt beside ``src/``) and a dev checkout resolve the same name.
    """
    candidate = Path.cwd() / filename
    return candidate if candidate.is_file() else REPO_ROOT / filename


async def collect_final_text(
    prompt: str, options: ClaudeAgentOptions, error_cls: type[Exception], label: str
) -> str:
    """Drive one headless run and return the agent's final message.

    An API error that outlives the CLI's own retries (HTTP 429/500/529) still
    arrives as a ``subtype="success"`` result with ``is_error`` set, so it is
    detected explicitly and raised as ``error_cls`` with the real cause — the
    HTTP status and the CLI's error text — instead of the SDK's opaque
    "returned an error result: success". Raising before the caller records
    anything is what keeps an errored run reconsidered next time. Falls back
    to the last assistant message when the result carries no text.
    """
    final_text = ""
    assistant_text: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    assistant_text.append(block.text)
        elif isinstance(message, ResultMessage):
            if message.is_error:
                status = message.api_error_status
                detail = (message.result or "").strip() or message.subtype
                where = f" (API error HTTP {status})" if status else ""
                raise error_cls(f"{label} failed{where}: {detail}")
            final_text = message.result or ""
            if message.total_cost_usd is not None:
                log.info("%s finished ($%.4f)", label, message.total_cost_usd)
    return final_text or (assistant_text[-1] if assistant_text else "")


def run_agent(
    prompt: str, options: ClaudeAgentOptions, error_cls: type[Exception], label: str
) -> str:
    """Synchronous wrapper around ``collect_final_text`` for the CLI commands."""
    return anyio.run(collect_final_text, prompt, options, error_cls, label)
