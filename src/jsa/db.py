"""The single Turso ``postings`` table: schema, idempotent insert, and queries.

One hosted libSQL database backs both the headless cloud cron (Steps 1–2) and
the local review session (Step 3); this module is the only place that touches
it. The table holds search output, the canonical-URL idempotency key, the
full job description, and the user's fit feedback — deliberately no application
state (that lives in the Google Sheet tracker's user columns, per the PRD).

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
#
# Held as a bare column list (rather than a whole CREATE statement) because the
# migration below rebuilds the table from the same text — the live schema and
# the migration target can therefore never drift apart.
_POSTINGS_COLUMNS = """
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
    search_agent       TEXT    NOT NULL CHECK (search_agent IN ('claude', 'perplexity', 'manual')),
    first_seen_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    decision           TEXT    CHECK (decision IN ('Apply', 'Skip')),
    fit_feedback       TEXT,
    added_to_tracker   INTEGER NOT NULL DEFAULT 0
"""

# The same columns as an ordered name list, so the migration's INSERT … SELECT
# copies by name rather than relying on positional alignment.
_POSTINGS_COLUMN_NAMES = (
    "id, company, title, url, date_posted, canonical_url, normalized_company, "
    "title_slug, jd_markdown, location, search_agent, first_seen_at, decision, "
    "fit_feedback, added_to_tracker"
)

_SCHEMA = f"CREATE TABLE IF NOT EXISTS postings ({_POSTINGS_COLUMNS})"

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
    # Set only by the direct job-add path, where supplying the URL *is* the
    # Apply decision. The pipeline leaves it None so searched postings enter the
    # Step 3 backlog. Writing it in the INSERT rather than a follow-up UPDATE
    # means the row is never briefly visible as undecided.
    decision: str | None = None


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
    """Create the ``postings`` and ``search_findings`` tables, then migrate.

    Every command that touches the database calls this, so the migration below
    is applied the first time an existing database is opened by a build that
    needs it — there is no separate migrate step to forget.
    """
    # Migrate FIRST. The rebuild below briefly parks the data under another
    # table name, and a CREATE TABLE IF NOT EXISTS run before the recovery step
    # would fill that window with a shiny empty `postings`, orphaning the real
    # rows. Migrating first means the create only ever fires on a truly new DB.
    migrate_postings_schema(client)
    client.execute(_SCHEMA)
    client.execute(_FINDINGS_SCHEMA)


def migrate_postings_schema(client: Connection) -> bool:
    """Widen the legacy ``search_agent`` CHECK to admit ``'manual'``.

    Databases created before the direct job-add path constrain ``search_agent``
    to the two search agents, which rejects a hand-added row. SQLite cannot
    ALTER a CHECK constraint, so this performs the standard table rebuild:
    create the new table, copy, swap names, drop the old one.

    On hosted Turso each statement is a separate autocommitted round-trip, so
    the rebuild is *not* atomic: a failure between the two renames would leave
    the data intact but parked under ``postings_migrated``. Rather than depend
    on transaction semantics the HTTP transport may not offer, the sequence is
    ordered so no step can lose data and ``_recover_interrupted_migration``
    finishes any half-done swap on the next run. Returns True if the rebuild
    ran, False if the schema was already current (the common case, costing one
    ``sqlite_master`` read).
    """
    _recover_interrupted_migration(client)
    row = client.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'postings'"
    ).fetchone()
    if row is None or "'manual'" in (row[0] or ""):
        return False
    client.execute("DROP TABLE IF EXISTS postings_migrated")
    client.execute(f"CREATE TABLE postings_migrated ({_POSTINGS_COLUMNS})")
    client.execute(
        f"INSERT INTO postings_migrated ({_POSTINGS_COLUMN_NAMES}) "
        f"SELECT {_POSTINGS_COLUMN_NAMES} FROM postings"
    )
    client.execute("ALTER TABLE postings RENAME TO postings_pre_manual")
    client.execute("ALTER TABLE postings_migrated RENAME TO postings")
    client.execute("DROP TABLE postings_pre_manual")
    return True


def _table_names(client: Connection) -> set[str]:
    return {r[0] for r in client.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _recover_interrupted_migration(client: Connection) -> None:
    """Finish a rebuild that died between statements. Idempotent and cheap.

    Two recoverable states, matching the two gaps in the sequence above:
    ``postings`` renamed away but its replacement not yet renamed in (the only
    window where the table is missing entirely), and the superseded copy left
    undropped. Neither is reachable on a healthy run — this exists so a dropped
    connection mid-migration is self-healing rather than a manual repair.
    """
    names = _table_names(client)
    if "postings" not in names and "postings_migrated" in names:
        client.execute("ALTER TABLE postings_migrated RENAME TO postings")
        names = _table_names(client)
    if "postings" in names and "postings_pre_manual" in names:
        client.execute("DROP TABLE postings_pre_manual")


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
            normalized_company, title_slug, search_agent, decision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            posting.decision,
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


def find_by_canonical_url(client: Connection, canonical_url: str) -> tuple | None:
    """Return an existing row for ``canonical_url``, or None.

    Only a UX affordance for the manual-add path (so it can report the existing
    row instead of silently no-opping). It is *not* the idempotency mechanism —
    that remains the UNIQUE constraint on the insert, which also closes the race
    this read cannot.
    """
    cursor = client.execute(
        "SELECT id, company, title, url, decision, added_to_tracker "
        "FROM postings WHERE canonical_url = ?",
        (canonical_url,),
    )
    return cursor.fetchone()


def pending_tracker(client: Connection, posting_id: int | None = None) -> list[tuple]:
    """Return ``Apply`` rows not yet written to the application tracker (Step 5).

    ``added_to_tracker`` is the completion signal, so this query *is* the
    idempotency guard for the tracker write: an already-appended row drops out
    of the backlog and can never be double-appended. Passing ``posting_id``
    narrows to one row while keeping both eligibility conditions.
    """
    sql = (
        "SELECT id, normalized_company, title, url, date_posted "
        "FROM postings WHERE decision = 'Apply' AND added_to_tracker = 0"
    )
    params: tuple = ()
    if posting_id is not None:
        sql += " AND id = ?"
        params = (posting_id,)
    cursor = client.execute(sql + " ORDER BY first_seen_at ASC, id ASC", params)
    return list(cursor.fetchall())


def pending_packets(client: Connection, posting_id: int | None = None) -> list[tuple]:
    """Return ``Apply`` rows awaiting an application-packet directory (Step 4).

    The default queue mirrors prd.md's Step 4 eligibility: ``decision = 'Apply'
    AND added_to_tracker = 0``. Passing ``posting_id`` drops the tracker
    condition -- but never the Apply one: while the interim track-on-Apply
    trigger runs ``jsa track`` ahead of packet creation, most Apply rows are
    tracked before any packet exists, and the explicit id is how a packet is
    still built for one of those.
    """
    sql = (
        "SELECT id, normalized_company, title_slug, company, title, jd_markdown "
        "FROM postings WHERE decision = 'Apply'"
    )
    params: tuple = ()
    if posting_id is not None:
        sql += " AND id = ?"
        params = (posting_id,)
    else:
        sql += " AND added_to_tracker = 0"
    cursor = client.execute(sql + " ORDER BY first_seen_at ASC, id ASC", params)
    return list(cursor.fetchall())


def rows_for_refetch(
    client: Connection,
    posting_id: int | None = None,
    *,
    include_all: bool = False,
) -> list[tuple]:
    """Return rows whose ATS record should be re-read.

    Defaults to ``Apply`` rows -- the ones where upstream drift can still change
    what happens next (the packet naming and a pending or not-yet-applied
    tracker row are built from them). The caller narrows further against the
    tracker Sheet's Date Applied column, which the database deliberately does
    not mirror. ``include_all`` widens to every stored row regardless of
    decision or tracker state; ``posting_id`` targets one row unconditionally.
    """
    sql = (
        "SELECT id, url, title, location, added_to_tracker, "
        "normalized_company, title_slug, jd_markdown FROM postings"
    )
    params: tuple = ()
    if posting_id is not None:
        sql += " WHERE id = ?"
        params = (posting_id,)
    elif not include_all:
        sql += " WHERE decision = 'Apply'"
    return list(client.execute(sql + " ORDER BY id ASC", params).fetchall())


def mark_tracked(client: Connection, posting_id: int) -> None:
    """Flag a row as written to the application tracker (Step 5's tail)."""
    client.execute("UPDATE postings SET added_to_tracker = 1 WHERE id = ?", (posting_id,))


def set_decision(client: Connection, posting_id: int, decision: str) -> None:
    """Set only the ``decision`` column, leaving ``fit_feedback`` untouched.

    Distinct from ``record_decision`` (which writes both) because the direct
    job-add path may upgrade a row the user already reviewed: the note they
    wrote about it is still worth keeping, and is still ground-truth material.
    """
    client.execute("UPDATE postings SET decision = ? WHERE id = ?", (decision, posting_id))


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
