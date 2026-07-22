"""A-day search: Claude Deep Research via the Claude Agent SDK.

Run headless on the cron with ``claude-opus-4-8`` at high effort — long-horizon
agentic web research with hard liveness gates rewards Opus-tier
instruction-following (see the rationale in ``prd.md``). The runner drives the
SDK's web tools and returns the model's final text, which ``parse.py`` then
validates.
"""

from __future__ import annotations

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

MODEL = "claude-opus-4-8"
# Web research over many sources with per-posting ATS list-endpoint checks needs
# generous turn headroom.
_MAX_TURNS = 120


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
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    assistant_text.append(block.text)
        elif isinstance(message, ResultMessage):
            final_text = getattr(message, "result", "") or ""

    # Prefer the SDK's final result string; fall back to concatenated assistant
    # text if the result message carried none.
    return final_text or "\n".join(assistant_text)


def run_claude_search(prompt: str) -> str:
    """Run the A-day Claude Deep Research search and return its raw output."""
    return anyio.run(_run, prompt)
