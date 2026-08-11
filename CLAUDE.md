# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

A personal job-search agent (Claude Agent SDK) that runs a daily search for Customer Enablement / Education / AI Enablement roles, stores postings idempotently with their full job descriptions, and collects fit feedback for a learning loop.

**Steps 1–3 and 5 are implemented** in Python (`src/jsa/`, managed with `uv`): the A/B search runners, canonical-URL idempotent insert, full-JD capture from all four supported ATS platforms, the direct job-add path (`jsa add`), the deterministic review loop, the Google Sheet tracker write (`jsa track`), the `jsa` CLI, and the Fly.io deployment (`Dockerfile` + `fly.toml`).

**Deferred (specified, not built):** Step 4 (per-job resume revisions from `base_resume.docx`) and the ground-truth prompt-refinement cron. Open decisions live as checkboxes in `TODO.md`.

The design docs remain the source of truth and precede the code:
- `prd.md` — product requirements and **source of truth**. The pipeline is a numbered 5-step flow (Steps 1–5, with 4a/4b); internalize that numbering — the rest of the docs reference it constantly.
- `deep_research_prompt.md` — the actual search prompt, a template with a `{{SEARCH_WINDOW}}` slot. Carries the full output contract and liveness gates inline.
- `TODO.md` — open decisions and the user's setup checklist.

## Commands

```bash
uv sync                                                   # create venv, install deps
uv run jsa init-db                                        # create the postings table (idempotent)
uv run jsa search                                         # today's search (agent auto-selected by date)
uv run jsa search --agent claude --window-hours 72        # explicit agent + window
uv run jsa add <ATS_URL>                                  # add one posting by hand (local, prompts)
uv run jsa review                                         # work the fit-review backlog (local, interactive)
uv run jsa track --dry-run                                # preview the Step 5 tracker append
uv run jsa track                                          # append Apply rows to the Sheet (local, needs gws)
uv run pytest                                             # test suite
uv run ruff check src tests                               # lint
uv run ruff format src tests                              # format
```

- **The `pytest` suite covers Steps 3 and 5, the manual-add path, and the schema migration.** It is deliberately hermetic: a throwaway `file:` SQLite DB, a stubbed ATS fetch, and a stub `gws` binary via `JSA_GWS_BIN` — it never touches hosted Turso or a real Sheet. Pure logic (`canonicalize`, `naming`, `search.parse`, `ats.resolve`, `review.parse_feedback_entry`, `tracker.build_row`, `manual.company_from_board`) is written I/O-free so it stays trivially testable — keep new pure logic that way. The search runners (Steps 1–2) are still untested; they need real API spend.
- **Local dev without hosted Turso:** set `TURSO_DATABASE_URL=file:dev.db` in `.env` to run against a throwaway local SQLite file (the DB layer is transport-agnostic — see below).
- **Deploy (run by the user; sets billed secrets — never via an agent transcript):** `fly launch --no-deploy` → `fly secrets set …` → `fly machine run . --rm` (one-off smoke test) → `fly machine run . --schedule daily`. Full sequence in `README.md`.

## Architecture the code must respect

Load-bearing decisions from `prd.md`. Do not quietly deviate:

