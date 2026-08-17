"""Step 5: elevate ``Apply`` postings into the Google Sheet tracker.

The relationship (reframed 2026-08-16): **the database is the source of truth
for posting data; the Sheet is its human-readable projection plus Nicky's
workspace.** Concretely, the Sheet's columns split into two classes. The
agent-written columns (ID, Company, Title, URL, Date Posted, Date Added) are a
view of the ``postings`` row — the agent appends them here and ``jsa refetch``
refreshes the Title cell when the underlying row changes (``update_title``).
The user columns (Date Applied, Status) are written only by Nicky; they are
the one application state the system keeps, deliberately unmirrored into
Turso, and the agent touches them in exactly one way: ``read_tracker_index``
reads Date Applied so refetch can scope itself to postings not yet applied to.
Authority never flows Sheet → database for posting data, and no agent write
ever lands in a user column.

**Idempotency** is the ``added_to_tracker`` column, exactly as the PRD
specifies: the backlog query selects ``decision = 'Apply' AND added_to_tracker
= 0``, and the flag is set immediately after each successful append, so an
already-elevated row cannot be appended twice. Rows are appended one at a time
(rather than batched into one call) so the flag is written per row and a single
rejected posting cannot strand the rest of the backlog in an ambiguous state.

**Credential placement** follows the cloud/local split: the append shells out to
the local ``gws`` CLI, which holds the OAuth refresh token for the user's
personal Google account. That credential never reaches the Fly.io container.

**Why ``OVERWRITE`` and not ``INSERT_ROWS``.** The PRD originally specified
``insertDataOption = INSERT_ROWS``, reasoning that appending at the bottom
avoids row-index bookkeeping and read-modify-write. It does -- but it also
inserts a *new* row, and measurement on the live sheet (2026-08-11) showed that
carries two costs the reasoning missed:

1. **The Status dropdown is lost.** The inserted row lands above the data
   validation and conditional-formatting ranges, so it gets neither the enum
   dropdown nor the per-status colors. Worse, inserting above those ranges
   shifts them down by one, so every append drags the covered region further
   from the data and the sheet never self-corrects.
2. **It inherits the header's formatting.** A row inserted directly beneath the
   bold grey header comes out bold and grey, and each later append then inherits
   from that row, propagating indefinitely.

``OVERWRITE`` writes into the sheet's already-existing blank rows instead of
inserting. Sheets still locates the table server-side and writes after its last
row, so the PRD's actual requirement -- no bookkeeping, no read-modify-write --
is met, while the written row keeps the validation, conditional formatting and
default styling it already had. The tradeoff is that anything a user parks in
the rows directly below the table would be overwritten; that region is the
tracker's own growth area, so this is the right trade.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime

from . import db
from .config import Config
from .search.prompt import EASTERN

log = logging.getLogger(__name__)

# Fixed configuration per prd.md — the agent targets this Sheet, it does not
# discover it at runtime. Overridable for a scratch copy while testing.
DEFAULT_SPREADSHEET_ID = "1DQNix3tZ9oFqfA9R2r0Npj1UWvAu2cEg_RJVf6SGki4"
# Eight columns, ID first (added 2026-08-16): the postings.id in column A is
# the join key that lets refetch's scope lookup match Sheet rows back to DB
# rows without comparing URLs.
TRACKER_RANGE = "Applications!A:H"

# 0-indexed positions in a TRACKER_RANGE row, for the index read below, and
# the A1-notation column of the one cell refetch may refresh.
_ID_COL = 0
_DATE_APPLIED_COL = 6
_TITLE_COL = "C"

# Write into the sheet's existing empty rows instead of inserting new ones.
# See the module docstring: INSERT_ROWS silently broke the Status dropdown.
INSERT_DATA_OPTION = "OVERWRITE"

# gws's documented exit code for "credentials missing or invalid".
_GWS_AUTH_EXIT = 2


class TrackerError(RuntimeError):
    """The append did not demonstrably land a row in the Sheet.

    Raised on any ambiguity — non-zero exit, unparseable output, or a response
    that does not report an updated row. Treating ambiguity as failure is what
    keeps ``added_to_tracker`` honest: the flag is only ever set after the Sheet
    API itself confirms the row, so a posting is never silently dropped from the
    backlog without reaching the tracker.
    """


@dataclass(frozen=True)
class TrackerRow:
    """One posting rendered into the Sheet's eight columns (A–H)."""

    posting_id: int
    company: str
    title: str
    url: str
    date_posted: str
    date_added: str

    def as_values(self) -> list[str]:
        """The row as the Sheet stores it.

        Date Applied and Status are written blank on purpose: they are the
        user's columns, filled in by hand as an application progresses.
        """
        return [
            str(self.posting_id),  # ID — the postings.id, refetch's join key
            self.company,
            self.title,
            self.url,
            self.date_posted,
            self.date_added,
            "",  # Date Applied — the user's
            "",  # Status — the user's (dropdown)
        ]


