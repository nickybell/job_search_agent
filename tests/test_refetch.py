"""Reconciling a stored posting against its (mutable) ATS record.

The motivating case is real: a Stepful req was retitled in place from
"Fractional GTM Enablement Lead" to "Fractional Sales Enablement Lead" under an
unchanged URL and publishedAt. These pin that such a change is detected and
applied, and -- more importantly -- that a fetch failure can never destroy a
good capture.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import make_posting

from jsa import db, refetch
from jsa.ats.fetch import ATSDetail
from jsa.tracker import TrackedRow, TrackerError

GREENHOUSE_URL = "https://job-boards.greenhouse.io/acme/jobs/4567"
OLD_TITLE = "Fractional GTM Enablement Lead"
NEW_TITLE = "Fractional Sales Enablement Lead"


class IndexStub:
    """Stands in for tracker.read_tracker_index: {posting_id: TrackedRow}."""

    def __init__(self):
        self.data: dict[int, TrackedRow] = {}
        self.calls = 0

    def __call__(self, sheet_id: str) -> dict[int, TrackedRow]:
        self.calls += 1
        return dict(self.data)


@pytest.fixture(autouse=True)
def sheet_index(monkeypatch) -> IndexStub:
    # Autouse so no test can accidentally shell out to the real gws CLI for
    # the Sheet index read; default is an empty Sheet (nothing tracked).
    stub = IndexStub()
    monkeypatch.setattr(refetch, "read_tracker_index", stub)
    return stub


@pytest.fixture(autouse=True)
def title_updates(monkeypatch) -> list[tuple[int, str]]:
    # Autouse for the same reason: the Title refresh is a real Sheet write.
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        refetch,
        "update_title",
        lambda sheet_id, row_number, title: calls.append((row_number, title)),
    )
    return calls


@pytest.fixture
def stub_fetch(monkeypatch):
    def _stub(detail=None, raises=None):
        def fake(resolved, http_client):
            if raises is not None:
                raise raises
            return detail

        monkeypatch.setattr(refetch, "fetch_detail", fake)

    return _stub


def seed(client, *, decision="Apply", **overrides) -> int:
    # decision defaults to Apply because that is the refetch scope now: only
    # rows where drift could still change the user's next action are re-read.
    fields = {
        "title": OLD_TITLE,
        "title_slug": OLD_TITLE,
        "url": GREENHOUSE_URL,
        "canonical_url": GREENHOUSE_URL,
    }
    fields.update(overrides)
    posting_id = db.insert_posting(client, make_posting(**fields))
    db.update_jd_capture(
        client, posting_id, jd_markdown="the original JD", location="Remote", title=None
    )
    if decision:
        db.record_decision(client, posting_id, decision, None)
    return posting_id


def row(client, posting_id) -> dict:
    cursor = client.execute(
        "SELECT title, title_slug, jd_markdown, location FROM postings WHERE id = ?",
        (posting_id,),
    )
    keys = "title title_slug jd_markdown location".split()
    return dict(zip(keys, cursor.fetchone(), strict=True))


# --- diff_row (pure) -------------------------------------------------------


def test_diff_row_flags_a_retitled_posting():
    result = refetch.diff_row(
        (7, GREENHOUSE_URL, OLD_TITLE, "Remote", 0),
        ATSDetail(jd_markdown="x", location="Remote", title=NEW_TITLE),
    )
    assert result.changed_fields == ["title"]
    assert (result.title_before, result.title_after) == (OLD_TITLE, NEW_TITLE)


def test_diff_row_reports_no_change_when_the_ats_still_agrees():
    result = refetch.diff_row(
        (7, GREENHOUSE_URL, OLD_TITLE, "Remote", 0),
        ATSDetail(jd_markdown="x", location="Remote", title=OLD_TITLE),
    )
    assert result.changed is False


def test_diff_row_ignores_a_missing_ats_title():
    # An ATS record without a title must not read as "retitled to nothing".
    result = refetch.diff_row(
        (7, GREENHOUSE_URL, OLD_TITLE, "Remote", 0),
        ATSDetail(jd_markdown="x", location=None, title=None),
    )
    assert result.changed_fields == []


# --- applying the reconciliation -------------------------------------------


def test_a_retitled_posting_is_updated_and_the_slug_re_derived(config, client, stub_fetch):
    posting_id = seed(client)
    stub_fetch(ATSDetail(jd_markdown="the revised JD", location="Remote, US", title=NEW_TITLE))

    summary, results = refetch.run_refetch(config)

    assert (summary.examined, summary.changed) == (1, 1)
    stored = row(client, posting_id)
    assert stored["title"] == NEW_TITLE
    # title_slug must track the title, or Step 4's packet naming drifts.
    assert stored["title_slug"] == NEW_TITLE
    assert stored["jd_markdown"] == "the revised JD"
    assert stored["location"] == "Remote, US"
    assert results[0].changed_fields == ["title", "location"]


def test_dry_run_reports_without_writing(config, client, stub_fetch):
    posting_id = seed(client)
    stub_fetch(ATSDetail(jd_markdown="the revised JD", location="Remote", title=NEW_TITLE))

    summary, results = refetch.run_refetch(config, dry_run=True)

    assert summary.changed == 1
    assert results[0].title_after == NEW_TITLE
    assert row(client, posting_id)["title"] == OLD_TITLE


def test_a_failed_fetch_leaves_the_row_completely_untouched(config, client, stub_fetch):
    # The whole point: never trade a good capture for a network blip.
    posting_id = seed(client)
    stub_fetch(raises=httpx.HTTPError("boom"))

    summary, results = refetch.run_refetch(config)

    assert (summary.failed, summary.changed) == (1, 0)
    assert "HTTPError" in results[0].error
    assert row(client, posting_id) == {
        "title": OLD_TITLE,
        "title_slug": OLD_TITLE,
        "jd_markdown": "the original JD",
        "location": "Remote",
    }


def test_a_pulled_posting_is_reported_not_deleted(config, client, stub_fetch):
    # A 404/LookupError means the req is gone from the board. That is worth
    # surfacing, but the stored JD is the only remaining record of it.
    posting_id = seed(client)
    stub_fetch(raises=LookupError("job 4567 not found on board acme"))

    summary, _ = refetch.run_refetch(config)

    assert summary.failed == 1
    assert row(client, posting_id)["jd_markdown"] == "the original JD"


def test_an_unsupported_ats_is_skipped_not_failed(config, client, stub_fetch):
    posting_id = db.insert_posting(
        client,
        make_posting(
            url="https://acme.wd1.myworkdayjobs.com/careers/job/R-42",
            canonical_url="https://acme.wd1.myworkdayjobs.com/careers/job/R-42",
        ),
    )
    db.record_decision(client, posting_id, "Apply", None)
    stub_fetch(ATSDetail(jd_markdown="unused", location=None, title="Unused"))

    summary, results = refetch.run_refetch(config)

    assert (summary.unresolved, summary.failed) == (1, 0)
    assert results[0].unresolved is True
    assert row(client, posting_id)["title"] == "Customer Enablement Lead"


# --- row selection ---------------------------------------------------------


def test_skip_and_undecided_rows_are_out_of_scope_by_default(config, client, stub_fetch):
    # A refetch there cannot change what the user does next.
    seed(client, decision="Skip")
    seed(
        client,
        decision=None,
        url="https://jobs.lever.co/acme/abc123def456",
        canonical_url="https://jobs.lever.co/acme/abc123def456",
    )
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    summary, _ = refetch.run_refetch(config)

    assert summary.examined == 0


def test_scope_is_an_or_absent_from_sheet_or_no_date_applied(
    config, client, stub_fetch, sheet_index
):
    # The spec: Apply AND (absent from the Sheet OR blank Date Applied).
    # Three Apply rows pin the truth table: absent → in, present-blank → in,
    # present-dated → out.
    absent = seed(client)
    present_blank = seed(
        client,
        url="https://jobs.lever.co/acme/abc123def456",
        canonical_url="https://jobs.lever.co/acme/abc123def456",
    )
    present_dated = seed(
        client,
        url="https://job-boards.greenhouse.io/acme/jobs/999",
        canonical_url="https://job-boards.greenhouse.io/acme/jobs/999",
    )
    sheet_index.data[present_blank] = TrackedRow(row_number=2, date_applied="")
    sheet_index.data[present_dated] = TrackedRow(row_number=3, date_applied="2026-08-15")
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    summary, results = refetch.run_refetch(config)

    assert (summary.examined, summary.skipped_applied) == (2, 1)
    assert {r.posting_id for r in results} == {absent, present_blank}
    assert row(client, present_dated)["title"] == OLD_TITLE


def test_a_corrected_title_is_propagated_to_the_unapplied_tracker_row(
    config, client, stub_fetch, sheet_index, title_updates
):
    # The Sheet is a projection of the database, so a title correction on a
    # tracked-but-unapplied row refreshes the Sheet's Title cell too.
    posting_id = seed(client)
    db.mark_tracked(client, posting_id)
    sheet_index.data[posting_id] = TrackedRow(row_number=4, date_applied="")
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    summary, results = refetch.run_refetch(config)

    assert (summary.changed, summary.tracker_updated, summary.stale_in_tracker) == (1, 1, 0)
    assert title_updates == [(4, NEW_TITLE)]
    assert results[0].sheet_updated is True
    assert "tracker row Title refreshed" in refetch.describe(results[0])


def test_a_failed_title_refresh_degrades_to_the_hand_fix_flag(
    config, client, stub_fetch, sheet_index, monkeypatch
):
    # DB first, Sheet second: the reconciliation must survive a Sheet failure,
    # with the stale row flagged rather than silently left wrong.
    posting_id = seed(client)
    db.mark_tracked(client, posting_id)
    sheet_index.data[posting_id] = TrackedRow(row_number=4, date_applied="")

    def boom(sheet_id, row_number, title):
        raise TrackerError("gws exited 1")

    monkeypatch.setattr(refetch, "update_title", boom)
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    summary, results = refetch.run_refetch(config)

    assert (summary.changed, summary.tracker_updated, summary.stale_in_tracker) == (1, 0, 1)
    assert row(client, posting_id)["title"] == NEW_TITLE  # the DB write stands
    assert "fix it by hand" in refetch.describe(results[0])


def test_dry_run_reports_the_pending_title_refresh_without_writing(
    config, client, stub_fetch, sheet_index, title_updates
):
    posting_id = seed(client)
    db.mark_tracked(client, posting_id)
    sheet_index.data[posting_id] = TrackedRow(row_number=4, date_applied="")
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    summary, results = refetch.run_refetch(config, dry_run=True)

    assert summary.tracker_updated == 1
    assert title_updates == []
    assert "would be refreshed" in refetch.describe(results[0])
    assert row(client, posting_id)["title"] == OLD_TITLE


def test_a_failed_index_read_fails_loudly_in_the_default_scope(
    config, client, stub_fetch, monkeypatch
):
    # Silently refetching everything (or nothing) would defeat the scoping,
    # so a broken gws read must surface, not degrade.
    posting_id = seed(client)

    def boom(sheet_id):
        raise TrackerError("gws exited 2")

    monkeypatch.setattr(refetch, "read_tracker_index", boom)
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    with pytest.raises(TrackerError):
        refetch.run_refetch(config)
    assert row(client, posting_id)["title"] == OLD_TITLE


def test_all_bypasses_the_apply_filter(config, client, stub_fetch):
    seed(client, decision="Skip")
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    summary, results = refetch.run_refetch(config, include_all=True)

    assert summary.examined == 1
    assert results[0].changed_fields == ["title"]


def test_all_survives_a_failed_index_read_and_flags_unrefreshed_drift(
    config, client, stub_fetch, monkeypatch
):
    # Under --all the selection does not depend on the Sheet, so the index
    # read is best-effort: its failure only forfeits the Title refresh.
    posting_id = seed(client)
    db.mark_tracked(client, posting_id)

    def boom(sheet_id):
        raise TrackerError("gws exited 2")

    monkeypatch.setattr(refetch, "read_tracker_index", boom)
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    summary, results = refetch.run_refetch(config, include_all=True)

    assert (summary.examined, summary.changed, summary.stale_in_tracker) == (1, 1, 1)
    assert row(client, posting_id)["title"] == NEW_TITLE
    assert "check that row by hand" in refetch.describe(results[0])


def test_id_targets_a_single_row_and_still_refreshes_its_tracker_title(
    config, client, stub_fetch, sheet_index, title_updates
):
    # --id selects unconditionally, but the projection still follows: a
    # tracked-but-unapplied row gets its Sheet Title refreshed.
    kept = seed(client)
    other = db.insert_posting(
        client,
        make_posting(
            url="https://jobs.lever.co/acme/abc123def456",
            canonical_url="https://jobs.lever.co/acme/abc123def456",
        ),
    )
    db.mark_tracked(client, kept)
    sheet_index.data[kept] = TrackedRow(row_number=2, date_applied="")
    stub_fetch(ATSDetail(jd_markdown="x", location=None, title=NEW_TITLE))

    summary, results = refetch.run_refetch(config, posting_id=kept)

    assert summary.examined == 1
    assert results[0].posting_id == kept
    assert row(client, other)["title"] == "Customer Enablement Lead"
    assert title_updates == [(2, NEW_TITLE)]
