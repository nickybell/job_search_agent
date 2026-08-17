"""Re-read a stored posting's ATS record and reconcile the row against it.

Step 2 captures the job description once, at insert time, and deliberately
never looks again -- the PRD's reasoning holds, since Step 4 may run days later
and the posting must be captured while it is alive. But employers edit reqs in
place: on 2026-08-08 a Stepful posting read "Fractional GTM Enablement Lead"
and by 2026-08-11 the same UUID, same URL and same ``publishedAt`` read
"Fractional Sales Enablement Lead" (confirmed against an Internet Archive
snapshot). Capture-once plus edit-in-place means a row can silently drift from
its source with no path back.

This is that path: re-resolve the ATS record and apply the same rule the insert
uses -- the ATS-canonical title wins, and ``title_slug`` is re-derived from it
so the Step 4 packet naming stays consistent.

One constraint is inherited from the pipeline: a failed fetch leaves the row
completely untouched rather than overwriting a good description with nothing
-- the same degrade-don't-destroy stance, applied in the direction that
matters here.

Scope (decided 2026-08-16): only postings where drift could still change what
Nicky does next -- ``decision = 'Apply'`` rows that are *either* absent from
the tracker Sheet *or* present with a blank Date Applied. That is an OR: being
in the tracker does not exempt a job that has not actually been applied to.
Once an application is out the door the row is skipped and its Sheet copy
becomes a frozen record of what was submitted. If the Sheet index read fails
in this default scope, the run fails loudly rather than guessing.

The Sheet relationship follows the 2026-08-16 reframing (see ``tracker``): the
database is the source of truth for posting data; the tracker is its
human-readable projection plus the user's workspace. So when a title
correction lands on a row that sits in the Sheet unapplied, the projection
follows -- ``tracker.update_title`` refreshes that one cell, DB first, Sheet
second, and a failed Sheet write degrades to a flagged hand-fix rather than
blocking the reconciliation. Under ``--id``/``--all`` the rows are selected
unconditionally, so the index read is best-effort there: it only enables the
refresh, and its failure downgrades to the flag.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import db
from .ats import fetch_detail, resolve_ats_url
from .config import Config
from .naming import slugify_title
from .packet import packet_dir_name, packets_dir, write_job_posting
from .tracker import TrackedRow, TrackerError, read_tracker_index, spreadsheet_id, update_title

log = logging.getLogger(__name__)


@dataclass
class RowResult:
    """What re-reading one posting's ATS record turned up."""

    posting_id: int
    url: str
    already_tracked: bool = False
    title_before: str | None = None
    title_after: str | None = None
    changed_fields: list[str] = field(default_factory=list)
    unresolved: bool = False
    error: str | None = None
    sheet_row: int | None = None  # Sheet row to refresh: tracked & unapplied only
    sheet_updated: bool = False
    sheet_error: str | None = None
    packet_old: Path | None = None  # stale packet dir on disk, when one exists
    packet_new: Path | None = None  # its replacement (same path if the slug held)
    packet_rebuilt: bool = False
    packet_error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.changed_fields)

    @property
    def sheet_refresh_pending(self) -> bool:
        """A title change mapped to an unapplied Sheet row, not (yet) written."""
        return (
            "title" in self.changed_fields
            and self.sheet_row is not None
            and not self.sheet_updated
            and self.sheet_error is None
        )


@dataclass
class RefetchSummary:
    """Counts from one reconciliation pass."""

    examined: int = 0
    changed: int = 0
    unchanged: int = 0
    unresolved: int = 0
    failed: int = 0
    tracker_updated: int = 0
    stale_in_tracker: int = 0
    skipped_applied: int = 0
    packets_rebuilt: int = 0

    def __str__(self) -> str:
        return (
            f"examined={self.examined} changed={self.changed} unchanged={self.unchanged} "
            f"unresolved={self.unresolved} failed={self.failed} "
            f"tracker_updated={self.tracker_updated} "
            f"stale_in_tracker={self.stale_in_tracker} skipped_applied={self.skipped_applied} "
            f"packets_rebuilt={self.packets_rebuilt}"
        )


