"""Fetch and normalize the ATS detail record for a resolved posting.

Each platform exposes the full description differently; this module hides those
shapes behind one ``fetch_detail`` call returning a uniform ``ATSDetail``. The
caller (the pipeline) treats any exception as "capture failed" and leaves the
row's jd_markdown/location NULL.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .html_to_md import html_to_markdown
from .resolve import ResolvedATS

_TIMEOUT = httpx.Timeout(20.0)
_HEADERS = {"User-Agent": "job-search-agent/0.1 (+https://github.com/nicky-bell)"}


@dataclass
class ATSDetail:
    """Normalized detail-record fields captured at insert time."""

    jd_markdown: str
    location: str | None
    title: str | None


def _stringify_location(value: object) -> str | None:
    """Coerce an ATS location to text.

    Handles a plain string, a dict (Greenhouse ``{"name": …}``), and a list of
    strings (Rippling ``workLocations``).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("name", "location", "locationName", "text"):
            if value.get(key):
                return str(value[key])
    if isinstance(value, list):
        names = [_stringify_location(v) for v in value]
        joined = "; ".join(n for n in names if n)
        return joined or None
    return None


def fetch_detail(resolved: ResolvedATS, client: httpx.Client) -> ATSDetail:
    """Dispatch to the per-platform fetcher. Raises on any HTTP/parse failure."""
    if resolved.platform == "greenhouse":
        return _fetch_greenhouse(resolved, client)
    if resolved.platform == "lever":
        return _fetch_lever(resolved, client)
    if resolved.platform == "ashby":
        return _fetch_ashby(resolved, client)
    if resolved.platform == "rippling":
        return _fetch_rippling(resolved, client)
    raise ValueError(f"unsupported platform: {resolved.platform}")


def _fetch_greenhouse(resolved: ResolvedATS, client: httpx.Client) -> ATSDetail:
    url = f"https://boards-api.greenhouse.io/v1/boards/{resolved.board}/jobs/{resolved.job_id}"
    data = client.get(url, headers=_HEADERS, timeout=_TIMEOUT).raise_for_status().json()
    # `content` is HTML-entity-escaped and must be unescaped before conversion.
    return ATSDetail(
        jd_markdown=html_to_markdown(data["content"], unescape=True),
        location=_stringify_location(data.get("location")),
        title=data.get("title"),
    )


def _fetch_lever(resolved: ResolvedATS, client: httpx.Client) -> ATSDetail:
    # Lever postings live on either the US or EU host; try US, fall back to EU.
    hosts = ["api.lever.co", "api.eu.lever.co"]
    last_exc: Exception | None = None
    for host in hosts:
        url = f"https://{host}/v0/postings/{resolved.board}/{resolved.job_id}?mode=json"
        try:
            data = client.get(url, headers=_HEADERS, timeout=_TIMEOUT).raise_for_status().json()
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            continue
        description_html = data.get("description") or data.get("descriptionPlain", "")
        location = data.get("categories", {}).get("location")
        return ATSDetail(
            jd_markdown=html_to_markdown(description_html),
            location=_stringify_location(location),
            title=data.get("text"),
        )
    raise last_exc if last_exc else RuntimeError("lever fetch failed")


def _fetch_ashby(resolved: ResolvedATS, client: httpx.Client) -> ATSDetail:
    # Ashby has no per-id detail GET: fetch the org board and match the UUID.
    url = f"https://api.ashbyhq.com/posting-api/job-board/{resolved.board}"
    data = client.get(url, headers=_HEADERS, timeout=_TIMEOUT).raise_for_status().json()
    for job in data.get("jobs", []):
        if job.get("id") == resolved.job_id or str(job.get("jobUrl", "")).rstrip("/").endswith(
            resolved.job_id
        ):
            return ATSDetail(
                jd_markdown=html_to_markdown(job.get("descriptionHtml", "")),
                location=_stringify_location(job.get("location")),
                title=job.get("title"),
            )
    raise LookupError(f"ashby job {resolved.job_id} not found on board {resolved.board}")


def _fetch_rippling(resolved: ResolvedATS, client: httpx.Client) -> ATSDetail:
    # Verified against a live board (2026-07-21): the v2 per-job record exposes
    # the description as a dict of HTML sections ({"role": ..., "company": ...}),
    # the title as `name`, and locations as a `workLocations` list. Fall back to
    # scanning the paginated list only if the per-job endpoint 404s (that path
    # recovers title/location but not the description).
    base = f"https://ats.rippling.com/api/v2/board/{resolved.board}/jobs"
    try:
        data = (
            client.get(f"{base}/{resolved.job_id}", headers=_HEADERS, timeout=_TIMEOUT)
            .raise_for_status()
            .json()
        )
        job = data.get("job", data)
    except httpx.HTTPStatusError:
        job = _rippling_scan_list(resolved, client, base)
    return ATSDetail(
        jd_markdown=html_to_markdown(_rippling_description_html(job.get("description"))),
        location=_stringify_location(job.get("workLocations") or job.get("locations")),
        title=job.get("name") or job.get("title"),
    )


def _rippling_description_html(description: object) -> str:
    """Flatten Rippling's description into one HTML string.

    A per-job record serves ``{"role": <html>, "company": <html>}`` (role is the
    job itself, company is boilerplate — role first); older/list shapes may serve
    a bare HTML string.
    """
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        parts = [description.get("role", ""), description.get("company", "")]
        return "\n".join(p for p in parts if p)
    return ""


def _rippling_scan_list(resolved: ResolvedATS, client: httpx.Client, base: str) -> dict:
    page = 0
    while page < 20:  # bound the pagination
        resp = client.get(
            base,
            params={"page": page, "pageSize": 50},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        ).raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            break
        for job in items:
            if str(job.get("id")) == resolved.job_id or str(job.get("url", "")).endswith(
                resolved.job_id
            ):
                return job
        page += 1
    raise LookupError(f"rippling job {resolved.job_id} not found on board {resolved.board}")
