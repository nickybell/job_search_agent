"""The ``jsa`` command-line entry point.

Wraps the automated Steps 1–2 pipeline (``search``), the schema bootstrap
(``init-db``), the direct job-add path (``add``), the deterministic Step 3
review loop (``review``), the drift reconciliation (``refetch``), Step 4's
resume generation (``generate``, with ``packet`` as its deterministic head),
and the Step 5 tracker write (``track``). The search command is what the
Fly.io cron runs; it is also invocable by hand with a parameterized window and
agent.

The cloud/local split shows up here as which commands the Fly image ever runs:
only ``cron`` (and ``search``/``init-db`` by hand). Everything else is local —
wanting a terminal, the local disk (``base_resume.docx``, the packet
directories), and, for the Sheet-touching commands, the local Google OAuth
token held by the ``gws`` CLI.
"""

from __future__ import annotations

import logging
from datetime import datetime

import click

from . import db, generate, packet, prompting
from .config import load_config
from .manual import ManualAddError, add_posting
from .pipeline import run_pipeline, schedule_for_date
from .refetch import describe, run_refetch
from .refine import run_refine
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
    default="perplexity",
    show_default=True,
    help="Search agent to run.",
)
@click.option(
    "--window-hours",
    type=int,
    default=48,
    show_default=True,
    help="How far back to search, in hours.",
)
def search_command(agent: str, window_hours: int) -> None:
    """Run one search and idempotently capture new postings (Steps 1–2)."""
    _configure_logging()
    config = load_config()
    summary = run_pipeline(agent, window_hours, config)
    click.echo(str(summary))


@main.command("cron")
def cron_command() -> None:
    """Daily-cron entrypoint: self-gate to the weekly search schedule.

    Fly's scheduler only fires a fuzzy ``daily`` interval (no weekday selector,
    no per-run args), so this runs every morning and gates itself against
    ``CRON_SCHEDULE``: Perplexity on Mon (72h) / Wed (48h) / Fri (48h), plus a
    weekly Claude Deep Research sweep (168h) on Friday that runs first. It exits
    quietly on days with no scheduled search. One shared ``now`` per day so every
    search that day lands under the same ``run_date``.
    """
    _configure_logging()
    config = load_config()
    now = datetime.now(EASTERN)
    runs = schedule_for_date(now)
    if not runs:
        click.echo(f"{now.date().isoformat()} ({now:%A}): no search scheduled today.")
        return
    for agent, window_hours in runs:
        summary = run_pipeline(agent, window_hours, config, now=now)
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
    "include_all",
    is_flag=True,
    default=False,
    help="Re-read every stored posting, regardless of decision or tracker state.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what changed upstream without writing to the database.",
)
def refetch_command(posting_id: int | None, include_all: bool, dry_run: bool) -> None:
    """Re-read stored postings from their ATS and reconcile drift.

    Employers edit reqs in place, so a title or description captured at insert
    time can go stale under an unchanged URL. This re-applies the insert's rule:
    the ATS-canonical title wins and ``title_slug`` is re-derived from it. A
    failed fetch leaves the row untouched rather than blanking a good capture.

    Defaults to Apply postings either absent from the tracker Sheet or without
    a Date Applied there — the ones where a change could still change your next
    action. A corrected title is also propagated to the tracker row's Title
    cell when the job sits there unapplied (the Sheet is a projection of the
    database). The Sheet index read needs the local ``gws`` CLI; under ``--id``
    and ``--all`` it is best-effort and only enables that Title refresh.
    """
    _configure_logging()
    config = load_config()
    try:
        summary, results = run_refetch(
            config,
            posting_id=posting_id,
            include_all=include_all,
            dry_run=dry_run,
        )
    except TrackerError as exc:
        raise click.ClickException(
            f"could not read the tracker Sheet to scope the refetch: {exc}\n"
            "Use --all or --id to refetch without the Sheet lookup."
        ) from exc
    if summary.examined == 0:
        if summary.skipped_applied:
            click.echo(
                f"No postings to re-read ({summary.skipped_applied} Apply row(s) "
                "skipped — already applied per the tracker)."
            )
        else:
            click.echo("No postings to re-read.")
        return
    for result in results:
        click.echo(describe(result))
    if dry_run:
        click.echo(f"(dry run — nothing written) {summary}")
        return
    click.echo(str(summary))


