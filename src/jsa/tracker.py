"""Step 5: elevate ``Apply`` postings into the write-only Google Sheet tracker.

The Sheet is append-only from the agent's side — it never reads back — so the
write is a single ``values.append`` per posting and application state (Date
Applied, Status) stays in the Sheet, deliberately unmirrored into Turso.

**Idempotency** is the ``added_to_tracker`` column, exactly as the PRD
specifies: the backlog query selects ``decision = 'Apply' AND added_to_tracker
= 0``, and the flag is set immediately after each successful append, so an
already-elevated row cannot be appended twice. Rows are appended one at a time
(rather than batched into one call) so the flag is written per row and a single
rejected posting cannot strand the rest of the backlog in an ambiguous state.

**Credential placement** follows the cloud/local split: the append shells out to
the local ``gws`` CLI, which holds the OAuth refresh token for the user's
personal Google account. That credential never reaches the Fly.io container.
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
TRACKER_RANGE = "Applications!A:G"

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
    """One posting rendered into the Sheet's seven columns (A–G)."""

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


def append_row(row: TrackerRow, sheet_id: str) -> int:
    """Append one row to the tracker Sheet; return the rows the API confirmed.

    ``INSERT_ROWS`` adds at the bottom without any row-index bookkeeping or
    read-modify-write, and ``USER_ENTERED`` lets the ISO dates land as real
    dates rather than text. Raises ``TrackerError`` unless the response reports
    at least one updated row.
    """
    params = {
        "spreadsheetId": sheet_id,
        "range": TRACKER_RANGE,
        "valueInputOption": "USER_ENTERED",
        "insertDataOption": "INSERT_ROWS",
    }
    body = {"values": [row.as_values()]}
    command = [
        _gws_binary(),
        "sheets",
        "spreadsheets",
        "values",
        "append",
        "--params",
        json.dumps(params),
        "--json",
        json.dumps(body),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise TrackerError(
            f"the {_gws_binary()!r} CLI was not found on PATH — the tracker write needs it "
            "(it holds the local Google OAuth token)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TrackerError("the gws Sheets append timed out after 60s.") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if completed.returncode == _GWS_AUTH_EXIT:
            # gws documents exit 2 as "credentials missing or invalid". The
            # usual cause is a refresh token that Google expired: an OAuth
            # client left in "Testing" publishing status has its grants expire
            # after 7 days, so this recurs weekly until the consent screen is
            # published. Say so, rather than surfacing a raw invalid_grant.
            raise TrackerError(
                "the local Google OAuth grant is invalid or expired — re-authorize with "
                "`gws auth login`, then re-run `jsa track` (nothing was appended, and the "
                f"postings are still queued).\n      {detail}"
            )
        raise TrackerError(f"gws exited {completed.returncode}: {detail}")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TrackerError(f"gws returned non-JSON output: {completed.stdout[:200]!r}") from exc
    updated = (response.get("updates") or {}).get("updatedRows")
    if not updated:
        raise TrackerError(f"the Sheets API reported no appended row: {response!r}")
    return int(updated)


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
