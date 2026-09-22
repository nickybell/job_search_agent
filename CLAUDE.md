# CLAUDE.md

Guidance for Claude Code working in this repository.

## Project status

A personal job-search agent (Claude Agent SDK; Python in `src/jsa/`, managed with `uv`). All five PRD steps are implemented and in live use: the two search runners and the weekly cron cadence, canonical-URL idempotent insert with full-JD capture from four ATS platforms, the direct job-add path, the deterministic review loop, render-loop resume generation over a template library, the Google Sheet tracker write, and two learning loops (search-prompt refinement as a weekly PR, bullet-library sync). Deployed to Fly.io for the cloud half.

**Three documents, one owner per fact:**
- `prd.md` — the **source of truth**: every design decision *and its rationale*, present tense. The pipeline is a numbered 5-step flow (Steps 1–5); the rest of the docs reference that numbering constantly.
- `README.md` — how to run it, deploy it, and adapt it to another user.
- This file — the checklist of what the code must not break, with pointers into `prd.md`. Don't restate rationale here; link to it.

The prompts are versioned templates: `deep_research_prompt.md` (search, `{{SEARCH_WINDOW}}` slot, carries its output contract and liveness gates inline), `tailoring_prompt.md` (resume patch contract + render loop), `bullet_sync_prompt.md`, `refine_search_prompt.md`.

## Per-user configuration

Everything specific to one candidate lives outside `src/` and the shared prompts, behind an environment variable or a gitignored/vault file. When forking for another user these are the touch points (README, "Using this for your own search", is the user-facing checklist):

- `deep_research_prompt.md` — the Candidate section, target titles, and non-negotiable filters are user-authored; Sources, Output, and Liveness are machinery.
- `pipeline.CRON_SCHEDULE` — the weekly cadence.
- `resume_templates/` — the gitignored `.docx` library, one per role family.
- The vault files beside the packets (`JSA_PACKETS_DIR`): `resume_tailoring_rules.md` (`{{TAILORING_RULES}}`, e.g. a protected entry or a title-wording rule), `resume_voice.md` (`{{VOICE_SAMPLES}}`), `resume_bullets.csv` (`{{BULLET_LIBRARY}}`, synced by `jsa bullets`). All hand-curated; each degrades to a placeholder when missing.
- `JSA_CANDIDATE_NAME` (resume file-name prefix), `JSA_TRACKER_SPREADSHEET_ID`, and the `JSA_*` overrides listed in `.env.example`.
- The role-category vocabulary shared by `tailoring_prompt.md`, `bullet_sync_prompt.md`, and the CSV's `categories` column.
- `fly.toml`'s app name.

Never hard-code a candidate's name, employer, or personal path into `src/` or the shared prompts.

## Commands

```bash
uv sync                                                   # create venv, install deps
uv run jsa init-db                                        # create the postings table (idempotent)
uv run jsa search                                         # run one search (defaults to Perplexity, 48h)
uv run jsa search --agent claude --window-hours 72        # explicit agent + window
uv run jsa add <ATS_URL>                                  # add one posting by hand (local, prompts)
uv run jsa review                                         # work the fit-review backlog (local, interactive)
uv run jsa refetch --dry-run                              # report ATS drift on Apply postings not yet applied to (reads the Sheet to scope)
uv run jsa refetch --id 24                                # re-read one posting and reconcile it (no Sheet lookup)
uv run jsa packet --dry-run                               # preview Step 4's packet directories (Apply + untracked queue)
uv run jsa packet --id 24                                 # build one Apply row's packet even if already tracked
uv run jsa generate --dry-run                             # preview the Step 4 resume-generation queue
uv run jsa generate                                       # tailor resumes + track for the Apply + untracked queue (local)
uv run jsa generate --id 24                               # regenerate one Apply row even if already tracked
uv run jsa bullets --dry-run                              # list resumes newer than the last bullet-library sync
uv run jsa bullets                                        # fold new resumes' bullets into resume_bullets.csv (local)
uv run jsa refine --dry-run                               # preview the refinement scope (no model call, no run recorded)
uv run jsa refine                                         # run the ground-truth refinement pass by hand (CI runs it weekly)
uv run jsa track --dry-run                                # preview the Step 5 tracker append
uv run jsa track                                          # append Apply rows to the Sheet (local, needs gws)
uv run ruff check src                                     # lint
uv run ruff format src                                    # format
```

## Working rules

