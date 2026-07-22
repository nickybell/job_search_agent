"""The ``jsa`` command-line entry point.

Wraps the automated Steps 1–2 pipeline (``search``), the schema bootstrap
(``init-db``), and the deterministic Step 3 review loop (``review``). The search
command is what the Fly.io cron runs; it is also invocable by hand with a
parameterized window and agent.
"""

from __future__ import annotations

import logging

import click

from . import db
from .config import load_config
from .pipeline import run_pipeline, select_agent_for_date
from .review import run_review


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
def search_command(agent: str | None, window_hours: int) -> None:
    """Run the daily search and idempotently capture new postings (Steps 1–2)."""
    _configure_logging()
    config = load_config()
    agent = agent or select_agent_for_date()
    summary = run_pipeline(agent, window_hours, config)
    click.echo(str(summary))


@main.command("review")
def review_command() -> None:
    """Work through the backlog of postings awaiting a fit decision (Step 3)."""
    config = load_config()
    run_review(config)


if __name__ == "__main__":
    main()
