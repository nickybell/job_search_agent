"""Steps 1→2 orchestration: search → idempotent insert → full-JD capture.

This is the body of the daily cron (and the ``jsa search`` CLI command). It runs
one search, then for each returned posting canonicalizes the URL and inserts it
idempotently; only for genuinely new rows does it fetch the ATS detail record to
capture the full job description. A failed capture never excludes a row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from . import db
from .ats import fetch_detail, resolve_ats_url
from .canonicalize import canonicalize_url
from .config import Config
from .naming import normalize_company, slugify_title
from .search import load_prompt, parse_search_output
from .search.claude_runner import run_claude_search
from .search.perplexity_runner import run_perplexity_search
from .search.prompt import EASTERN

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """Counts from one pipeline run, for the closing log line."""

    agent: str
    found: int = 0
    inserted: int = 0
    already_present: int = 0
    jd_captured: int = 0
    unresolved: int = 0
    fetch_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"[{self.agent}] found={self.found} inserted={self.inserted} "
            f"already_present={self.already_present} jd_captured={self.jd_captured} "
            f"unresolved={self.unresolved} fetch_failed={self.fetch_failed}"
        )


def select_agent_for_date(now: datetime | None = None) -> str:
    """Derive the A/B agent from day-of-year parity (even=claude, odd=perplexity)."""
    now = now or datetime.now(EASTERN)
    return "claude" if now.timetuple().tm_yday % 2 == 0 else "perplexity"


# The fixed weekly cadence for the daily cron: the look-back window (in hours)
# for each search day, or absent on days we don't search. Python's weekday():
# Mon=0 .. Sun=6. Monday reaches back across the weekend (72h); Wednesday and
# Friday each cover the 48h since the prior run. The windows deliberately
# overlap, so a skipped fire is recovered by the next run (and re-inserts no-op).
CRON_WINDOWS: dict[int, int] = {0: 72, 2: 48, 4: 48}  # Mon, Wed, Fri


def window_for_date(now: datetime | None = None) -> int | None:
    """Look-back window (hours) for the daily cron on `now`'s ET weekday, or None to skip."""
    now = now or datetime.now(EASTERN)
    return CRON_WINDOWS.get(now.weekday())


def _run_search(agent: str, prompt: str, config: Config) -> str:
    if agent == "claude":
        return run_claude_search(prompt)
    if agent == "perplexity":
        if not config.perplexity_api_key:
            raise RuntimeError("PERPLEXITY_API_KEY is not set (required for a B-day search).")
        return run_perplexity_search(prompt, config.perplexity_api_key)
    raise ValueError(f"unknown agent: {agent!r}")


def run_pipeline(
    agent: str,
    window_hours: int,
    config: Config,
    now: datetime | None = None,
) -> RunSummary:
    """Run one search-and-capture cycle and return a summary of what happened."""
    summary = RunSummary(agent=agent)
    now = now or datetime.now(EASTERN)
    run_date = now.date().isoformat()

    prompt = load_prompt(window_hours, now)
    log.info("running %s search over a %dh window", agent, window_hours)
    raw = _run_search(agent, prompt, config)
    output = parse_search_output(raw)
    summary.found = len(output.postings)
    log.info("search returned %d posting(s)", summary.found)

    client = db.connect(config)
    db.init_db(client)
    try:
        with httpx.Client(follow_redirects=True) as http:
            for rank, posting in enumerate(output.postings, start=1):
                _process_posting(
                    posting, agent, run_date, window_hours, rank, client, http, summary
                )
    finally:
        client.close()

    log.info("run complete: %s", summary)
    return summary


def _process_posting(
    posting, agent, run_date, window_hours, rank, client, http, summary: RunSummary
) -> None:
    canonical = canonicalize_url(posting.url)
    # Log the finding for A/B attribution BEFORE the idempotent insert, so a req
    # the insert no-ops (already present from the other agent) still credits
    # this agent. Both agents converge on the same canonical_url by design.
    db.record_finding(
        client,
        run_date=run_date,
        agent=agent,
        canonical_url=canonical,
        window_hours=window_hours,
        rank=rank,
    )
    new_id = db.insert_posting(
        client,
        db.NewPosting(
            company=posting.company,
            title=posting.title,
            url=posting.url,
            date_posted=posting.date_posted,
            canonical_url=canonical,
            normalized_company=normalize_company(posting.company),
            title_slug=slugify_title(posting.title),
            search_agent=agent,
        ),
    )
    if new_id is None:
        summary.already_present += 1
        return
    summary.inserted += 1

    # Full-JD capture for the genuinely new row. Any failure leaves NULL.
    resolved = resolve_ats_url(posting.url)
    if resolved is None:
        summary.unresolved += 1
        return
    try:
        detail = fetch_detail(resolved, http)
    except Exception as exc:  # content capture, not a liveness gate
        summary.fetch_failed += 1
        summary.errors.append(f"{posting.url}: {type(exc).__name__}: {exc}")
        log.warning("JD fetch failed for %s: %s", posting.url, exc)
        return
    jd = detail.jd_markdown or None
    db.update_jd_capture(
        client, new_id, jd_markdown=jd, location=detail.location, title=detail.title
    )
    if jd:
        summary.jd_captured += 1