- **No agent-authored tests.** The repo deliberately ships without a test suite: a test written in the same agentic loop as the code only mirrors what that loop already believed. Tests come from the user or an adversarial agent in a separate loop. Do not add tests, pytest configuration, or test dependencies; verify with real-world checks (dry-runs against live data, one-off invocations, throwaway replicas). Keep the seams that make outside testing possible: pure logic (`canonicalize`, `naming`, `search.parse`, `ats.resolve`, `review.parse_feedback_entry`, `tracker.build_row`, `manual.company_from_board`, `docx_patch`, `bullets.is_resume_docx`, `generate.resume_file_stem`) stays I/O-free, and the override points (`TURSO_DATABASE_URL=file:…`, the `JSA_*` variables, the injectable `tailor`/`runner` callables) stay in place.
- **Every document is a living document, `prd.md` included.** Present-tense design plus current rationale; superseded content is rewritten in place — no resolved markers, no dated decision annotations, no "considered and rejected" records. The only archive is the git log. Reflect design decisions made in conversation in `prd.md`; an open question goes to the user (or, for the refiner, the PR body) — there is no TODO file.
- Use `AskUserQuestion` liberally for decisions requiring judgment.
- **The repo is a public portfolio piece — keep it clean.** `base_resume.docx`, `resume_templates/`, `IDEAS.md`, `reference/`, and `.env` are gitignored and never committed.
- **Local dev without hosted Turso:** `TURSO_DATABASE_URL=file:dev.db` runs against a throwaway SQLite file.
- **Deploy is run by the user** (it sets billed secrets, never via an agent transcript): `fly launch --no-deploy` → `fly secrets set …` → `fly machine run . --rm` → `fly machine run . --schedule daily`. Full sequence in `README.md`.

## Invariants the code must respect

Each is argued in `prd.md`; the section named is where the rationale lives. Do not quietly deviate.

