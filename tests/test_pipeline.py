"""The weekly cron cadence (``schedule_for_date`` / ``CRON_SCHEDULE``).

Pure logic — a datetime in, the ordered (agent, window) searches out — so it is
tested I/O-free, no DB or network. Dates below are real 2026 weekdays.
"""

from __future__ import annotations

from datetime import datetime

from jsa.pipeline import schedule_for_date
from jsa.search.prompt import EASTERN


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=EASTERN)


def test_monday_is_perplexity_only_over_the_weekend_window():
    assert schedule_for_date(_at("2026-08-17T09:00")) == (("perplexity", 72),)


def test_wednesday_is_perplexity_only_over_a_48h_window():
    assert schedule_for_date(_at("2026-08-19T09:00")) == (("perplexity", 48),)


def test_friday_runs_claude_168h_before_perplexity_48h():
    # Ordering is the contract: Claude's weekly sweep runs first, then the
    # incremental Perplexity pass.
    assert schedule_for_date(_at("2026-08-21T09:00")) == (
        ("claude", 168),
        ("perplexity", 48),
    )


def test_non_search_days_return_empty():
    for iso in ("2026-08-18T09:00", "2026-08-20T09:00", "2026-08-22T09:00", "2026-08-23T09:00"):
        assert schedule_for_date(_at(iso)) == ()