@main.command("packet")
@click.option(
    "--id",
    "posting_id",
    type=int,
    default=None,
    help="Build one Apply posting's packet even if it is already in the tracker.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report the directories that would be created without touching the filesystem.",
)
def packet_command(posting_id: int | None, dry_run: bool) -> None:
    """Create and seed application-packet directories (Step 4's first two steps).

    For each ``Apply`` posting not yet in the tracker, creates
    ``~/Documents/Job Applications/{normalized_company} - {title_slug}`` with a
    fail-if-exists mkdir — an existing packet is skipped, never clobbered — and
    writes the captured job description inside as ``job_posting.md``. The
    resume tailoring itself is ``jsa generate``, which builds on these
    directories (and, unlike this command, re-enters an existing bare one).
    """
    _configure_logging()
    config = load_config()
    summary, results = packet.run_packet(config, posting_id=posting_id, dry_run=dry_run)
    if summary.eligible == 0:
        if posting_id is not None:
            raise click.ClickException(
                f"id {posting_id} is not an Apply posting (or does not exist) — "
                "packets are only built for jobs decided Apply."
            )
        click.echo("No Apply postings awaiting a packet directory.")
        return
    for result in results:
        click.echo(packet.describe(result))
    if dry_run:
        click.echo(f"(dry run — nothing created) {summary}")
        return
    click.echo(str(summary))


@main.command("generate")
@click.option(
    "--id",
    "posting_id",
    type=int,
    default=None,
    help="Generate one Apply posting's packet even if it is already in the tracker.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what each queued row would do without touching anything.",
)
def generate_command(posting_id: int | None, dry_run: bool) -> None:
    """Tailor a per-job resume and complete the application packet (Step 4).

    For each ``Apply`` posting not yet in the tracker (``--id`` waives the
    tracker condition, never the Apply one), ensures the packet directory and
    ``job_posting.md``, tailors the best-fit template from ``resume_templates/``
    in one pass (the model picks the template; a structured patch applied by
    python-docx; PDF via LibreOffice headless), writes
    ``resume_changelog.md``, and finishes by appending the row to the tracker
    Sheet (Step 5). A row with no captured JD is skipped, never tailored
    blind; a hand-filled ``job_posting.md`` in the packet directory is used as
    the JD instead.
    """
    _configure_logging()
    config = load_config()
    try:
        summary, results = generate.run_generate(config, posting_id=posting_id, dry_run=dry_run)
    except generate.GenerateError as exc:
        raise click.ClickException(str(exc)) from exc
    if summary.eligible == 0:
        if posting_id is not None:
            raise click.ClickException(
                f"id {posting_id} is not an Apply posting (or does not exist) — "
                "resumes are only generated for jobs decided Apply."
            )
        click.echo("No Apply postings awaiting a resume packet.")
        return
    for result in results:
        click.echo(generate.describe(result))
    if dry_run:
        click.echo(f"(dry run — nothing generated) {summary}")
        return
    click.echo(str(summary))
    if summary.failed or summary.track_failed:
        raise SystemExit(1)


@main.command("refine")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report the ground truth in scope without running the model or recording a run.",
)
def refine_command(dry_run: bool) -> None:
    """Refine the search prompt from new ground truth (the weekly PR loop).

    Considers only postings decided since the last recorded refinement run,
    drives the refiner agent over this checkout, and writes the PR body to
    ``.refine_pr_body.md``. The scheduled GitHub Actions workflow commits the
    resulting diff and opens the pull request; running this by hand is the
    manual escape hatch and produces the same working-tree diff for you to
    commit (or discard) yourself.
    """
    _configure_logging()
    config = load_config()
    summary = run_refine(config, dry_run=dry_run)
    if summary.considered == 0:
        click.echo("No new ground truth since the last refinement run.")
        return
    if dry_run:
        return
    if summary.changed_files:
        click.echo(f"Proposed changes to: {', '.join(summary.changed_files)}")
    else:
        click.echo("The refiner proposed no changes.")
    click.echo(f"PR body written to {summary.pr_body_path}")


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
