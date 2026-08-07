"""A-day search: Claude Deep Research via the Claude Agent SDK.

Run headless on the cron with ``claude-opus-4-8`` at high effort — long-horizon
agentic web research with hard liveness gates rewards Opus-tier
instruction-following (see the rationale in ``prd.md``). The runner drives the
SDK's web tools and returns the model's final text, which ``parse.py`` then
validates.

Because the run is otherwise a multi-minute black box (no permission prompts —
it runs ``bypassPermissions``), the message loop emits a live trace to the
logger: every tool call (each ``WebSearch`` query / ``WebFetch`` URL it checks
against the ATS list endpoints), the model's narration and thinking, any tool
error, and an end-of-run line with turn count, wall time, and dollar cost. That
trace is the evidence the run is progressing (and the first place misdirection
or errors surface), locally and in ``fly logs`` alike.
"""

from __future__ import annotations

import logging

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    UserMessage,
    query,
)

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
# Web research over many sources with per-posting ATS list-endpoint checks needs
# generous turn headroom.
_MAX_TURNS = 120

# Keep any single trace line to one readable line.
_TRACE_LIMIT = 280


def _truncate(text: str, limit: int = _TRACE_LIMIT) -> str:
    """Collapse whitespace and clip to a single readable line."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit].rstrip() + "…"


def _format_tool_input(tool_input: object) -> str:
    """Summarize a tool call's input to its most informative field.

    WebSearch carries a ``query``; WebFetch a ``url`` — surface those directly
    and fall back to a compact repr for anything else.
    """
    if isinstance(tool_input, dict):
        for key in ("query", "url", "pattern", "prompt"):
            value = tool_input.get(key)
            if value:
                return _truncate(str(value), 200)
        return _truncate(str(tool_input), 200)
    return _truncate(str(tool_input), 200)


def _log_block(block: object) -> None:
    """Emit one trace line for a content block, if it's worth surfacing."""
    # Tool call — client tools (ToolUseBlock) and server tools
    # (ServerToolUseBlock) both expose ``name`` + ``input``; duck-type so we
    # catch web_search/web_fetch regardless of which class the SDK used.
    name = getattr(block, "name", None)
    if name is not None and hasattr(block, "input"):
        log.info("  → %s: %s", name, _format_tool_input(block.input))
        return
    if isinstance(block, TextBlock):
        text = block.text.strip()
        if text:
            log.info("  \U0001f4ac %s", _truncate(text))
        return
    if isinstance(block, ThinkingBlock):
        thinking = block.thinking.strip()
        if thinking:
            log.info("  \U0001f4ad %s", _truncate(thinking))
        return
    # Tool results (ToolResultBlock / ServerToolResultBlock) — only surface
    # failures; a successful fetch is implied by the next step.
    if getattr(block, "is_error", False):
        content = getattr(block, "content", "")
        log.warning("  ⚠ tool error: %s", _truncate(str(content), 200))


def _log_result(message: ResultMessage) -> None:
    """Emit the closing line: turns, wall time, and dollar cost."""
    parts: list[str] = []
    turns = getattr(message, "num_turns", None)
    if turns is not None:
        parts.append(f"{turns} turns")
    duration_ms = getattr(message, "duration_ms", None)
    if duration_ms is not None:
        parts.append(f"{duration_ms / 1000:.1f}s")
    cost = getattr(message, "total_cost_usd", None)
    if cost is not None:
        parts.append(f"${cost:.4f}")
    if getattr(message, "is_error", False):
        parts.append("ERROR")
    log.info("Claude research finished: %s", ", ".join(parts) or "(no metrics reported)")


async def _run(prompt: str) -> str:
    options = ClaudeAgentOptions(
        model=MODEL,
        # xhigh is Opus's default effort in the harness; pin the model and let
        # the web tools run without interactive permission prompts.
        allowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=_MAX_TURNS,
    )

    final_text = ""
    assistant_text: list[str] = []
    log.info("Claude Deep Research starting (model=%s, max_turns=%d)", MODEL, _MAX_TURNS)
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                _log_block(block)
                if isinstance(block, TextBlock):
                    assistant_text.append(block.text)
        elif isinstance(message, UserMessage):
            # Tool results come back on UserMessage.content (when it's a list of
            # blocks); scan them so tool errors surface in the trace.
            content = message.content
            if isinstance(content, list):
                for block in content:
                    _log_block(block)
        elif isinstance(message, ResultMessage):
            final_text = getattr(message, "result", "") or ""
            _log_result(message)

    # Prefer the SDK's final result string; fall back to concatenated assistant
    # text if the result message carried none.
    return final_text or "\n".join(assistant_text)


def run_claude_search(prompt: str) -> str:
    """Run the A-day Claude Deep Research search and return its raw output."""
    return anyio.run(_run, prompt)
