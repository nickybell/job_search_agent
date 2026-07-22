"""Step 1: the daily search runners.

The search alternates by day — Claude Deep Research on even days, Perplexity Pro
Search on odd days — but both read the same ``deep_research_prompt.md`` template
and must return the same ``postings`` JSON contract, which ``parse.py`` validates
through the shared pydantic models.
"""

from .parse import parse_search_output
from .prompt import format_search_window, load_prompt

__all__ = ["parse_search_output", "format_search_window", "load_prompt"]