- **Cloud/local split.** Steps 1–2 (search, insert) and the prompt-refinement cron run **headless on Fly.io**; Steps 3–5 (review, resume revisions, Sheet write, `.docx`/`.pdf`) run **locally**. This drives where credentials live (Perplexity key is a Fly secret; Google Sheets OAuth stays local).
- **One hosted DB, no copies.** The JD database is **Turso (hosted libSQL)**. Both cloud cron and local sessions connect to the *same* copy — never introduce a second local SQLite file that diverges. (`file:` URLs are for throwaway dev only.) The connection goes through `turso_serverless` (DB-API 2.0 over Turso's HTTP/Hrana transport — the managed platform does not serve WebSockets); `db.connect` enables autocommit via **`isolation_level = None`** — the DB-API `autocommit` attribute is a silent no-op on this client, so without it every write opens an implicit `BEGIN` that `close()` rolls back (a silent-data-loss trap that once ate a whole run). Both pipeline inserts and the review loop depend on this.
- **Single `postings` table.** Holds search output, the `canonical_url` idempotency key, the full JD, and fit feedback only. It deliberately does **not** store application state — the Google Sheet is the write-only tracker and the agent never reads it back. Don't mirror sheet/application state into the DB.
- **Single-mechanism insert idempotency — no dedup subsystem.** Dedup is exactly one mechanism: URL canonicalization (`canonicalize.py`) into a `UNIQUE canonical_url` + `INSERT … ON CONFLICT DO NOTHING RETURNING id`. Because the prompt requires index-linked ATS URLs, both agents converge on the same canonical URL for a req, so this also covers cross-source duplicates and makes re-runs safe. Do **not** reintroduce a second dedup stage or embeddings (both were removed by design).
- **Full-JD capture at insert, not a liveness gate.** Only genuinely-new rows (those `insert_posting` returns an id for) get a full-JD fetch. `ats/resolve.py` maps the URL to platform/board/id; `ats/fetch.py` GETs the ATS detail record and stores `jd_markdown` (HTML→Markdown), structured `location`, and the ATS-canonical `title` (which overwrites the agent's transcription and re-derives `title_slug`). **A failed fetch never excludes a row** — it degrades to `NULL jd_markdown`.
- **Four supported ATS = an inclusion criterion.** Greenhouse, Lever, Ashby, Rippling. Each has a distinct detail-record shape (see the per-platform fetchers in `ats/fetch.py`, verified live — e.g. Rippling's description is a `{role, company}` HTML dict, Lever falls back US→EU host, Ashby has no per-id GET so the org board is scanned). A URL that resolves to none of them is out of scope. Any other platform (Workday, custom sites) has no supported index check — do not add per-platform verification for it.
- **A/B search alternation.** `select_agent_for_date` derives the agent from day-of-year parity (even = Claude Deep Research, odd = Perplexity **Agent API deep research at the `xhigh` preset**) so one cron entry self-selects. Both runners must return the `postings` JSON contract in `prd.md`; the Perplexity runner also enforces it via `response_format` json_schema. It calls the **Agent API** (`POST /v1/agent` with `preset` + `input` + an explicit `web_search` tool), **not** the older Sonar `sonar-pro` chat-completions endpoint (a single-pass search, not comparable in depth). It streams typed SSE events whose *progress* vocabulary is preset-dependent — the reasoning tiers emit `response.reasoning.*` events, but `xhigh` searches inside sandboxed code (`response.sandbox.results`) — so the runner's pure `_StreamState` counts both; `high` is the cheaper fallback tier (a one-line `PRESET` change).
- **CLI-runnable crons.** The automated Steps 1–2 must also be invocable by hand with a parameterized window and agent — the cron and `jsa search` share `run_pipeline`.
- **The manual-add path reuses Step 2, it does not fork it.** `manual.py` runs the same canonicalize → `insert_posting` → `resolve_ats_url` → `fetch_detail` sequence, so a hand-added row is indistinguishable downstream (`search_agent = 'manual'` is the only marker). It is also **decided `Apply` on arrival** (written in the INSERT, not a follow-up UPDATE) and so skips Step 3 entirely, landing directly in the Step 5 tracker queue — supplying the URL *is* the decision. Re-adding an existing row promotes it to `Apply` (keeping `fit_feedback` and `search_agent`, which record what really happened). Two further intentional departures, both load-bearing: it writes **no `search_findings` row** (that table is A/B search telemetry; a supplied posting would inflate an agent's coverage), and **an unsupported ATS is not a rejection** (the four-ATS rule is a liveness proxy for postings an agent found on its own — the user has already vouched for this one, so it inserts with a `NULL jd_markdown`).
- **Step 5's idempotency is `added_to_tracker`, and the flag is set only on confirmed appends.** `pending_tracker` (`decision = 'Apply' AND added_to_tracker = 0`) *is* the guard. `tracker.append_row` raises on any ambiguity — non-zero `gws` exit, unparseable output, a response reporting no updated row — because optimistically flagging on exit code 0 would drop a job out of the tracker permanently. Rows are appended one at a time so a single failure cannot strand the rest of the backlog. While Step 4 is unbuilt the trigger is the `Apply` decision rather than packet-exists; see the interim note in `prd.md`.
- **`decision` is revisable inside a review session, and the comment field must be truly editable.** `fit_feedback` feeds the ground-truth loop, so friction there is a design defect: prompts go through `prompting.py` (a `prompt_toolkit` wrapper with an `input()` fallback for non-TTY/headless) for arrow keys, word delete, and the *pre-filled editable buffer* that the amend flow depends on. Don't regress the review loop back to bare `input()`.
- **A schema change to `postings` needs a migration, not just an edited `_SCHEMA`.** `CREATE TABLE IF NOT EXISTS` no-ops against the live database, and SQLite cannot ALTER a CHECK constraint. `db.migrate_postings_schema` does the rebuild (create → copy → rename → rename → drop, ordered so the data is never absent from a table named `postings`) and `init_db` calls it, so every command applies it. Keep the column list in one place (`_POSTINGS_COLUMNS`) so the live schema and the migration target cannot drift.

## How the pipeline is wired

`cli.py` → `pipeline.run_pipeline` is the daily-cron body: `search.load_prompt` (interpolates the window) → the selected runner (`search/claude_runner.py` or `search/perplexity_runner.py`) → `search.parse_search_output` → per posting: `canonicalize_url` → `db.insert_posting` (idempotent) → for new rows only, `resolve_ats_url` → `fetch_detail` → `db.update_jd_capture`. Step 3 (`review.py`) is a separate deterministic, no-LLM loop: query `NULL decision` rows, open each in Chrome, write the decision back — invoked directly (`uv run jsa review` / `!`-prefixed), never as a slash command (which would reintroduce per-posting token cost).

The two local side-doors reuse those same pieces rather than duplicating them:

- `cli.py` → `manual.add_posting` is the direct job-add path: `canonicalize_url` → `db.find_by_canonical_url` (a UX read only — the UNIQUE constraint is still the real guard) → `resolve_ats_url` → `fetch_detail` (best-effort) → `db.insert_posting` → `db.update_jd_capture`.
- `cli.py` → `tracker.run_tracker` is Step 5: `db.pending_tracker` → `tracker.build_row` (pure) → `tracker.append_row` (shells out to the local `gws` CLI) → `db.mark_tracked`, per row. Step 4, when built, calls `jsa track --id <row>` as its final action — that is the seam between the two steps.

## Working conventions

- Keep `prd.md` the source of truth. Reflect design decisions made in conversation there; when a decision is still open, it belongs in `TODO.md` as a checkbox with the reasoning, not silently in code.
- Use `AskUserQuestion` liberally for decisions requiring judgment (a global user preference).
- `base_resume.docx` is gitignored and never committed. The repo is intended as a public portfolio piece — keep it clean.
