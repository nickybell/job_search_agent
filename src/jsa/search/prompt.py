"""Load the search prompt template and interpolate the ``{{SEARCH_WINDOW}}`` slot.

The prompt is a template shared by both search agents; the cron (or a CLI
invocation) fills the window before the prompt is sent. The window is rendered
as an explicit America/New_York date range so the model judges recency against
concrete dates rather than a relative phrase.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
_PLACEHOLDER = "{{SEARCH_WINDOW}}"
_PROMPT_FILENAME = "deep_research_prompt.md"


def _default_prompt_path() -> Path:
    """Locate ``deep_research_prompt.md`` (repo root in dev and in the container)."""
    cwd_candidate = Path.cwd() / _PROMPT_FILENAME
    if cwd_candidate.is_file():
        return cwd_candidate
    # src/jsa/search/prompt.py -> repo root is three parents up from src/jsa.
    return Path(__file__).resolve().parents[3] / _PROMPT_FILENAME


def format_search_window(window_hours: int, now: datetime | None = None) -> str:
    """Render the human-readable window phrase interpolated into the prompt."""
    now = now or datetime.now(EASTERN)
    start = now - timedelta(hours=window_hours)
    fmt = "%Y-%m-%d %H:%M %Z"
    return f"the last {window_hours} hours (from {start.strftime(fmt)} through {now.strftime(fmt)})"


def load_prompt(
    window_hours: int = 48,
    now: datetime | None = None,
    path: Path | None = None,
) -> str:
    """Read the template and substitute the search window."""
    path = path or _default_prompt_path()
    text = path.read_text(encoding="utf-8")
    return text.replace(_PLACEHOLDER, format_search_window(window_hours, now))