def diff_row(row: tuple, detail) -> RowResult:
    """Compare a stored row against a freshly fetched ATS record. Pure.

    ``title`` and the description are compared exactly: an employer editing a
    req in place is precisely the drift this exists to catch, and normalizing
    the comparison would hide it. Each field only registers as changed when the
    ATS actually supplied a value, so a sparse record cannot read as "changed
    to nothing".
    """
    posting_id, url, title, location, tracked, _normalized_company, _title_slug, jd_before = row
    result = RowResult(
        posting_id=int(posting_id),
        url=url,
        already_tracked=bool(tracked),
        title_before=title,
        title_after=getattr(detail, "title", None),
    )
    if result.title_after and result.title_after != title:
        result.changed_fields.append("title")
    new_jd = getattr(detail, "jd_markdown", None)
    if new_jd and new_jd != jd_before:
        result.changed_fields.append("description")
    new_location = getattr(detail, "location", None)
    if new_location and new_location != location:
        result.changed_fields.append("location")
    return result


def run_refetch(
    config: Config,
    *,
    posting_id: int | None = None,
    include_all: bool = False,
    dry_run: bool = False,
) -> tuple[RefetchSummary, list[RowResult]]:
    """Re-read each selected posting's ATS record and reconcile the stored row.

    The default scope is Apply rows either absent from the tracker Sheet or
    present without a Date Applied — raising ``tracker.TrackerError`` if that
    index read fails, since silently refetching everything (or nothing) would
    defeat the scoping. ``posting_id`` and ``include_all`` select rows
    unconditionally; the index is then read best-effort, purely to refresh the
    Sheet Title of a corrected row.
    """
    summary = RefetchSummary()
    results: list[RowResult] = []

    client = db.connect(config)
    db.init_db(client)
    try:
        rows = db.rows_for_refetch(client, posting_id, include_all=include_all)
        index: dict[int, TrackedRow] = {}
        if rows:
            if posting_id is None and not include_all:
                index = read_tracker_index(spreadsheet_id())
                kept = [r for r in rows if not _already_applied(index.get(int(r[0])))]
                summary.skipped_applied = len(rows) - len(kept)
                rows = kept
            else:
                try:
                    index = read_tracker_index(spreadsheet_id())
                except TrackerError as exc:
                    log.warning(
                        "tracker index unavailable; Sheet titles will not be refreshed: %s",
                        exc,
                    )
        summary.examined = len(rows)
        if not rows:
            return summary, results
        with httpx.Client(follow_redirects=True) as http:
            for row in rows:
                tracked = index.get(int(row[0]))
                sheet_row = tracked.row_number if tracked and not tracked.date_applied else None
                result = _refetch_one(client, row, http, dry_run=dry_run, sheet_row=sheet_row)
                results.append(result)
                if result.error:
                    summary.failed += 1
                elif result.unresolved:
                    summary.unresolved += 1
                elif result.changed:
                    summary.changed += 1
                    if result.sheet_updated or (dry_run and result.sheet_refresh_pending):
                        summary.tracker_updated += 1
                    elif "title" in result.changed_fields and result.already_tracked:
                        summary.stale_in_tracker += 1
                    if result.packet_rebuilt or (
                        dry_run and result.packet_new is not None and not result.packet_error
                    ):
                        summary.packets_rebuilt += 1
                else:
                    summary.unchanged += 1
    finally:
        client.close()
    return summary, results


def _already_applied(tracked: TrackedRow | None) -> bool:
    """True only for rows BOTH present in the Sheet AND bearing a Date Applied.

    The scope spec is an OR — absent from the Sheet, *or* present with a blank
    Date Applied, keeps a row eligible — so the skip is its negation: presence
    and a date, together.
    """
    return tracked is not None and bool(tracked.date_applied)


