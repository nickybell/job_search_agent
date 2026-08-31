"""Runtime configuration, read from the environment.

Locally these come from a gitignored ``.env`` (loaded here via python-dotenv);
in the Fly.io deployment the same names are injected as ``fly secrets``. Either
way the code just reads ``os.environ``.

Note what is *not* here: Claude auth. Every Claude-driven step drives the Claude
Agent SDK, which spawns the Claude Code CLI subprocess, and that CLI reads its
credential (``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``, or a
pay-as-you-go ``ANTHROPIC_API_KEY``) straight from the inherited environment. No
code passes it, so it does not belong on ``Config``.
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
    perplexity_api_key: str | None


def load_config() -> Config:
    """Read configuration from the environment.

    Only ``TURSO_DATABASE_URL`` is required for every command; the Perplexity
    API key is validated lazily by the search runner that needs it, so that
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
        perplexity_api_key=os.environ.get("PERPLEXITY_API_KEY") or None,
    )
