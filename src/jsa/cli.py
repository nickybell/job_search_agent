"""The ``jsa`` command-line entry point.

Wraps the automated Steps 1–2 pipeline (``search``), the schema bootstrap
(``init-db``), and the deterministic Step 3 review loop (``review``). The search
command is what the Fly.io cron runs; it is also invocable by hand with a
parameterized window and agent.
"""

from __future__ import annotations

import logging
from datetime import datetime

import click

from . import db
from .config import load_config
from .pipeline import run_pipeline, select_agent_for_date, window_for_date
from .review import run_review
from .search.prompt import EASTERN


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
def main() -> None:
    """Job Search Agent — daily search, capture, and fit review."""


@main.command("init-db")
def init_db_command() -> None:
    """Create the postings table if it does not exist."""
    config = load_config()
    client = db.connect(config)
    try:
        db.init_db(client)
    finally:
        client.close()
    click.echo("postings table is ready.")


@main.command("search")
@click.option(
    "--agent",
    type=click.Choice(["claude", "perplexity"]),
    default=None,
    help="Search agent. Defaults to the A/B choice for today (even day-of-year = claude).",
)
@click.option(
    "--window-hours",
    type=int,
    default=48,
    show_default=True,
    help="How far back to search, in hours.",
)
@click.option(
    "--both",
    is_flag=True,
    default=False,
    help="A/B trial: run BOTH agents over the same window (bypasses the "
    "day-of-year A/B pick). Temporary — for the bounded A/B trial.",
)
def search_command(agent: str | None, window_hours: int, both: bool) -> None:
    """Run the daily search and idempotently capture new postings (Steps 1–2)."""
    _configure_logging()
    config = load_config()
    if both:
        if agent:
            raise click.UsageError("--both runs both agents; do not also pass --agent.")
        # One shared `now` so both agents search the identical window and land
        # under the same search_findings.run_date — the controlled A/B condition.
        now = datetime.now(EASTERN)
        for a in ("claude", "perplexity"):
            summary = run_pipeline(a, window_hours, config, now=now)
            click.echo(str(summary))
        return
    agent = agent or select_agent_for_date()
    summary = run_pipeline(agent, window_hours, config)
    click.echo(str(summary))


@main.command("cron")
def cron_command() -> None:
    """Daily-cron entrypoint: self-gate to Mon/Wed/Fri with a weekday-sized window.

    Fly's scheduler only fires a fuzzy ``daily`` interval (no weekday selector,
    no per-run args), so this runs every morning and gates itself: it searches
    only on Mon (72h) / Wed (48h) / Fri (48h) and exits quietly on other days.
    It runs BOTH agents over the same window — the current A/B trial condition.
    """
    _configure_logging()
    config = load_config()
    now = datetime.now(EASTERN)
    window_hours = window_for_date(now)
    if window_hours is None:
        click.echo(f"{now.date().isoformat()} ({now:%A}): no search scheduled today.")
        return
    for a in ("claude", "perplexity"):
        summary = run_pipeline(a, window_hours, config, now=now)
        click.echo(str(summary))


@main.command("review")
def review_command() -> None:
    """Work through the backlog of postings awaiting a fit decision (Step 3)."""
    config = load_config()
    run_review(config)


@main.command("ab-report")
def ab_report_command() -> None:
    """Summarize the A/B search trial: coverage, overlap, and Apply precision."""
    config = load_config()
    client = db.connect(config)
    try:
        report = db.ab_report(client)
    finally:
        client.close()

    click.echo("Coverage (distinct reqs found):")
    for agent, n in report["coverage"]:
        click.echo(f"  {agent:<11} {n}")

    both, claude_only, perplexity_only = report["overlap"] or (0, 0, 0)
    click.echo("Overlap:")
    click.echo(f"  both            {both or 0}")
    click.echo(f"  claude only     {claude_only or 0}")
    click.echo(f"  perplexity only {perplexity_only or 0}")

    click.echo("Apply precision (applies / decided):")
    for agent, applies, decided in report["precision"]:
        rate = f"{applies / decided:.0%}" if decided else "n/a"
        click.echo(f"  {agent:<11} {applies}/{decided} ({rate})")


if __name__ == "__main__":
    main()