def build_row(row: tuple, date_added: str) -> TrackerRow:
    """Render a ``pending_tracker`` row into tracker columns. Pure.

    ``Company`` comes from ``normalized_company`` (suffix-stripped and
    filesystem-safe) so the tracker, the application-packet directory, and the
    resume filenames all carry the same company rendering.
    """
    posting_id, normalized_company, title, url, date_posted = row
    return TrackerRow(
        posting_id=int(posting_id),
        company=normalized_company or "",
        title=title or "",
        url=url or "",
        date_posted=date_posted or "",
        date_added=date_added,
    )


def _gws_binary() -> str:
    """Path to the ``gws`` CLI (overridable so tests can stub the Sheet write)."""
    return os.environ.get("JSA_GWS_BIN") or "gws"


def spreadsheet_id() -> str:
    """The tracker Sheet id, overridable via ``JSA_TRACKER_SPREADSHEET_ID``."""
    return os.environ.get("JSA_TRACKER_SPREADSHEET_ID") or DEFAULT_SPREADSHEET_ID


def _run_gws(args: list[str], *, timeout: int = 60) -> dict:
    """Shell out to gws and return its parsed JSON, raising on any ambiguity.

    Shared by the append and the read-only scope lookup so both report the same
    failure modes: a missing binary, a timeout, gws's documented exit 2 for a
    lapsed OAuth grant (a Testing-status client expires refresh tokens every 7
    days until the consent screen is published — say so rather than surfacing a
    raw invalid_grant), and unparseable output.
    """
    command = [_gws_binary(), *args]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise TrackerError(
            f"the {_gws_binary()!r} CLI was not found on PATH — talking to the tracker Sheet "
            "needs it (it holds the local Google OAuth token)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TrackerError(f"the gws call timed out after {timeout}s.") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if completed.returncode == _GWS_AUTH_EXIT:
            raise TrackerError(
                "the local Google OAuth grant is invalid or expired — re-authorize with "
                f"`gws auth login`, then re-run (nothing was written).\n      {detail}"
            )
        raise TrackerError(f"gws exited {completed.returncode}: {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TrackerError(f"gws returned non-JSON output: {completed.stdout[:200]!r}") from exc


def append_row(row: TrackerRow, sheet_id: str) -> int:
    """Append one row to the tracker Sheet; return the rows the API confirmed.

    Sheets finds the table in ``TRACKER_RANGE`` and writes after its last row,
    so there is no row-index bookkeeping or read-modify-write, and
    ``USER_ENTERED`` lets the ISO dates land as real dates rather than text.
    Raises ``TrackerError`` unless the response reports at least one updated row.

    ``OVERWRITE`` rather than ``INSERT_ROWS`` — see the module docstring for why
    that distinction is load-bearing rather than cosmetic.
    """
    params = {
        "spreadsheetId": sheet_id,
        "range": TRACKER_RANGE,
        "valueInputOption": "USER_ENTERED",
        "insertDataOption": INSERT_DATA_OPTION,
    }
    body = {"values": [row.as_values()]}
    response = _run_gws(
        [
            "sheets",
            "spreadsheets",
            "values",
            "append",
            "--params",
            json.dumps(params),
            "--json",
            json.dumps(body),
        ]
    )
    updated = (response.get("updates") or {}).get("updatedRows")
    if not updated:
        raise TrackerError(f"the Sheets API reported no appended row: {response!r}")
    return int(updated)


@dataclass(frozen=True)
class TrackedRow:
    """One Sheet data row, as ``jsa refetch``'s scope-and-propagation index."""

    row_number: int  # 1-based Sheet row — the values.update target
    date_applied: str  # "" = in the tracker but not yet applied to


def read_tracker_index(sheet_id: str) -> dict[int, TrackedRow]:
    """Read the Sheet's ID → (row number, Date Applied) index for refetch.

    Two uses, both refetch's: Date Applied scopes the run to postings not yet
    applied to (where a correction still changes something), and the row number
    is where ``update_title`` refreshes the projection when the DB row changes.
    A row whose ID cell is not an integer — the header, or a row typed into the
    Sheet by hand — has no DB row to index, so it is skipped (but still counts
    toward row numbering). A missing Date Applied cell (the Sheets API
    truncates trailing blanks) reads as the empty string, i.e. "tracked but not
    yet applied".
    """
    params = {"spreadsheetId": sheet_id, "range": TRACKER_RANGE}
    response = _run_gws(["sheets", "spreadsheets", "values", "get", "--params", json.dumps(params)])
    index: dict[int, TrackedRow] = {}
    # Row 1 is the header, so data enumeration starts at Sheet row 2.
    for row_number, row in enumerate((response.get("values") or [])[1:], start=2):
        try:
            posting_id = int(str(row[_ID_COL]).strip())
        except (IndexError, ValueError):
            continue
        date_applied = row[_DATE_APPLIED_COL] if len(row) > _DATE_APPLIED_COL else ""
        index[posting_id] = TrackedRow(
            row_number=row_number, date_applied=str(date_applied).strip()
        )
    return index


def update_title(sheet_id: str, row_number: int, title: str) -> None:
    """Refresh one tracked row's Title cell from the database row.

    The agent-written columns are a projection of ``postings`` (the DB is the
    source of truth), so when refetch corrects a title, the projection follows
    — for rows not yet applied to. This writes exactly one cell and can never
    touch the user's columns. Raises ``TrackerError`` unless the API confirms
    the updated cell, so a silent no-op cannot masquerade as a refresh.
    """
    params = {
        "spreadsheetId": sheet_id,
        "range": f"Applications!{_TITLE_COL}{row_number}",
        "valueInputOption": "USER_ENTERED",
    }
    response = _run_gws(
        [
            "sheets",
            "spreadsheets",
            "values",
            "update",
            "--params",
            json.dumps(params),
            "--json",
            json.dumps({"values": [[title]]}),
        ]
    )
    if not response.get("updatedCells"):
        raise TrackerError(f"the Sheets API reported no updated cell: {response!r}")


@dataclass
class TrackerSummary:
    """Counts from one elevation pass, for the closing log line."""

    eligible: int = 0
    appended: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return f"eligible={self.eligible} appended={self.appended} failed={self.failed}"


def run_tracker(
    config: Config,
    *,
    posting_id: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> TrackerSummary:
    """Append every eligible ``Apply`` posting to the tracker Sheet.

    Each row is appended and flagged individually, so a failure on one posting
    leaves the others elevated and the failed one still in the backlog for the
    next run. ``dry_run`` prints the exact values without calling ``gws``.
    """
    summary = TrackerSummary()
    now = now or datetime.now(EASTERN)
    date_added = now.date().isoformat()
    sheet_id = spreadsheet_id()

    if not dry_run and shutil.which(_gws_binary()) is None:
        raise TrackerError(
            f"the {_gws_binary()!r} CLI was not found on PATH — the tracker write needs it "
            "(it holds the local Google OAuth token). Re-run with --dry-run to preview."
        )

    client = db.connect(config)
    db.init_db(client)
    try:
        rows = db.pending_tracker(client, posting_id)
        summary.eligible = len(rows)
        if not rows:
            return summary
        if dry_run:
            print(f"{summary.eligible} posting(s) would be appended (dry run):")
        for row in rows:
            tracker_row = build_row(row, date_added)
            label = f"[id {tracker_row.posting_id}] {tracker_row.company} — {tracker_row.title}"
            if dry_run:
                print(f"  {label}")
                print(f"      {TRACKER_RANGE} <- {tracker_row.as_values()}")
                continue
            try:
                append_row(tracker_row, sheet_id)
            except TrackerError as exc:
                summary.failed += 1
                log.error("tracker append failed for %s: %s", label, exc)
                print(f"  ✗ {label}\n      {exc}")
                continue
            # Only now, with the API's own confirmation in hand.
            db.mark_tracked(client, tracker_row.posting_id)
            summary.appended += 1
            print(f"  ✓ {label}  (added_to_tracker = 1)")
    finally:
        client.close()
    return summary
