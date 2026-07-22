"""Validated shape of the search agents' structured output (PRD Step 1).

Both the Claude (A-day) and Perplexity (B-day) runners must return the same
``postings`` JSON contract defined in ``prd.md`` and ``deep_research_prompt.md``.
Parsing raw model output through these pydantic models is what guarantees a
malformed response is caught before it reaches the database.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Posting(BaseModel):
    """One job posting as surfaced by a search agent.

    ``company``, ``title``, and ``url`` are required by the schema; ``date_posted``
    is optional because the prompt forbids guessing a date when none is anchored.
    """

    company: str
    title: str
    url: str
    date_posted: str | None = None


class SearchOutput(BaseModel):
    """Top-level object each search agent returns: ``{"postings": [...]}``."""

    postings: list[Posting] = Field(default_factory=list)
