"""The single Turso ``postings`` table: schema, idempotent insert, and queries.

One hosted libSQL database backs both the headless cloud cron (Steps 1–2) and
the local review session (Step 3); this module is the only place that touches
it. The table holds search output, the canonical-URL idempotency key, the
full job description, and the user's fit feedback — deliberately no application
state (that lives write-only in the Google Sheet, per the PRD).
"""

from __future__ import annotations

from dataclasses import dataclass

import libsql_client

from .config import Config

# Indicative column set per prd.md "postings columns". decision is nullable
# until Step 3; added_to_tracker/jd_markdown/location fill in later steps.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
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


@dataclass
class NewPosting:
    """Everything Step 2 writes for a freshly discovered posting."""

    company: str
    title: str
    url: str
    date_posted: str | None
    canonical_url: str
    normalized_company: str
    title_slug: str
    search_agent: str


def connect(config: Config) -> libsql_client.ClientSync:
    """Open a synchronous libSQL client.

    A ``file:`` URL (local dev) takes no auth token; a hosted Turso URL
    (``libsql://…``) takes the token from config.
    """
    if config.turso_database_url.startswith("file:"):
        return libsql_client.create_client_sync(config.turso_database_url)
    return libsql_client.create_client_sync(
        config.turso_database_url,
        auth_token=config.turso_auth_token,
    )


def init_db(client: libsql_client.ClientSync) -> None:
    """Create the ``postings`` table if it does not already exist."""
    client.execute(_SCHEMA)


def insert_posting(client: libsql_client.ClientSync, posting: NewPosting) -> int | None:
    """Idempotently insert a posting; return its new id, or None if it existed.

    Uses ``INSERT … ON CONFLICT(canonical_url) DO NOTHING RETURNING id``: a
    re-encountered posting conflicts on the UNIQUE canonical_url and yields no
    row, so the caller can tell genuinely-new rows (which need a full-JD fetch)
    from re-surfaced ones.
    """
    result = client.execute(
        """
        INSERT INTO postings (
            company, title, url, date_posted, canonical_url,
            normalized_company, title_slug, search_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_url) DO NOTHING
        RETURNING id
        """,
        [
            posting.company,
            posting.title,
            posting.url,
            posting.date_posted,
            posting.canonical_url,
            posting.normalized_company,
            posting.title_slug,
            posting.search_agent,
        ],
    )
    if not result.rows:
        return None
    return int(result.rows[0][0])


def update_jd_capture(
    client: libsql_client.ClientSync,
    posting_id: int,
    *,
    jd_markdown: str,
    location: str | None,
    title: str | None,
) -> None:
    """Store the full-JD fetch results on a row.

    On a successful ATS detail fetch the canonical ``title`` replaces the search
    agent's transcription (the two agents transcribe titles inconsistently); a
    ``None`` title leaves the existing value untouched.
    """
    if title:
        client.execute(
            "UPDATE postings SET jd_markdown = ?, location = ?, title = ? WHERE id = ?",
            [jd_markdown, location, title, posting_id],
        )
    else:
        client.execute(
            "UPDATE postings SET jd_markdown = ?, location = ? WHERE id = ?",
            [jd_markdown, location, posting_id],
        )


def pending_review(client: libsql_client.ClientSync) -> list[libsql_client.Row]:
    """Return rows awaiting a fit decision, oldest first (the Step 3 backlog)."""
    result = client.execute(
        """
        SELECT id, company, title, url, location, date_posted
        FROM postings
        WHERE decision IS NULL
        ORDER BY first_seen_at ASC, id ASC
        """
    )
    return list(result.rows)


def record_decision(
    client: libsql_client.ClientSync,
    posting_id: int,
    decision: str,
    fit_feedback: str | None,
) -> None:
    """Write a user's ``Apply``/``Skip`` decision and optional feedback."""
    client.execute(
        "UPDATE postings SET decision = ?, fit_feedback = ? WHERE id = ?",
        [decision, fit_feedback, posting_id],
    )
