"""Derive filesystem-safe company and title fields at insert time (PRD Step 2).

Computing ``normalized_company`` and ``title_slug`` once at write time means the
Step 4 application-packet builder can compose its ``~/Documents/Job
Applications/…`` paths directly from the row without re-sanitizing — and no
path-hostile character can reach the Step 4 ``mkdir`` guard to cause a spurious
failure. Pure functions, no I/O.
"""

from __future__ import annotations

import re

# Company suffixes stripped for a cleaner tracker/display name.
_COMPANY_SUFFIXES = re.compile(
    r"[,\s]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|co|co\.|"
    r"gmbh|plc|s\.a\.|sa|ag|nv|bv|pty|pbc)\.?$",
    re.IGNORECASE,
)

# Characters that are unsafe in a path segment on macOS/Unix (and Windows too).
_PATH_HOSTILE = re.compile(r'[/\\:*?"<>|\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")

_TITLE_SLUG_MAX = 80


def _strip_path_hostile(text: str) -> str:
    """Replace path-hostile characters with a space and collapse whitespace."""
    cleaned = _PATH_HOSTILE.sub(" ", text)
    return _WHITESPACE.sub(" ", cleaned).strip()


def normalize_company(company: str) -> str:
    """Title-case, suffix-strip, and filesystem-clean a raw company name."""
    name = company.strip()
    # Strip a trailing corporate suffix (possibly more than one, e.g. "Co., Ltd").
    while True:
        stripped = _COMPANY_SUFFIXES.sub("", name).strip()
        if stripped == name or not stripped:
            break
        name = stripped
    name = _strip_path_hostile(name)
    return name or company.strip()


def slugify_title(title: str) -> str:
    """Render the verbatim ``title`` filesystem-safe and length-bounded.

    ``title`` itself is stored verbatim in the DB; this slug is only what the
    Step 4 directory/filename builder consumes.
    """
    slug = _strip_path_hostile(title)
    if len(slug) > _TITLE_SLUG_MAX:
        slug = slug[:_TITLE_SLUG_MAX].rstrip()
    return slug or "untitled"