def _refetch_one(
    client, row: tuple, http: httpx.Client, *, dry_run: bool, sheet_row: int | None = None
) -> RowResult:
    posting_id, url, title, _location, tracked, normalized_company, title_slug, jd_before = row
    resolved = resolve_ats_url(url)
    if resolved is None:
        # No supported ATS to re-read (a hand-added Workday URL, say). Nothing
        # to reconcile against, which is not a failure.
        return RowResult(
            posting_id=int(posting_id),
            url=url,
            already_tracked=bool(tracked),
            title_before=title,
            unresolved=True,
        )
    try:
        detail = fetch_detail(resolved, http)
    except Exception as exc:
        log.warning("refetch failed for %s: %s", url, exc)
        return RowResult(
            posting_id=int(posting_id),
            url=url,
            already_tracked=bool(tracked),
            title_before=title,
            error=f"{type(exc).__name__}: {exc}",
        )

    result = diff_row(row, detail)
    result.sheet_row = sheet_row
    _plan_packet_rebuild(result, normalized_company, title_slug)
    if dry_run:
        return result

    jd = detail.jd_markdown or None
    # Only pass a title when the ATS actually supplied one, so a record missing
    # the field cannot blank out a good stored title (update_jd_capture leaves
    # the title and slug alone when passed None).
    db.update_jd_capture(
        client,
        int(posting_id),
        jd_markdown=jd,
        location=detail.location,
        title=detail.title or None,
    )
    # DB first, Sheet second: the tracker is a projection of the row just
    # corrected, so refresh its Title cell when the row sits there unapplied.
    # A failed Sheet write must not undo or block the reconciliation — it
    # degrades to the flagged hand-fix in describe().
    if "title" in result.changed_fields and sheet_row is not None:
        try:
            update_title(spreadsheet_id(), sheet_row, result.title_after)
        except TrackerError as exc:
            result.sheet_error = str(exc)
        else:
            result.sheet_updated = True
    _apply_packet_rebuild(result, detail.jd_markdown or jd_before)
    return result


def _plan_packet_rebuild(result: RowResult, normalized_company: str, old_slug: str) -> None:
    """Mark a stale packet directory for rebuild, if one exists on disk.

    The packet is a derived artifact — ``job_posting.md`` projects the DB row,
    and the (future) Step 4 resume is a function of the JD — so a title or
    description change invalidates it wholesale (decided 2026-08-16): the job
    no longer exists as it was packeted. A location-only change touches
    nothing, since the packet does not contain the location.
    """
    if not {"title", "description"} & set(result.changed_fields):
        return
    old_path = packets_dir() / packet_dir_name(normalized_company, old_slug)
    if not old_path.is_dir():
        return
    new_slug = slugify_title(result.title_after) if result.title_after else old_slug
    result.packet_old = old_path
    result.packet_new = packets_dir() / packet_dir_name(normalized_company, new_slug)


def _apply_packet_rebuild(result: RowResult, jd_markdown: str | None) -> None:
    """Delete the stale packet directory and build its replacement.

    Deleting rather than archiving is deliberate: the contents are regenerable
    by construction, and since a packet directory's existence is Step 4's
    idempotency guard, removing a stale one *is* the signal that the job needs
    a fresh packet and resume. The delete only ever targets the exact expected
    old path, and refuses to clobber a distinct directory already sitting at
    the new name.
    """
    if result.packet_new is None:
        return
    if result.packet_new != result.packet_old and result.packet_new.exists():
        result.packet_error = f"a directory already exists at {result.packet_new}"
        return
    try:
        shutil.rmtree(result.packet_old)
        result.packet_new.mkdir(parents=True)
        if jd_markdown:
            write_job_posting(result.packet_new, jd_markdown)
    except OSError as exc:
        result.packet_error = f"{type(exc).__name__}: {exc}"
        return
    result.packet_rebuilt = True


def describe(result: RowResult) -> str:
    """One human-readable line per examined posting."""
    if result.error:
        return f"  ! id {result.posting_id}: fetch failed — {result.error}"
    if result.unresolved:
        return f"  - id {result.posting_id}: no supported ATS to re-read; left as is"
    if not result.changed:
        return f"  = id {result.posting_id}: unchanged"
    lines = [f"  ~ id {result.posting_id}: {', '.join(result.changed_fields)} changed"]
    if "title" in result.changed_fields:
        lines.append(f"      title: {result.title_before!r} → {result.title_after!r}")
        lines.append("      (title_slug re-derived to match)")
        if result.sheet_updated:
            lines.append("      tracker row Title refreshed to match")
        elif result.sheet_error:
            lines.append(
                "      NOTE: the tracker row's Title could not be refreshed — fix it "
                f"by hand. ({result.sheet_error})"
            )
        elif result.sheet_refresh_pending:
            lines.append("      tracker row Title would be refreshed to match")
        elif result.already_tracked:
            lines.append(
                "      NOTE: already in the tracker Sheet, but its row could not be "
                "matched (or is already applied to) — check that row by hand."
            )
    if result.packet_error:
        lines.append(f"      NOTE: stale packet directory NOT rebuilt — {result.packet_error}")
    elif result.packet_rebuilt:
        lines.append(f"      stale packet directory rebuilt: {result.packet_new}")
    elif result.packet_new is not None:
        lines.append(f"      stale packet directory would be rebuilt: {result.packet_new}")
    return "\n".join(lines)
