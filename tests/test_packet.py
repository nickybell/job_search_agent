"""Steps 1-2 of the Step 4 packet: directory creation and job_posting.md.

Per prd.md (Resume Revisions, revised 2026-08-21), Step 4's completion guard
is added_to_tracker; on this standalone command the fail-if-exists mkdir is
kept as a cheap safety, so the load-bearing claims here are that an existing
directory -- even a half-built one -- is never clobbered (jsa generate, not
this command, is what re-enters one), and that a missing JD degrades to a
directory without a job_posting.md rather than a failure.
"""

from __future__ import annotations

import pytest
from conftest import make_posting

from jsa import db, packet


@pytest.fixture(autouse=True)
def packets_root(tmp_path, monkeypatch):
    """Point JSA_PACKETS_DIR at a tmp dir so nothing touches ~/Documents."""
    root = tmp_path / "Job Applications"
    monkeypatch.setenv("JSA_PACKETS_DIR", str(root))
    return root


def seed(client, *, decision="Apply", tracked=0, jd="# The JD", n=0) -> int:
    # n varies the URL so multiple seeds don't collide on canonical_url.
    posting_id = db.insert_posting(
        client,
        make_posting(
            company=f"Acme{n or ''}",
            normalized_company=f"Acme{n or ''}",
            url=f"https://job-boards.greenhouse.io/acme/jobs/{100 + n}",
            canonical_url=f"https://job-boards.greenhouse.io/acme/jobs/{100 + n}",
        ),
    )
    if jd is not None:
        db.update_jd_capture(client, posting_id, jd_markdown=jd, location="Remote", title=None)
    if decision:
        db.record_decision(client, posting_id, decision, None)
    if tracked:
        db.mark_tracked(client, posting_id)
    return posting_id


# --- eligibility (db.pending_packets) --------------------------------------


def test_default_queue_is_apply_and_untracked(client):
    eligible = seed(client)
    seed(client, decision="Skip", n=1)
    seed(client, decision=None, n=2)
    seed(client, tracked=1, n=3)
    assert [r[0] for r in db.pending_packets(client)] == [eligible]


def test_id_reaches_a_tracked_row_but_never_a_non_apply_one(client):
    # The interim track-on-Apply trigger tracks rows before packets exist, so
    # --id must get past added_to_tracker; the Apply condition still holds.
    tracked_id = seed(client, tracked=1)
    skip_id = seed(client, decision="Skip", n=1)
    assert [r[0] for r in db.pending_packets(client, tracked_id)] == [tracked_id]
    assert db.pending_packets(client, skip_id) == []


# --- filesystem behaviour --------------------------------------------------


def test_creates_the_directory_and_writes_the_jd(config, client, packets_root):
    seed(client)
    summary, results = packet.run_packet(config)
    assert (summary.eligible, summary.created) == (1, 1)
    path = packets_root / "Acme - Customer Enablement Lead"
    assert results[0].path == path
    assert (path / "job_posting.md").read_text() == "# The JD\n"


def test_an_existing_directory_is_skipped_and_never_clobbered(config, client, packets_root):
    # The standalone-path safety: a packet already worked on (say, a tailored
    # resume refined by hand) must survive a re-run of jsa packet untouched.
    # (jsa generate deliberately does not inherit this skip.)
    seed(client)
    existing = packets_root / "Acme - Customer Enablement Lead"
    existing.mkdir(parents=True)
    (existing / "draft.txt").write_text("half-built packet")

    summary, results = packet.run_packet(config)

    assert (summary.created, summary.skipped_existing) == (0, 1)
    assert results[0].status == "exists"
    assert (existing / "draft.txt").read_text() == "half-built packet"
    assert not (existing / "job_posting.md").exists()


def test_a_missing_jd_still_gets_a_directory(config, client, packets_root):
    # Same degrade-don't-destroy stance as the pipeline: a NULL jd_markdown
    # (failed capture) must not block the packet directory.
    seed(client, jd=None)
    summary, results = packet.run_packet(config)
    assert (summary.created, summary.missing_jd) == (1, 1)
    assert results[0].path.is_dir()
    assert not (results[0].path / "job_posting.md").exists()


def test_dry_run_touches_nothing(config, client, packets_root):
    seed(client)
    summary, results = packet.run_packet(config, dry_run=True)
    assert summary.eligible == 1
    assert results[0].status == "would_create"
    assert not packets_root.exists()
