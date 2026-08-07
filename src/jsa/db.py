"""The single Turso ``postings`` table: schema, idempotent insert, and queries.

One hosted libSQL database backs both the headless cloud cron (Steps 1–2) and
the local review session (Step 3); this module is the only place that touches
it. The table holds search output, the canonical-URL idempotency key, the
full job description, and the user's fit feedback — deliberately no application
state (that lives write-only in the Google Sheet, per the PRD).

Connections go through ``turso_serverless``, Turso's pure-Python DB-API 2.0
driver that speaks Hrana over HTTP (Turso's managed platform serves HTTP only;
it does not offer the WebSocket transport the older ``libsql-client`` forced).
A ``file:`` URL instead opens a local SQLite file via stdlib ``sqlite3`` for
throwaway dev — both are DB-API 2.0, so the rest of this module is transport-
agnostic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import turso_serverless

from .config import Config
from .naming import slugify_title

# Either backend is a DB-API 2.0 connection; callers treat them identically.
Connection = sqlite3.Connection | turso_serverless.Connection

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

# Append-only A/B search telemetry. postings stores each req exactly once (the
# idempotent insert no-ops re-encounters), which hides *which* agents also found
# a req — the overlap an A/B comparison most wants. search_findings records one
# row per (run_date, agent, canonical_url) for EVERY posting an agent returns,
# whether or not the postings insert no-ops, so both agents get credit for a
# shared find. This is search telemetry, not application state.
_FINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_findings (
    run_date      TEXT    NOT NULL,
    agent         TEXT    NOT NULL CHECK (agent IN ('claude', 'perplexity')),
    canonical_url TEXT    NOT NULL,
    window_hours  INTEGER NOT NULL,
    rank          INTEGER,
    found_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_date, agent, canonical_url)
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


def connect(config: Config) -> Connection:
    """Open a DB-API 2.0 connection with per-statement autocommit.

    A ``file:`` URL (local dev) opens a local SQLite file with no auth token; a
    hosted Turso URL (``libsql://…``) connects over HTTP with the token from
    config. Autocommit is enabled on both so every ``execute`` persists on its
    own — the review loop relies on each decision being committed immediately,
    and ``turso_serverless`` otherwise defaults to a deferred transaction that
    would silently drop uncommitted writes.
    """
    if config.turso_database_url.startswith("file:"):
        return sqlite3.connect(config.turso_database_url, uri=True, autocommit=True)
    client = turso_serverless.connect(
        url=config.turso_database_url,
        auth_token=config.turso_auth_token,
    )
    # turso_serverless mirrors stdlib sqlite3's *legacy* transaction model:
    # isolation_level=None is what actually enables autocommit. The DB-API
    # `autocommit` attribute it also exposes is merely stored (`_autocommit_mode`)
    # and never consulted by execute/commit, so setting it is a no-op — every DML
    # would open an implicit BEGIN DEFERRED that close() rolls back, silently
    # dropping the write. Setting isolation_level=None is what the review loop
    # (and the pipeline insert) rely on to persist each statement immediately.
    client.isolation_level = None
    return client


def init_db(client: Connection) -> None:
    """Create the ``postings`` and ``search_findings`` tables if absent."""
    client.execute(_SCHEMA)
    client.execute(_FINDINGS_SCHEMA)


def insert_posting(client: Connection, posting: NewPosting) -> int | None:
    """Idempotently insert a posting; return its new id, or None if it existed.

    Uses ``INSERT … ON CONFLICT(canonical_url) DO NOTHING RETURNING id``: a
    re-encountered posting conflicts on the UNIQUE canonical_url and yields no
    row, so the caller can tell genuinely-new rows (which need a full-JD fetch)
    from re-surfaced ones.
    """
    cursor = client.execute(
        """
        INSERT INTO postings (
            company, title, url, date_posted, canonical_url,
            normalized_company, title_slug, search_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_url) DO NOTHING
        RETURNING id
        """,
        (
            posting.company,
            posting.title,
            posting.url,
            posting.date_posted,
            posting.canonical_url,
            posting.normalized_company,
            posting.title_slug,
            posting.search_agent,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return int(row[0])


def update_jd_capture(
    client: Connection,
    posting_id: int,
    *,
    jd_markdown: str | None,
    location: str | None,
    title: str | None,
) -> None:
    """Store the full-JD fetch results on a row.

    On a successful ATS detail fetch the canonical ``title`` replaces the search
    agent's transcription (the two agents transcribe titles inconsistently), and
    ``title_slug`` is re-derived from it so the Step 4 application-packet naming
    carries the canonical title too (per prd.md). A ``None`` title leaves both
    the existing title and slug untouched.
    """
    if title:
        client.execute(
            "UPDATE postings SET jd_markdown = ?, location = ?, title = ?, title_slug = ? "
            "WHERE id = ?",
            (jd_markdown, location, title, slugify_title(title), posting_id),
        )
    else:
        client.execute(
            "UPDATE postings SET jd_markdown = ?, location = ? WHERE id = ?",
            (jd_markdown, location, posting_id),
        )


def pending_review(client: Connection) -> list[tuple]:
    """Return rows awaiting a fit decision, oldest first (the Step 3 backlog)."""
    cursor = client.execute(
        """
        SELECT id, company, title, url, location, date_posted
        FROM postings
        WHERE decision IS NULL
        ORDER BY first_seen_at ASC, id ASC
        """
    )
    return list(cursor.fetchall())


def record_decision(
    client: Connection,
    posting_id: int,
    decision: str,
    fit_feedback: str | None,
) -> None:
    """Write a user's ``Apply``/``Skip`` decision and optional feedback."""
    client.execute(
        "UPDATE postings SET decision = ?, fit_feedback = ? WHERE id = ?",
        (decision, fit_feedback, posting_id),
    )


def record_finding(
    client: Connection,
    *,
    run_date: str,
    agent: str,
    canonical_url: str,
    window_hours: int,
    rank: int | None = None,
) -> None:
    """Log that ``agent`` surfaced ``canonical_url`` in the run for ``run_date``.

    Written for every posting an agent returns — including ones the idempotent
    ``postings`` insert no-ops — so the A/B overlap between agents is
    measurable. ``ON CONFLICT DO NOTHING`` keeps a re-run of the same day+agent
    from duplicating findings.
    """
    client.execute(
        """
        INSERT INTO search_findings (run_date, agent, canonical_url, window_hours, rank)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(run_date, agent, canonical_url) DO NOTHING
        """,
        (run_date, agent, canonical_url, window_hours, rank),
    )


def ab_report(client: Connection) -> dict:
    """Compute the A/B comparison from ``search_findings`` joined to ``postings``.

    Attribution lives in the append-only ``search_findings`` log, not the
    ``postings`` row's ``search_agent`` (which records only the first inserter),
    so a req both agents found credits both. Returns coverage (distinct reqs per
    agent), overlap (both / claude-only / perplexity-only), and per-agent Apply
    precision (the single ``postings.decision`` fanned out to every finder).
    """
    coverage = client.execute(
        "SELECT agent, COUNT(DISTINCT canonical_url) "
        "FROM search_findings GROUP BY agent ORDER BY agent"
    ).fetchall()
    overlap = client.execute(
        """
        SELECT
          SUM(CASE WHEN c = 1 AND p = 1 THEN 1 ELSE 0 END),
          SUM(CASE WHEN c = 1 AND p = 0 THEN 1 ELSE 0 END),
          SUM(CASE WHEN p = 1 AND c = 0 THEN 1 ELSE 0 END)
        FROM (
          SELECT canonical_url,
            MAX(CASE WHEN agent = 'claude' THEN 1 ELSE 0 END) AS c,
            MAX(CASE WHEN agent = 'perplexity' THEN 1 ELSE 0 END) AS p
          FROM search_findings GROUP BY canonical_url
        )
        """
    ).fetchone()
    precision = client.execute(
        """
        SELECT f.agent,
          SUM(CASE WHEN pst.decision = 'Apply' THEN 1 ELSE 0 END) AS applies,
          SUM(CASE WHEN pst.decision IN ('Apply', 'Skip') THEN 1 ELSE 0 END) AS decided
        FROM search_findings f
        JOIN postings pst ON pst.canonical_url = f.canonical_url
        GROUP BY f.agent ORDER BY f.agent
        """
    ).fetchall()
    return {"coverage": coverage, "overlap": overlap, "precision": precision}
