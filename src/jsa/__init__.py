"""Job Search Agent — daily search, idempotent capture, and fit review.

Package layout mirrors the PRD's numbered flow (Steps 1–5):

- ``search/``  — Step 1: the Claude and Perplexity daily search runners.
- ``ats/``     — Step 2: full-JD capture from the posting's own ATS detail record.
- ``db.py``    — the single Turso ``postings`` table and its idempotent insert.
- ``pipeline.py`` — Steps 1→2 orchestration (search → insert → JD fetch).
- ``manual.py`` — the direct job-add path: one user-supplied URL through Step 2.
- ``review.py`` — Step 3: the deterministic (no-LLM) fit-review loop.
- ``prompting.py`` — line-edited terminal input shared by the local commands.
- ``refetch.py`` — reconciles stored postings against their (mutable) ATS record.
- ``packet.py`` — Step 4's deterministic head: the packet directory + ``job_posting.md``.
- ``generate.py`` — Step 4: the one-shot resume tailoring (``jsa generate``).
- ``docx_patch.py`` — applies the structured tailoring patch to the ``.docx``.
- ``tracker.py`` — Step 5: appending ``Apply`` rows to the Google Sheet tracker.
"""

__version__ = "0.1.0"
