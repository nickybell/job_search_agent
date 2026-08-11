"""The direct job-add path: ingest one posting from a URL the user supplies.

The PRD's Database section always assumed Nicky could hand the agent a posting
directly; this is that route. It deliberately reuses Step 2 wholesale —
canonicalize → idempotent insert → full-JD fetch — rather than opening a second
way into the table, so a hand-added row is indistinguishable downstream from a
searched one and lands in the same Step 3 review backlog. The only difference
is provenance: ``search_agent = 'manual'``.

**A hand-added posting is decided `Apply` on arrival.** Supplying the URL *is*
the decision — the user found this posting, read it, and chose to add it — so
routing it through the Step 3 backlog would ask a question they just answered.
It therefore skips review entirely and lands directly in the Step 5 tracker
queue, reaching the Google Sheet by the same path as a searched posting the
user marked `Apply`. Adding a URL already in the table upgrades that row's
decision to `Apply` for the same reason, keeping any `fit_feedback` already
written about it.

Two further deliberate departures from the automated pipeline:

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


# Supplying a URL by hand is itself the Apply decision (see module docstring).
MANUAL_DECISION = "Apply"


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
    # What the row's decision was before this add, when it already existed:
    # None for a fresh insert, otherwise the prior value (possibly NULL).
    previous_decision: str | None = None
    decision_changed: bool = False


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
            return _upgrade_existing(client, existing, canonical)

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
                decision=MANUAL_DECISION,
            ),
        )
        if new_id is None:
            # Lost the race against a concurrent insert; the UNIQUE constraint
            # is the real guard and it held.
            row = db.find_by_canonical_url(client, canonical)
            if row is not None:
                return _upgrade_existing(client, row, canonical)
            return ManualAddResult(
                status="already_present",
                canonical_url=canonical,
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
            decision_changed=True,
        )
    finally:
        client.close()


def _upgrade_existing(client, existing: tuple, canonical: str) -> ManualAddResult:
    """Report a row that already exists, promoting its decision to ``Apply``.

    Re-adding a URL by hand carries the same intent as adding a new one, so a
    row previously left undecided (or skipped in review) is promoted. Only the
    decision moves: any ``fit_feedback`` written about it stays, since it is
    still ground truth for the search-prompt loop.
    """
    existing_id, company, title, _url, decision, _tracked = existing
    posting_id = int(existing_id)
    changed = decision != MANUAL_DECISION
    if changed:
        db.set_decision(client, posting_id, MANUAL_DECISION)
    return ManualAddResult(
        status="already_present",
        canonical_url=canonical,
        posting_id=posting_id,
        company=company,
        title=title,
        previous_decision=decision,
        decision_changed=changed,
    )
