"""The prompt-refinement loop: scoping, bookkeeping, and rendering.

NOTE: agent-authored and awaiting Nicky's review (the standing rule in
TODO.md).

Hermetic: a throwaway ``file:`` SQLite DB, a stub runner in place of the
refiner agent, and a tmp-path PR-body target — the real agent and GitHub
never enter (the git-diff helper is exercised only as a read-only baseline).
"""

from __future__ import annotations

import pytest
from conftest import make_posting

from jsa import db, refine


def seed_decided(
    client,
    *,
    decision="Skip",
    feedback=None,
    jd="We build ABM software for revenue teams.",
    agent="perplexity",
    decided_at=None,
    n=0,
) -> int:
    posting_id = db.insert_posting(
        client,
        make_posting(
            company=f"Acme{n or ''}",
            url=f"https://job-boards.greenhouse.io/acme/jobs/{100 + n}",
            canonical_url=f"https://job-boards.greenhouse.io/acme/jobs/{100 + n}",
            search_agent=agent,
        ),
    )
    if jd is not None:
        db.update_jd_capture(client, posting_id, jd_markdown=jd, location="Remote", title=None)
    db.record_decision(client, posting_id, decision, feedback)
    if decided_at is not None:
        client.execute("UPDATE postings SET decided_at = ? WHERE id = ?", (decided_at, posting_id))
    return posting_id


def record_run(client, run_at: str) -> None:
    client.execute(
        "INSERT INTO prompt_refinement_runs (run_at, considered, changed) VALUES (?, 1, 0)",
        (run_at,),
    )


# --- scope ------------------------------------------------------------------


def test_scope_excludes_pre_column_decisions(client):
    # NULL decided_at = decided before the column existed, already
    # incorporated by the manual refinement rounds.
    legacy = seed_decided(client, n=0)
    client.execute("UPDATE postings SET decided_at = NULL WHERE id = ?", (legacy,))
    fresh = seed_decided(client, n=1)

    rows = db.rows_for_refinement(client, None)

    assert [row[0] for row in rows] == [fresh]


def test_cutoff_scopes_to_rows_decided_after_the_last_run(client):
    seed_decided(client, n=0, decided_at="2026-08-19 00:00:00")
    new = seed_decided(client, n=1, decided_at="2026-08-21 00:00:00")
    record_run(client, "2026-08-20 00:00:00")

    rows = db.rows_for_refinement(client, db.refinement_cutoff(client))

    assert [row[0] for row in rows] == [new]


def test_a_redecided_row_reenters_scope(client):
    posting_id = seed_decided(client, decided_at="2026-08-19 00:00:00")
    record_run(client, "2026-08-20 00:00:00")
    assert db.rows_for_refinement(client, db.refinement_cutoff(client)) == []

    db.record_decision(client, posting_id, "Apply", "changed my mind")

    rows = db.rows_for_refinement(client, db.refinement_cutoff(client))
    assert [row[0] for row in rows] == [posting_id]


# --- run_refine -------------------------------------------------------------


def test_run_refine_runs_the_agent_records_the_run_and_writes_the_pr_body(
    config, client, tmp_path, monkeypatch
):
    seed_decided(client, feedback="We should exclude Developer Relations roles")
    calls: list[str] = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        return "## Changelog\nSharpened the DevRel exclusion."

    diffs = iter([[], ["deep_research_prompt.md"]])  # baseline, then after the agent
    monkeypatch.setattr(refine, "_changed_files", lambda: next(diffs))
    body = tmp_path / "body.md"

    summary = refine.run_refine(config, runner=runner, pr_body_path=body)

    assert (summary.considered, summary.ran) == (1, True)
    assert summary.changed_files == ["deep_research_prompt.md"]
    assert body.read_text().startswith("## Changelog")
    # The prompt carried the interpolated ground truth, feedback included.
    assert "exclude Developer Relations" in calls[0]
    assert "{{GROUND_TRUTH}}" not in calls[0] and "{{HISTORY}}" not in calls[0]
    # The run is recorded (the cutoff advances) regardless of any PR outcome.
    assert db.refinement_cutoff(client) is not None


def test_a_dirty_checkout_does_not_count_as_proposed_changes(config, client, tmp_path, monkeypatch):
    seed_decided(client)
    monkeypatch.setattr(refine, "_changed_files", lambda: ["TODO.md"])  # unchanged by the run

    summary = refine.run_refine(
        config, runner=lambda prompt: "nothing to change", pr_body_path=tmp_path / "b.md"
    )

    assert summary.changed_files == []
    assert client.execute("SELECT considered, changed FROM prompt_refinement_runs").fetchall() == [
        (1, 0)
    ]


def test_dry_run_neither_runs_the_agent_nor_records(config, client, capsys):
    posting_id = seed_decided(client)

    def boom(prompt: str) -> str:
        raise AssertionError("the agent must not run under --dry-run")

    summary = refine.run_refine(config, dry_run=True, runner=boom)

    assert (summary.considered, summary.ran) == (1, False)
    assert db.refinement_cutoff(client) is None  # the cutoff must not advance
    assert str(posting_id) in capsys.readouterr().out


def test_no_new_ground_truth_is_a_quiet_noop(config, client):
    def boom(prompt: str) -> str:
        raise AssertionError("the agent must not run with nothing in scope")

    summary = refine.run_refine(config, runner=boom)

    assert summary.considered == 0
    assert db.refinement_cutoff(client) is None


def test_a_failed_agent_records_nothing_so_rows_are_reconsidered(config, client, tmp_path):
    seed_decided(client)

    def boom(prompt: str) -> str:
        raise RuntimeError("agent crashed")

    with pytest.raises(RuntimeError):
        refine.run_refine(config, runner=boom, pr_body_path=tmp_path / "b.md")

    assert db.refinement_cutoff(client) is None  # next run reconsiders the rows


# --- rendering --------------------------------------------------------------


def test_ground_truth_rendering_carries_jd_feedback_and_the_manual_flag(client):
    seed_decided(client, feedback="too much curriculum authoring", n=0)
    db.insert_posting(
        client,
        make_posting(
            company="ByHand",
            url="https://jobs.lever.co/byhand/abc123def456",
            canonical_url="https://jobs.lever.co/byhand/abc123def456",
            search_agent="manual",
            decision="Apply",
        ),
    )

    text = refine.render_ground_truth(db.rows_for_refinement(client, None))

    assert "We build ABM software" in text  # the JD is the pattern-mining input
    assert "too much curriculum authoring" in text
    assert "the search did NOT surface this posting" in text  # the recall flag


def test_history_renders_counts_and_compact_lines_only(client):
    seed_decided(client, decision="Apply", n=0)
    seed_decided(client, decision="Skip", n=1)

    text = refine.render_history(db.decided_history(client))

    assert "1 Apply, 1 Skip" in text
    assert "[Apply] Acme" in text
    assert "We build ABM software" not in text  # history carries no JDs
