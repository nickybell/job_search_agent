"""Map a posting's detail URL to its ATS platform and identifiers.

The four supported platforms are an *inclusion criterion* (PRD): a URL that does
not resolve to one of them is out of scope, and its row simply keeps a NULL
``jd_markdown``. Identifiers are derived from the URL exactly as shown — never
guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ResolvedATS:
    """A posting resolved to a supported ATS.

    ``platform`` is one of greenhouse/lever/ashby/rippling; ``board`` is the
    token/slug/org/board segment; ``job_id`` is the numeric id or UUID.
    """

    platform: str
    board: str
    job_id: str


_GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_GREENHOUSE_PATH = re.compile(r"^/(?P<board>[^/]+)/jobs/(?P<id>\d+)")

_LEVER_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}
_UUID = r"[0-9a-fA-F-]{16,}"
_LEVER_PATH = re.compile(rf"^/(?P<board>[^/]+)/(?P<id>{_UUID})")

_ASHBY_HOSTS = {"jobs.ashbyhq.com"}
_ASHBY_PATH = re.compile(rf"^/(?P<board>[^/]+)/(?P<id>{_UUID})")

_RIPPLING_HOSTS = {"ats.rippling.com"}
_RIPPLING_PATH = re.compile(r"^/(?P<board>[^/]+)/jobs/(?P<id>[^/?]+)")


def resolve_ats_url(url: str) -> ResolvedATS | None:
    """Return the resolved ATS for ``url``, or ``None`` if unsupported."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path

    if host in _GREENHOUSE_HOSTS:
        m = _GREENHOUSE_PATH.match(path)
        if m:
            return ResolvedATS("greenhouse", m["board"], m["id"])
    elif host in _LEVER_HOSTS:
        m = _LEVER_PATH.match(path)
        if m:
            return ResolvedATS("lever", m["board"], m["id"])
    elif host in _ASHBY_HOSTS:
        m = _ASHBY_PATH.match(path)
        if m:
            return ResolvedATS("ashby", m["board"], m["id"])
    elif host in _RIPPLING_HOSTS:
        m = _RIPPLING_PATH.match(path)
        if m:
            return ResolvedATS("rippling", m["board"], m["id"])
    return None
