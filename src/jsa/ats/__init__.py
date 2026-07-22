"""Full-JD capture from a posting's own public ATS detail record (PRD Step 2).

For each genuinely new row, Step 2 GET-fetches the posting's ATS JSON *detail*
record — the same four supported platforms the search prompt already restricts
every surviving posting to — and stores the description as Markdown plus the
ATS's structured location and canonical title. A failed fetch never excludes a
row (the row keeps NULL jd_markdown/location); this is content capture, not a
liveness gate.
"""

from .fetch import ATSDetail, fetch_detail
from .resolve import ResolvedATS, resolve_ats_url

__all__ = ["ATSDetail", "fetch_detail", "ResolvedATS", "resolve_ats_url"]
