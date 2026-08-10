"""The `search_agent` CHECK widening that lets a hand-added row exist.

A database created before the direct job-add path constrains ``search_agent``
to the two search agents; SQLite cannot ALTER a CHECK, so ``init_db`` rebuilds
the table. These tests pin the two things that matter: existing rows survive
intact, and the migration is a no-op the second time.
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


@pytest.fixture
def legacy_client(config):
    """A connection whose `postings` table predates the manual-add migration."""
    conn = db.connect(config)
    conn.execute(_LEGACY_SCHEMA)
    yield conn
    conn.close()


def test_legacy_schema_rejects_manual_before_migrating(legacy_client):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_posting(legacy_client, make_posting(search_agent="manual"))


def test_migration_preserves_rows_and_admits_manual(legacy_client):
    kept = db.insert_posting(legacy_client, make_posting(search_agent="perplexity"))
    db.record_decision(legacy_client, kept, "Apply", "great fit")

    assert db.migrate_postings_schema(legacy_client) is True

    row = legacy_client.execute(
        "SELECT id, company, decision, fit_feedback, search_agent FROM postings"
    ).fetchall()
    assert row == [(kept, "Acme", "Apply", "great fit", "perplexity")]

    manual_id = db.insert_posting(
        legacy_client,
        make_posting(
            search_agent="manual",
            canonical_url="https://jobs.lever.co/acme/abc123def456",
            url="https://jobs.lever.co/acme/abc123def456",
        ),
    )
    assert manual_id is not None and manual_id != kept


def test_migration_is_idempotent(legacy_client):
    assert db.migrate_postings_schema(legacy_client) is True
    assert db.migrate_postings_schema(legacy_client) is False


def test_unique_constraint_survives_the_rebuild(legacy_client):
    db.insert_posting(legacy_client, make_posting())
    db.migrate_postings_schema(legacy_client)
    # The idempotency mechanism is the UNIQUE canonical_url; a rebuilt table
    # that lost it would silently start duplicating every re-surfaced posting.
    assert db.insert_posting(legacy_client, make_posting(company="Acme Again")) is None


def test_recovers_a_migration_interrupted_between_the_two_renames(legacy_client):
    # The one window where no table is named `postings`. init_db must finish the
    # swap rather than create an empty table on top and orphan the real rows.
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
