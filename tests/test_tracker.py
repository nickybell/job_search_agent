"""Step 5: elevating Apply postings to the Google Sheet tracker.

The Sheet write shells out to the local ``gws`` CLI, so these tests point
``JSA_GWS_BIN`` at a stub that records its arguments and returns a canned
Sheets API response. Nothing here reaches Google.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap

import pytest
from conftest import make_posting

from jsa import db, tracker


def _write_stub(tmp_path, body: str):
    """Install a fake `gws` on JSA_GWS_BIN; returns the path it logs calls to."""
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "gws-stub"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, sys
            with open({str(log)!r}, "a") as fh:
                fh.write(json.dumps(sys.argv[1:]) + "\\n")
            {body}
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["JSA_GWS_BIN"] = str(script)
    return log


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JSA_TRACKER_SPREADSHEET_ID", "sheet-under-test")
    monkeypatch.delenv("JSA_GWS_BIN", raising=False)
    yield
    os.environ.pop("JSA_GWS_BIN", None)


@pytest.fixture
def ok_stub(tmp_path):
    return _write_stub(
        tmp_path,
        'print(json.dumps({"updates": {"updatedRows": 1, "updatedCells": 7}}))',
    )


def seed(client, *, decision="Apply", tracked=0, company="Acme", n=1) -> list[int]:
    ids = []
    for i in range(n):
        posting_id = db.insert_posting(
            client,
            make_posting(
                company=f"{company}{i}",
                normalized_company=f"{company}{i}",
                url=f"https://job-boards.greenhouse.io/a{i}/jobs/{i}",
                canonical_url=f"https://job-boards.greenhouse.io/a{i}/jobs/{i}",
            ),
        )
        if decision:
            db.record_decision(client, posting_id, decision, None)
        if tracked:
            db.mark_tracked(client, posting_id)
        ids.append(posting_id)
    return ids


def tracked_flags(client) -> list[int]:
    return [r[0] for r in client.execute("SELECT added_to_tracker FROM postings ORDER BY id")]


# --- build_row (pure) ------------------------------------------------------


def test_build_row_maps_the_seven_columns():
    row = tracker.build_row(
        (7, "Acme", "Enablement Lead", "https://x/1", "2026-08-08"), "2026-08-10"
    )
    assert row.posting_id == 7
    # Date Applied and Status are the user's columns and must go out blank.
    assert row.as_values() == [
        "Acme",
        "Enablement Lead",
        "https://x/1",
        "2026-08-08",
        "2026-08-10",
        "",
        "",
    ]


def test_build_row_renders_a_missing_date_posted_as_blank():
    row = tracker.build_row((7, "Acme", "Lead", "https://x/1", None), "2026-08-10")
    assert row.as_values()[3] == ""


# --- eligibility and idempotency ------------------------------------------


def test_only_apply_rows_are_eligible(client):
    seed(client, decision="Skip")
    assert db.pending_tracker(client) == []


def test_undecided_rows_are_not_eligible(client):
    seed(client, decision=None)
    assert db.pending_tracker(client) == []


def test_already_tracked_rows_are_not_eligible(client):
    seed(client, tracked=1)
    assert db.pending_tracker(client) == []


def test_append_marks_tracked_and_a_rerun_is_a_no_op(config, client, ok_stub):
    seed(client, n=2)
    first = tracker.run_tracker(config)
    assert (first.eligible, first.appended, first.failed) == (2, 2, 0)
    assert tracked_flags(client) == [1, 1]

    second = tracker.run_tracker(config)
    assert (second.eligible, second.appended) == (0, 0)
    assert len(ok_stub.read_text().splitlines()) == 2  # no second append


def test_append_sends_the_prd_gws_invocation(config, client, ok_stub):
    seed(client)
    tracker.run_tracker(config)
    argv = json.loads(ok_stub.read_text().splitlines()[0])
    assert argv[:5] == ["sheets", "spreadsheets", "values", "append", "--params"]
    params = json.loads(argv[5])
    assert params == {
        "spreadsheetId": "sheet-under-test",
        "range": "Applications!A:G",
        "valueInputOption": "USER_ENTERED",
        "insertDataOption": "INSERT_ROWS",
    }
    assert len(json.loads(argv[7])["values"][0]) == 7


def test_id_filter_still_enforces_eligibility(config, client, ok_stub):
    ids = seed(client, n=2)
    db.mark_tracked(client, ids[0])
    summary = tracker.run_tracker(config, posting_id=ids[0])
    assert summary.eligible == 0
    assert not ok_stub.exists()


def test_dry_run_writes_nothing(config, client, ok_stub, capsys):
    seed(client)
    summary = tracker.run_tracker(config, dry_run=True)
    assert (summary.eligible, summary.appended) == (1, 0)
    assert tracked_flags(client) == [0]
    assert not ok_stub.exists()
    assert "Applications!A:G" in capsys.readouterr().out


# --- failure handling ------------------------------------------------------


@pytest.mark.parametrize(
    "stub_body",
    [
        "sys.exit(2)",  # gws itself failed (e.g. expired OAuth token)
        'print("not json")',  # unparseable output
        'print(json.dumps({"updates": {"updatedRows": 0}}))',  # API appended nothing
        'print(json.dumps({"error": {"code": 403}}))',  # error payload, exit 0
    ],
)
def test_an_unconfirmed_append_never_sets_added_to_tracker(config, client, tmp_path, stub_body):
    # added_to_tracker is the only thing standing between a posting and being
    # silently dropped from the backlog, so ambiguity must not set it.
    _write_stub(tmp_path, stub_body)
    seed(client)
    summary = tracker.run_tracker(config)
    assert (summary.appended, summary.failed) == (0, 1)
    assert tracked_flags(client) == [0]


def test_an_auth_failure_is_reported_as_a_re_login_not_a_raw_invalid_grant(
    config, client, tmp_path, capsys
):
    # gws exit 2 means the OAuth grant lapsed (Testing-status clients expire
    # refresh tokens weekly); the message has to say what to do about it.
    _write_stub(tmp_path, 'sys.stderr.write("invalid_grant: Bad Request"); sys.exit(2)')
    seed(client)
    summary = tracker.run_tracker(config)
    assert (summary.appended, summary.failed) == (0, 1)
    assert tracked_flags(client) == [0]
    out = capsys.readouterr().out
    assert "gws auth login" in out
    assert "invalid_grant" in out  # the underlying detail is still shown


def test_one_bad_row_does_not_strand_the_rest(config, client, tmp_path):
    # Fail only the posting whose title contains "Company1".
    _write_stub(
        tmp_path,
        'sys.exit(2) if "Acme1" in " ".join(sys.argv) '
        'else print(json.dumps({"updates": {"updatedRows": 1}}))',
    )
    seed(client, n=3)
    summary = tracker.run_tracker(config)
    assert (summary.eligible, summary.appended, summary.failed) == (3, 2, 1)
    assert tracked_flags(client) == [1, 0, 1]


def test_missing_gws_binary_raises_before_touching_any_row(config, client, monkeypatch):
    monkeypatch.setenv("JSA_GWS_BIN", "definitely-not-a-real-binary")
    seed(client)
    with pytest.raises(tracker.TrackerError, match="not found on PATH"):
        tracker.run_tracker(config)
    assert tracked_flags(client) == [0]
