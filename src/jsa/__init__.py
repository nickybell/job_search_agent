"""Job Search Agent — daily search, idempotent capture, and fit review.

Package layout mirrors the PRD's numbered flow (Steps 1–5):

- ``search/``  — Step 1: the A/B-alternating daily search runners.
- ``ats/``     — Step 2: full-JD capture from the posting's own ATS detail record.
- ``db.py``    — the single Turso ``postings`` table and its idempotent insert.
- ``pipeline.py`` — Steps 1→2 orchestration (search → insert → JD fetch).
- ``review.py`` — Step 3: the deterministic (no-LLM) fit-review loop.

Steps 4–5 (resume revisions, Google Sheet tracker) are not yet implemented.
"""

__version__ = "0.1.0"
