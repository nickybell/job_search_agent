"""Capture a job description from schema.org JSON-LD, for unsupported ATS.

The four supported ATS platforms are an inclusion criterion for the *search*,
where they double as the liveness proxy: the agent found those postings on its
own, so their public JSON list endpoints are what establish the req is really
open. That reasoning does not extend to the direct job-add path, where the user
supplied the URL and has already vouched for it — there, the only question is
whether the description text can be retrieved.

It usually can. Google requires ``JobPosting`` JSON-LD for a posting to appear
in Google Jobs, so it is widely embedded server-side even on platforms whose
human-facing pages are JavaScript shells. Measured 2026-08-11: a Workday detail
page served a full ``JobPosting`` block to a plain GET with no JS, as did Ashby.
Greenhouse and Lever serve none — which is fine, since those have first-class
fetchers already.

**This is capture, never liveness.** A pulled or unlisted req can still render
perfectly good JSON-LD, so a successful extraction here says what the posting
*claims*, never whether it is open. Nothing may treat it as evidence a posting
is live — which is exactly why the search path does not use this module (see
``pipeline``): a searched URL outside the four platforms is a prompt violation
with unverified liveness, and enriching it would make it look legitimate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .html_to_md import html_to_markdown

_TIMEOUT = httpx.Timeout(20.0)
# A browser-ish UA: some career sites serve a stub or a challenge page to
# obviously-automated clients, and the JSON-LD is what gets dropped first.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_LD_BLOCK = re.compile(
    r"<script[^>]*type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class JobPostingLD:
    """The fields of a schema.org JobPosting this project cares about."""

    title: str | None
    description_html: str | None
    location: str | None


def _walk(node: object):
    """Yield every dict in a decoded JSON-LD document.

    JSON-LD arrives in several shapes in the wild: a bare object, a list of
    objects, or an ``@graph`` wrapper — sometimes nested. Walking everything is
    cheaper and more robust than special-casing each layout.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _is_job_posting(node: dict) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return "JobPosting" in node_type
    return node_type == "JobPosting"


def _location_from(node: dict) -> str | None:
    """Render ``jobLocation`` into the same flat text the ATS fetchers produce."""
    if node.get("jobLocationType") == "TELECOMMUTE" and not node.get("jobLocation"):
        return "Remote"
    parts: list[str] = []
    for loc in _as_list(node.get("jobLocation")):
        if isinstance(loc, str):
            parts.append(loc)
            continue
        if not isinstance(loc, dict):
            continue
        address = loc.get("address")
        if isinstance(address, str):
            parts.append(address)
        elif isinstance(address, dict):
            fields = ("addressLocality", "addressRegion", "addressCountry")
            bits = [str(address[f]) for f in fields if address.get(f)]
            if bits:
                parts.append(", ".join(bits))
    # Deduplicate while preserving order; multi-location reqs repeat a city.
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return "; ".join(seen) or None


def _as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def extract_job_posting(html_text: str) -> JobPostingLD | None:
    """Find the first JSON-LD ``JobPosting`` in a page. Pure — no I/O.

    Returns None when the page carries no JSON-LD, none of it is a JobPosting,
    or every block fails to parse. A malformed block never raises: pages
    routinely ship one broken script tag alongside good ones.
    """
    for raw in _LD_BLOCK.findall(html_text or ""):
        try:
            document = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk(document):
            if not _is_job_posting(node):
                continue
            title = node.get("title")
            return JobPostingLD(
                title=str(title).strip() if title else None,
                description_html=node.get("description") or None,
                location=_location_from(node),
            )
    return None


def _looks_entity_escaped(text: str) -> bool:
    """True when the description ships escaped markup rather than real HTML.

    Most sites put real HTML in the JSON string, but some escape it (the same
    trap Greenhouse's ``content`` field sets). Detect rather than guess, or the
    JD lands in the database as visible ``&lt;p&gt;`` noise.
    """
    return "<" not in text and ("&lt;" in text or "&gt;" in text)


def to_markdown(posting: JobPostingLD) -> str:
    """Render the extracted description as Markdown. Pure."""
    body = posting.description_html or ""
    return html_to_markdown(body, unescape=_looks_entity_escaped(body))


def fetch_jsonld_detail(url: str, client: httpx.Client) -> JobPostingLD:
    """GET ``url`` and extract its JSON-LD JobPosting. Raises if there is none."""
    response = client.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    response.raise_for_status()
    posting = extract_job_posting(response.text)
    if posting is None:
        raise LookupError("no schema.org JobPosting JSON-LD found on the page")
    return posting
