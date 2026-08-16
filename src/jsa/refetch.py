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

Two things it will not do. A failed fetch leaves the row completely untouched
rather than overwriting a good description with nothing -- the same
degrade-don't-destroy stance the pipeline takes, applied in the direction that
matters here. And a row already written to the tracker is flagged when it has
drifted, because the Sheet copy cannot be corrected from this side: the agent
only ever appends to it.

Scope (decided 2026-08-16): only postings where drift could still change what
Nicky does next -- ``decision = 'Apply'`` rows minus those already applied to.
"Already applied" lives only in the Sheet's Date Applied column, so the scope
check reads the tracker via ``tracker.read_applied_dates``, the one sanctioned
read of the otherwise write-only Sheet (it mirrors nothing into the database
and no write depends on it). If that lookup fails, the run fails loudly rather
than guessing at scope. ``--all`` widens to every stored row and skips the
lookup, as does an explicit ``--id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from . import db
from .ats import fetch_detail, resolve_ats_url
from .config import Config
from .tracker import read_applied_dates, spreadsheet_id

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

    @property
    def changed(self) -> bool:
        return bool(self.changed_fields)


@dataclass
class RefetchSummary:
    """Counts from one reconciliation pass."""

    examined: int = 0
    changed: int = 0
    unchanged: int = 0
    unresolved: int = 0
    failed: int = 0
    stale_in_tracker: int = 0
    skipped_applied: int = 0

    def __str__(self) -> str:
        return (
            f"examined={self.examined} changed={self.changed} unchanged={self.unchanged} "
            f"unresolved={self.unresolved} failed={self.failed} "
            f"stale_in_tracker={self.stale_in_tracker} skipped_applied={self.skipped_applied}"
        )


def diff_row(row: tuple, detail) -> RowResult:
    """Compare a stored row against a freshly fetched ATS record. Pure.

    ``title`` is compared exactly: an employer retitling a req is precisely the
    drift this exists to catch, and normalizing the comparison would hide it.
    """
    posting_id, url, title, location, tracked = row
    result = RowResult(
        posting_id=int(posting_id),
        url=url,
        already_tracked=bool(tracked),
        title_before=title,
        title_after=getattr(detail, "title", None),
    )
    if result.title_after and result.title_after != title:
        result.changed_fields.append("title")
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

    The default scope is Apply rows not yet applied to, per the tracker Sheet's
    Date Applied column — raising ``tracker.TrackerError`` if that lookup fails,
    since silently refetching everything (or nothing) would defeat the scoping.
    An explicit ``posting_id`` or ``include_all`` bypasses the lookup entirely.
    """
    summary = RefetchSummary()
    results: list[RowResult] = []

    client = db.connect(config)
    db.init_db(client)
    try:
        rows = db.rows_for_refetch(client, posting_id, include_all=include_all)
        if posting_id is None and not include_all and rows:
            applied = read_applied_dates(spreadsheet_id())
            kept = [r for r in rows if not applied.get(int(r[0]), "")]
            summary.skipped_applied = len(rows) - len(kept)
            rows = kept
        summary.examined = len(rows)
        if not rows:
            return summary, results
        with httpx.Client(follow_redirects=True) as http:
            for row in rows:
                result = _refetch_one(client, row, http, dry_run=dry_run)
                results.append(result)
                if result.error:
                    summary.failed += 1
                elif result.unresolved:
                    summary.unresolved += 1
                elif result.changed:
                    summary.changed += 1
                    if result.already_tracked:
                        summary.stale_in_tracker += 1
                else:
                    summary.unchanged += 1
    finally:
        client.close()
    return summary, results


def _refetch_one(client, row: tuple, http: httpx.Client, *, dry_run: bool) -> RowResult:
    posting_id, url, title, _location, tracked = row
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
    return result


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
    if result.already_tracked:
        lines.append(
            "      NOTE: already in the tracker Sheet, which this cannot correct — "
            "the agent only appends. Fix that row by hand."
        )
    return "\n".join(lines)
