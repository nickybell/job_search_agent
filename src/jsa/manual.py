"""The direct job-add path: ingest one posting from a URL the user supplies.

The PRD's Database section always assumed Nicky could hand the agent a posting
directly; this is that route. It deliberately reuses Step 2 wholesale —
canonicalize → idempotent insert → full-JD fetch — rather than opening a second
way into the table, so a hand-added row is indistinguishable downstream from a
searched one and lands in the same Step 3 review backlog. The only difference
is provenance: ``search_agent = 'manual'``.

Two deliberate departures from the automated pipeline:

* **No ``search_findings`` row.** That table is A/B search telemetry; a manual
  add is not a search result and must not skew agent coverage or precision.
* **The four-ATS rule does not gate the insert.** Supported-ATS membership is an
  inclusion criterion for what the *search* may return — it is a liveness proxy
  for postings the agent found on its own. Here the user has already vouched for
  the posting, so an unsupported URL still inserts; it simply keeps a NULL
  ``jd_markdown``, exactly as a failed fetch does in the pipeline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from . import db, prompting
from .ats import fetch_detail, resolve_ats_url
from .canonicalize import canonicalize_url
from .config import Config
from .naming import normalize_company, slugify_title

log = logging.getLogger(__name__)

_BOARD_SEPARATORS = re.compile(r"[-_.]+")


class ManualAddError(RuntimeError):
    """The posting could not be added (bad URL, or missing company/title)."""


@dataclass
class ManualAddResult:
    """What happened to one hand-added URL."""

    status: str  # "inserted" | "already_present"
    canonical_url: str
    posting_id: int | None = None
    company: str = ""
    title: str = ""
    platform: str | None = None
    jd_captured: bool = False
    fetch_error: str | None = None


def company_from_board(board: str) -> str:
    """Guess a company name from an ATS board slug. Pure.

    Board slugs are the company (``…/gitlab/jobs/123``), just lowercased and
    hyphenated, so this is a decent default — but only a default: it is offered
    pre-filled for the user to correct, never written unreviewed in an
    interactive session.
    """
    words = [w for w in _BOARD_SEPARATORS.split(board.strip()) if w]
    return " ".join(word.capitalize() for word in words)


def _fetch_best_effort(url: str) -> tuple[object | None, str | None, str | None]:
    """Resolve and fetch the ATS detail record; never raise. -> (detail, platform, error)."""
    resolved = resolve_ats_url(url)
    if resolved is None:
        return None, None, None
    try:
        with httpx.Client(follow_redirects=True) as http:
            return fetch_detail(resolved, http), resolved.platform, None
    except Exception as exc:  # content capture, not a gate — mirrors the pipeline
        log.warning("JD fetch failed for %s: %s", url, exc)
        return None, resolved.platform, f"{type(exc).__name__}: {exc}"


def add_posting(
    url: str,
    config: Config,
    *,
    company: str | None = None,
    title: str | None = None,
    date_posted: str | None = None,
    interactive: bool = True,
) -> ManualAddResult:
    """Add one user-supplied posting URL to the database.

    Company and title are resolved in precedence order: an explicit argument,
    then the ATS detail record's canonical title / the board slug, then (in an
    interactive session) whatever the user edits into the pre-filled prompt.
    Both are NOT NULL columns, so a non-interactive add that can derive neither
    fails loudly rather than inventing a placeholder.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ManualAddError(f"not an http(s) URL: {url!r}")
    canonical = canonicalize_url(url)

    client = db.connect(config)
    db.init_db(client)
    try:
        existing = db.find_by_canonical_url(client, canonical)
        if existing is not None:
            existing_id, existing_company, existing_title, _, _, _ = existing
            return ManualAddResult(
                status="already_present",
                canonical_url=canonical,
                posting_id=int(existing_id),
                company=existing_company,
                title=existing_title,
            )

        detail, platform, fetch_error = _fetch_best_effort(url)
        resolved_title = title or (getattr(detail, "title", None) or "")
        board = getattr(resolve_ats_url(url), "board", "") or ""
        resolved_company = company or company_from_board(board)

        if interactive:
            resolved_company = prompting.ask("  Company: ", default=resolved_company)
            resolved_title = prompting.ask("  Title:   ", default=resolved_title)
        if not resolved_company or not resolved_title:
            raise ManualAddError(
                "company and title are required and could not be derived from the URL — "
                "pass --company and --title."
            )

        new_id = db.insert_posting(
            client,
            db.NewPosting(
                company=resolved_company,
                title=resolved_title,
                url=url,
                date_posted=date_posted,
                canonical_url=canonical,
                normalized_company=normalize_company(resolved_company),
                title_slug=slugify_title(resolved_title),
                search_agent="manual",
            ),
        )
        if new_id is None:
            # Lost the race against a concurrent insert; the UNIQUE constraint
            # is the real guard and it held.
            row = db.find_by_canonical_url(client, canonical)
            return ManualAddResult(
                status="already_present",
                canonical_url=canonical,
                posting_id=int(row[0]) if row else None,
                company=resolved_company,
                title=resolved_title,
            )

        jd = getattr(detail, "jd_markdown", None) or None
        if detail is not None:
            # title=None: the row was already inserted under the chosen title
            # (ATS-canonical by default), and passing it here would clobber a
            # deliberate --company/--title override with the ATS transcription.
            db.update_jd_capture(
                client,
                new_id,
                jd_markdown=jd,
                location=getattr(detail, "location", None),
                title=None,
            )
        return ManualAddResult(
            status="inserted",
            canonical_url=canonical,
            posting_id=new_id,
            company=resolved_company,
            title=resolved_title,
            platform=platform,
            jd_captured=bool(jd),
            fetch_error=fetch_error,
        )
    finally:
        client.close()
