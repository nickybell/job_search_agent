"""Runtime configuration, read from the environment.

Locally these come from a gitignored ``.env`` (loaded here via python-dotenv);
in the Fly.io deployment the same names are injected as ``fly secrets``. Either
way the code just reads ``os.environ``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    """Resolved credentials and connection settings for one run."""

    turso_database_url: str
    turso_auth_token: str | None
    anthropic_api_key: str | None
    perplexity_api_key: str | None


def load_config() -> Config:
    """Read configuration from the environment.

    Only ``TURSO_DATABASE_URL`` is required for every command; the per-agent API
    keys are validated lazily by whichever search runner needs them, so that
    e.g. ``jsa review`` works with only the database configured.
    """
    url = os.environ.get("TURSO_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "TURSO_DATABASE_URL is not set. Copy .env.example to .env and fill it in, "
            "or export it in the environment (Fly secrets do this in the cloud)."
        )
    return Config(
        turso_database_url=url,
        turso_auth_token=os.environ.get("TURSO_AUTH_TOKEN") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        perplexity_api_key=os.environ.get("PERPLEXITY_API_KEY") or None,
    )
