"""URL canonicalization — the sole insert-idempotency mechanism (PRD Step 2).

Reducing every posting URL to a stable canonical form, stored under a ``UNIQUE``
constraint, is what makes the pipeline safe to re-run: overlapping daily search
windows, retried crons, and wide CLI windows all re-surface known postings, and
each one no-ops on the constraint. It also carries the cross-source dedup load,
since both search agents converge on the same index-linked ATS URL for a req.

This is a pure function with no I/O so it is trivially testable.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Exact query-parameter names to drop (tracking / session / stateful params).
_DENYLIST_EXACT = {
    "gh_src",
    "gh_jid",
    "currentjobid",
    "ref",
    "source",
    "src",
    "lever-source",
    "lever-origin",
    "trackingid",
    "recruiter",
    "utm",
}

# Query parameters whose names start with any of these prefixes are dropped.
_DENYLIST_PREFIXES = ("utm_",)


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k in _DENYLIST_EXACT or any(k.startswith(p) for p in _DENYLIST_PREFIXES)


def canonicalize_url(url: str) -> str:
    """Return a canonical form of ``url`` for use as the idempotency key.

    Steps: lowercase the scheme and host, strip tracking/session query params
    (see the denylist above), drop the URL fragment, and normalize the trailing
    slash. Path case is preserved because ATS job IDs and slugs are
    case-sensitive.
    """
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query = urlencode(kept)

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Fragment intentionally dropped.
    return urlunsplit((scheme, netloc, path, query, ""))
