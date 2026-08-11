"""The ``jsa`` command-line entry point.

Wraps the automated Steps 1–2 pipeline (``search``), the schema bootstrap
(``init-db``), the direct job-add path (``add``), the deterministic Step 3
review loop (``review``), and the Step 5 tracker write (``track``). The search
command is what the Fly.io cron runs; it is also invocable by hand with a
parameterized window and agent.

The cloud/local split shows up here as which commands the Fly image ever runs:
only ``cron`` (and ``search``/``init-db`` by hand). ``add``, ``review``, and
``track`` are local — they want a terminal and, for ``track``, the local Google
OAuth token held by the ``gws`` CLI.
"""

from __future__ import annotations

import logging
from datetime import datetime

import click

from . import db, prompting
from .config import load_config
from .manual import ManualAddError, add_posting
from .pipeline import run_pipeline, select_agent_for_date, window_for_date
from .refetch import describe, run_refetch
from .review import run_review
from .search.prompt import EASTERN
from .tracker import TrackerError, run_tracker


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


@main.command("add")
@click.argument("url")
@click.option("--company", default=None, help="Company name. Default: derived from the ATS board.")
@click.option("--title", default=None, help="Job title. Default: the ATS record's canonical title.")
@click.option("--date-posted", default=None, help="Posting date, verbatim. Optional.")
@click.option(
    "--no-input",
    is_flag=True,
    default=False,
    help="Never prompt: accept the derived company/title, or fail if they cannot be derived.",
)
def add_command(
    url: str,
    company: str | None,
    title: str | None,
    date_posted: str | None,
    no_input: bool,
) -> None:
    """Add a job posting by URL, reusing Step 2's insert and full-JD capture.

    Supplying the URL is itself the ``Apply`` decision, so the row skips the
    Step 3 review backlog and goes straight into the Step 5 tracker queue.
    Re-adding a known URL does not duplicate it — the same canonical-URL UNIQUE
    constraint guards this path — but it does promote that row to ``Apply``.
    """
    _configure_logging()
    config = load_config()
    try:
        result = add_posting(
            url,
            config,
            company=company,
            title=title,
            date_posted=date_posted,
            interactive=not no_input,
        )
    except ManualAddError as exc:
        raise click.ClickException(str(exc)) from exc
    except prompting.Quit as exc:
        # Ctrl-C/Ctrl-D at a prompt, or no terminal to prompt on at all. Nothing
        # has been written yet -- the insert happens after both prompts.
        raise click.ClickException(
            "aborted before anything was written. Pass --company/--title (and "
            "--no-input) to add a posting without prompting."
        ) from exc

    if result.status == "already_present":
        click.echo(
            f"Already in the database as id {result.posting_id}: {result.company} — {result.title}"
        )
        if result.decision_changed:
            was = result.previous_decision or "undecided"
            click.echo(f"  decision {was} → Apply; queued for `jsa track`.")
        else:
            click.echo("  already Apply; no change.")
        return
    click.echo(f"Added id {result.posting_id}: {result.company} — {result.title}")
    click.echo("  decision: Apply (review skipped); queued for `jsa track`.")
    if result.jd_captured:
        click.echo(f"  full JD captured from {result.platform}.")
    elif result.fetch_error:
        click.echo(f"  JD capture failed ({result.fetch_error}); the row keeps a NULL jd_markdown.")
    else:
        click.echo(
            "  no job description could be captured (no supported ATS and no "
            "JSON-LD on the page); the Step 4 packet will have no job_posting.md."
        )


@main.command("review")
def review_command() -> None:
    """Work through the backlog of postings awaiting a fit decision (Step 3)."""
    config = load_config()
    run_review(config)


@main.command("refetch")
@click.option("--id", "posting_id", type=int, default=None, help="Re-read only this posting id.")
@click.option(
    "--all",
    "include_tracked",
    is_flag=True,
    default=False,
    help="Include postings already written to the tracker (reports drift it cannot fix there).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what changed upstream without writing to the database.",
)
def refetch_command(posting_id: int | None, include_tracked: bool, dry_run: bool) -> None:
    """Re-read stored postings from their ATS and reconcile drift.

    Employers edit reqs in place, so a title or description captured at insert
    time can go stale under an unchanged URL. This re-applies the insert's rule:
    the ATS-canonical title wins and ``title_slug`` is re-derived from it. A
    failed fetch leaves the row untouched rather than blanking a good capture.

    Defaults to postings not yet in the tracker — the ones where a correction
    still propagates everywhere it matters.
    """
    _configure_logging()
    config = load_config()
    summary, results = run_refetch(
        config,
        posting_id=posting_id,
        include_tracked=include_tracked,
        dry_run=dry_run,
    )
    if summary.examined == 0:
        click.echo("No postings to re-read.")
        return
    for result in results:
        click.echo(describe(result))
    if dry_run:
        click.echo(f"(dry run — nothing written) {summary}")
        return
    click.echo(str(summary))


@main.command("track")
@click.option(
    "--id",
    "posting_id",
    type=int,
    default=None,
    help="Elevate only this posting id (it must still be Apply and untracked).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the rows that would be appended without calling gws.",
)
def track_command(posting_id: int | None, dry_run: bool) -> None:
    """Append Apply-decided postings to the Google Sheet tracker (Step 5).

    Eligibility is ``decision = 'Apply' AND added_to_tracker = 0``, and the flag
    is set only after the Sheets API confirms the append — so re-running never
    double-appends and a failed row stays in the backlog.
    """
    _configure_logging()
    config = load_config()
    try:
        summary = run_tracker(config, posting_id=posting_id, dry_run=dry_run)
    except TrackerError as exc:
        raise click.ClickException(str(exc)) from exc
    if summary.eligible == 0:
        click.echo("No Apply postings awaiting a tracker row.")
        return
    if dry_run:
        return
    click.echo(str(summary))
    if summary.failed:
        raise SystemExit(1)


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
