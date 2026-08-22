"""Schema migrations: the `search_agent` CHECK widening and `decided_at`.

A database created before the direct job-add path constrains ``search_agent``
to the two search agents; SQLite cannot ALTER a CHECK, so ``init_db`` rebuilds
the table. A database from before the prompt-refinement loop lacks
``decided_at``, added with a plain ALTER — which must run *before* the
rebuild, whose named copy list includes the column. These tests pin the
things that matter: existing rows survive intact, pre-migration decisions
keep a NULL ``decided_at`` (= already incorporated by the manual refinement
rounds), and the migration is a no-op the second time.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import make_posting

from jsa import db

# The pre-migration schema, verbatim from the shipped build.
_LEGACY_SCHEMA = """
CREATE TABLE postings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    company            TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    url                TEXT    NOT NULL,
    date_posted        TEXT,
    canonical_url      TEXT    NOT NULL UNIQUE,
    normalized_company TEXT    NOT NULL,
    title_slug         TEXT    NOT NULL,
    jd_markdown        TEXT,
    location           TEXT,
    search_agent       TEXT    NOT NULL CHECK (search_agent IN ('claude', 'perplexity')),
    first_seen_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    decision           TEXT    CHECK (decision IN ('Apply', 'Skip')),
    fit_feedback       TEXT,
    added_to_tracker   INTEGER NOT NULL DEFAULT 0
)
"""


# The post-manual, pre-decided_at schema (the 2026-08-11 → 2026-08-21 era).
_MANUAL_ERA_SCHEMA = _LEGACY_SCHEMA.replace(
    "IN ('claude', 'perplexity')", "IN ('claude', 'perplexity', 'manual')"
)


@pytest.fixture
def legacy_client(config):
    """A connection whose `postings` table predates the manual-add migration."""
    conn = db.connect(config)
    conn.execute(_LEGACY_SCHEMA)
    yield conn
    conn.close()


def _raw_insert(client, *, search_agent="perplexity", decision=None, fit_feedback=None, n=0):
    """Insert directly, bypassing insert_posting (whose SQL needs decided_at)."""
    url = f"https://job-boards.greenhouse.io/acme/jobs/{100 + n}"
    client.execute(
        "INSERT INTO postings (company, title, url, canonical_url, normalized_company, "
        "title_slug, search_agent, decision, fit_feedback) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Acme",
            "Customer Enablement Lead",
            url,
            url,
            "Acme",
            "Customer Enablement Lead",
            search_agent,
            decision,
            fit_feedback,
        ),
    )


def test_legacy_schema_rejects_manual_before_migrating(legacy_client):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(legacy_client, search_agent="manual")


def test_migration_preserves_rows_and_admits_manual(legacy_client):
    _raw_insert(legacy_client, decision="Apply", fit_feedback="great fit")

    assert db.migrate_postings_schema(legacy_client) is True

    rows = legacy_client.execute(
        "SELECT company, decision, fit_feedback, search_agent, decided_at FROM postings"
    ).fetchall()
    # decided_at NULL = a pre-column decision, already incorporated by the
    # manual refinement rounds; the refinement scope must never see it.
    assert rows == [("Acme", "Apply", "great fit", "perplexity", None)]

    manual_id = db.insert_posting(
        legacy_client,
        make_posting(
            search_agent="manual",
            canonical_url="https://jobs.lever.co/acme/abc123def456",
            url="https://jobs.lever.co/acme/abc123def456",
        ),
    )
    assert manual_id is not None


def test_a_manual_era_schema_gains_decided_at_without_a_rebuild(config):
    conn = db.connect(config)
    conn.execute(_MANUAL_ERA_SCHEMA)
    _raw_insert(conn, search_agent="manual", decision="Apply")
    assert db.migrate_postings_schema(conn) is True
    assert conn.execute("SELECT decided_at FROM postings").fetchall() == [(None,)]
    assert db.migrate_postings_schema(conn) is False
    conn.close()


def test_migration_is_idempotent(legacy_client):
    assert db.migrate_postings_schema(legacy_client) is True
    assert db.migrate_postings_schema(legacy_client) is False


def test_unique_constraint_survives_the_rebuild(legacy_client):
    db.migrate_postings_schema(legacy_client)
    db.insert_posting(legacy_client, make_posting())
    # The idempotency mechanism is the UNIQUE canonical_url; a rebuilt table
    # that lost it would silently start duplicating every re-surfaced posting.
    assert db.insert_posting(legacy_client, make_posting(company="Acme Again")) is None


def test_recovers_a_migration_interrupted_between_the_two_renames(legacy_client):
    # The one window where no table is named `postings`. init_db must finish the
    # swap rather than create an empty table on top and orphan the real rows.
    # The real sequence ALTERs decided_at in before the rebuild, so the
    # simulated interrupted state includes it.
    legacy_client.execute("ALTER TABLE postings ADD COLUMN decided_at TEXT")
    kept = db.insert_posting(legacy_client, make_posting())
    legacy_client.execute(f"CREATE TABLE postings_migrated ({db._POSTINGS_COLUMNS})")
    legacy_client.execute(
        f"INSERT INTO postings_migrated ({db._POSTINGS_COLUMN_NAMES}) "
        f"SELECT {db._POSTINGS_COLUMN_NAMES} FROM postings"
    )
    legacy_client.execute("ALTER TABLE postings RENAME TO postings_pre_manual")

    db.init_db(legacy_client)

    rows = legacy_client.execute("SELECT id, company FROM postings").fetchall()
    assert rows == [(kept, "Acme")]
    assert "postings_pre_manual" not in db._table_names(legacy_client)
    assert (
        db.insert_posting(
            legacy_client,
            make_posting(
                search_agent="manual",
                url="https://jobs.lever.co/acme/abc123def456",
                canonical_url="https://jobs.lever.co/acme/abc123def456",
            ),
        )
        is not None
    )


def test_recovers_a_migration_interrupted_before_the_final_drop(legacy_client):
    legacy_client.execute("ALTER TABLE postings ADD COLUMN decided_at TEXT")
    db.insert_posting(legacy_client, make_posting())
    legacy_client.execute("ALTER TABLE postings RENAME TO postings_pre_manual")
    legacy_client.execute(f"CREATE TABLE postings ({db._POSTINGS_COLUMNS})")
    db.init_db(legacy_client)
    assert "postings_pre_manual" not in db._table_names(legacy_client)


def test_init_db_applies_the_migration(config):
    conn = db.connect(config)
    conn.execute(_LEGACY_SCHEMA)
    db.init_db(conn)
    assert db.insert_posting(conn, make_posting(search_agent="manual")) is not None
    conn.close()


# --- decided_at: the refinement loop's review date --------------------------


def test_a_searched_insert_leaves_decided_at_null(client):
    posting_id = db.insert_posting(client, make_posting())
    row = client.execute("SELECT decided_at FROM postings WHERE id = ?", (posting_id,)).fetchone()
    assert row == (None,)


def test_a_manual_add_is_decided_at_insert(client):
    # Apply-on-arrival is written in the INSERT itself, so the review date is
    # the insert — the row must enter the refinement scope immediately.
    posting_id = db.insert_posting(client, make_posting(search_agent="manual", decision="Apply"))
    row = client.execute("SELECT decided_at FROM postings WHERE id = ?", (posting_id,)).fetchone()
    assert row[0] is not None


def test_record_decision_stamps_decided_at(client):
    posting_id = db.insert_posting(client, make_posting())
    db.record_decision(client, posting_id, "Skip", "not a fit")
    row = client.execute("SELECT decided_at FROM postings WHERE id = ?", (posting_id,)).fetchone()
    assert row[0] is not None


def test_set_decision_refreshes_decided_at(client):
    # A promotion (jsa add on an existing row) is a new decision: the row
    # re-enters the refinement scope.
    posting_id = db.insert_posting(client, make_posting())
    db.record_decision(client, posting_id, "Skip", None)
    client.execute(
        "UPDATE postings SET decided_at = '2020-01-01 00:00:00' WHERE id = ?", (posting_id,)
    )
    db.set_decision(client, posting_id, "Apply")
    row = client.execute("SELECT decided_at FROM postings WHERE id = ?", (posting_id,)).fetchone()
    assert row[0] != "2020-01-01 00:00:00"