- **Cloud/local split** (§ Architecture). Steps 1–2 and the refine cron run headless; Steps 3–5 run locally. Credentials follow: Perplexity key and Claude auth are cloud secrets; Google OAuth (`gws`) stays local. Claude auth is read by the SDK's CLI from the environment, never passed by code.
- **One hosted DB, no copies; autocommit via `isolation_level = None`** (§ Database, Connection contract). Never introduce a second local store; `file:` URLs are throwaway dev only. The DB-API `autocommit` attribute is a no-op on this client.
- **Single `postings` table, no application state; the Sheet is a projection** (§ Database, § Application Tracker). Agent-written Sheet columns may be refreshed from the DB (`tracker.update_title`); user columns (Date Applied, Status) are read only to scope work (`tracker.read_tracker_index`). Authority never flows Sheet → DB.
- **`search_findings` is append-only and outlives its `postings` rows** (§ Database). Its `decision` is denormalized by `db.sync_finding_decision` from `record_decision`/`set_decision`; `clear_decision` deliberately does not sync. No built-in report exists over it, by choice.
- **Single-mechanism idempotency, no dedup subsystem** (§ Insert Idempotency). `canonicalize.py` + `UNIQUE canonical_url` + `INSERT … ON CONFLICT DO NOTHING RETURNING id`. Do not reintroduce a second dedup stage or embeddings.
- **Full-JD capture is best-effort, never a gate** (§ Daily Search). New rows only; a failed fetch degrades to `NULL jd_markdown`. The ATS title overwrites the agent's and re-derives `title_slug`.
- **Four supported ATS = the search's inclusion criterion; capture ≠ liveness** (§ Daily Search, § Direct Job Add). Greenhouse, Lever, Ashby, Rippling. The JSON-LD fallback runs on the manual path only (fetcher → JSON-LD → `NULL`); a searched URL off the four platforms is never enriched. A JSON-LD capture is never evidence of liveness.
- **Weekly cadence in `pipeline.CRON_SCHEDULE`; Perplexity is the Agent API at `xhigh`, not `sonar-pro`** (§ Daily Search). The runner counts both `response.reasoning.*` and `response.sandbox.results` progress events. Claude is Opus at `xhigh`.
- **CLI-runnable crons** (§ Automation). `jsa cron` and `jsa search` share `run_pipeline`.
- **Manual add reuses Step 2, decided `Apply` on arrival, no `search_findings` row, unsupported ATS is not a rejection** (§ Direct Job Add). Re-adding promotes to `Apply` while keeping `fit_feedback` and `search_agent`.
- **Tracker row is A:H with `postings.id` in column A; append with `insertDataOption = OVERWRITE`, never `INSERT_ROWS`; flag only on a confirmed append** (§ Application Tracker). `append_row` raises on any ambiguity; rows go one at a time.
- **Refetch: a failed fetch leaves the row untouched; scope is Apply + not yet applied; title propagates to the Sheet; an existing packet is regenerated build-before-delete** (§ Posting Drift and Re-fetch). Refetch owns the delete, generate owns the rebuild; never creates a packet where none existed.
- **The Step 4 completion guard is `added_to_tracker`, not directory-exists** (§ Resume Revisions). `jsa packet` is fail-if-exists; `jsa generate` re-enters a bare directory.
- **Generate is a structured patch over the template library through a render loop, and never tailors blind** (§ Resume Revisions). `claude-fable-5` at `medium`; the model picks `base`, submits ops to `render_resume`, iterates to the two-page budget; `docx_patch.apply_patch` stays deterministic. The library expands outward (`new_family`), never force-fits. A `NULL` JD is skipped unless a hand-filled `job_posting.md` exists, which is never overwritten. The summary register is enforced structurally; candidate-specific rules come from the vault file, never the shared prompt.
- **The bullet library is tailoring ground truth; its sync is incremental via `bullet_sync_runs`** (§ Resume Revisions). Sonnet at `medium`, Read/Edit only; a run records itself only on success; `--baseline` seeds without a model call.
- **The refinement loop is incremental via `prompt_refinement_runs`; the prompt stays standalone; the PR is the only gate** (§ Search Prompt Updates). Opus at `high`; a run records whether or not the PR merges; an errored run records nothing. No sentinels, no pinning test, by choice.
- **Headless agent runs share `agent.py`.** `prompt_path` (cwd, then repo root), `collect_final_text`/`run_agent` (collects text; raises the caller's error class on an `is_error` result so nothing is recorded). Don't reimplement the loop per module.
- **The review comment field must be truly editable** (§ Job Fit Feedback). Prompts go through `prompting.py`; don't regress to bare `input()`.
- **A schema change to `postings` is a migration** (§ Database). `db.migrate_postings_schema` rebuilds; additive columns go through it as `ALTER TABLE ADD COLUMN` before the rebuild; `_POSTINGS_COLUMNS` is the single column list.

## How the pipeline is wired

`cli.py` → `pipeline.run_pipeline` is the cron body: `search.load_prompt` (interpolates the window) → the selected runner (`search/claude_runner.py` or `search/perplexity_runner.py`) → `search.parse_search_output` → per posting: `canonicalize_url` → `db.record_finding` → `db.insert_posting` (idempotent) → for new rows only, `resolve_ats_url` → `fetch_detail` → `db.update_jd_capture`. Step 3 (`review.py`) is a separate deterministic, no-LLM loop: query `NULL decision` rows, open each in Chrome, write the decision back — invoked directly (`uv run jsa review`), never as a slash command (which would reintroduce per-posting token cost).

The local commands reuse those pieces rather than duplicating them:

- `manual.add_posting`: `canonicalize_url` → `db.find_by_canonical_url` (a UX read; the UNIQUE constraint is the real guard) → `resolve_ats_url` → `fetch_detail` / `fetch_jsonld_detail` (best-effort) → `db.insert_posting` → `db.update_jd_capture`.
- `refetch.run_refetch`: `db.rows_for_refetch` → `tracker.read_tracker_index` → `resolve_ats_url` → `fetch_detail` → `db.update_jd_capture` (the same updater the pipeline uses) → `tracker.update_title` → `generate.run_generate` for a row with a stale packet, then remove the old directory.
- `packet.run_packet`: `db.pending_packets` → fail-if-exists `mkdir` → `write_job_posting`.
- `generate.run_generate`: `db.pending_packets` → `load_context` (templates, prompt, vault files, `soffice` preflight) → per row on a worker pool: ensure the directory + `job_posting.md` → `load_tailoring_prompt` → the render-loop agent (`agent.collect_final_text` with an in-process `render_resume` tool that `parse_patch`/`apply_patch`es and renders) → `_maybe_save_new_family` → `render_changelog` → `tracker.run_tracker(--id)` (the Step 4→5 seam, serialized).
- `bullets.run_bullets`: `db.bullet_sync_cutoff` → `resumes_since` (mtime scan of the vault) → `render_resume_blocks` → `load_bullet_sync_prompt` → `agent.run_agent` over the CSV → `db.record_bullet_run`.
- `refine.run_refine`: `db.refinement_cutoff` → `db.rows_for_refinement` → `render_ground_truth` + `render_history` → `load_refine_prompt` → `agent.run_agent` over the checkout → write `.refine_pr_body.md` → `db.record_refinement_run`. The Actions workflow wraps it with the branch, commit, and `gh pr create`.
- `tracker.run_tracker`: `db.pending_tracker` → `build_row` (pure) → `append_row` (shells out to `gws`) → `db.mark_tracked`, per row.
