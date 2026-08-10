"""Shared fixtures. Nothing here touches the hosted Turso DB or a real Sheet.

The database fixture points ``TURSO_DATABASE_URL`` at a throwaway ``file:``
SQLite database in a tmp dir, which the DB layer already supports for local dev
(both backends are DB-API 2.0, so the module under test is unchanged).
"""

from __future__ import annotations

import pytest

from jsa import db
from jsa.config import Config


@pytest.fixture
def config(tmp_path) -> Config:
    """A Config pointed at a fresh throwaway SQLite file."""
    return Config(
        turso_database_url=f"file:{tmp_path / 'test.db'}",
        turso_auth_token=None,
        anthropic_api_key=None,
        perplexity_api_key=None,
    )


@pytest.fixture
def client(config: Config):
    """An initialized connection to the throwaway database."""
    conn = db.connect(config)
    db.init_db(conn)
    yield conn
    conn.close()


def make_posting(**overrides) -> db.NewPosting:
    """A valid NewPosting with sensible defaults, for tests that need a row."""
    fields = {
        "company": "Acme",
        "title": "Customer Enablement Lead",
        "url": "https://job-boards.greenhouse.io/acme/jobs/123",
        "date_posted": "2026-08-08",
        "canonical_url": "https://job-boards.greenhouse.io/acme/jobs/123",
        "normalized_company": "Acme",
        "title_slug": "Customer Enablement Lead",
        "search_agent": "claude",
    }
    fields.update(overrides)
    return db.NewPosting(**fields)
