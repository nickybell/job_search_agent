"""Step 1: the daily search runners.

Two runners — Claude Deep Research and Perplexity Agent API deep research — read
the same ``deep_research_prompt.md`` template and must return the same
``postings`` JSON contract, which ``parse.py`` validates through the shared
pydantic models. The weekly cron cadence (which runner runs when) lives in
``pipeline.CRON_SCHEDULE``.
"""

from .parse import parse_search_output
from .prompt import format_search_window, load_prompt

__all__ = ["parse_search_output", "format_search_window", "load_prompt"]
