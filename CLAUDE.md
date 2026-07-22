# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status: pre-implementation

This repository currently contains **only design artifacts** — there is no code, build system, or test suite yet. Do not fabricate build/lint/test commands; they don't exist. The work so far lives in:

- `prd.md` — the product requirements document and **source of truth** for what this system does. Read it in full before proposing implementation work.
- `TODO.md` — open gaps that must be closed in `prd.md` before it is planning-ready (the ground-truth prompt-update mechanism, the runtime/language choice, `.docx`/`.pdf` and HTML→Markdown tooling, and other open decisions).
- `base_resume.docx` — Nicky's canonical base resume; the input to the resume-revision flow (Steps 4a/5).

Not a git repository yet. Offer `git init` before any commit work.

## What this system is

A personal job-search agent (built on the Claude Agent SDK) that runs a daily search for Customer Education / Enablement / Community roles, stores postings idempotently, collects the user's fit feedback, and generates per-job resume revisions. A learning loop feeds fit feedback back into the search prompt over time ("ground truth" refinement).

The pipeline is a numbered 5-step flow defined in `prd.md`; internalize that numbering (Steps 1–5, with 4a/4b) since the rest of the doc and `TODO.md` reference it constantly.

## Architecture the code must respect

These are load-bearing decisions from `prd.md` — a future implementation should not quietly deviate from them:

- **Cloud/local split.** Steps 1–2 (daily search, idempotent insert) and the prompt-refinement cron run **headless on Fly.io**. Steps 3–5 (fit review, resume revisions, Google Sheet write, `.docx`/`.pdf` generation) run **locally as interactive Claude Code sessions**. This split is the reason for the hosted DB (below) and drives where each credential lives (the Perplexity key is a Fly-managed secret; Google Sheets OAuth stays local).

- **One hosted DB, no copies.** The JD database is **Turso (hosted libSQL, SQLite-compatible)**. Both the cloud cron and local sessions connect to the *same* copy — never introduce a second local SQLite file that would diverge.

- **Single `postings` table.** It holds search output, the canonical-URL idempotency key, and fit feedback only. It deliberately does **not** store application state — the Google Sheet is the write-only application tracker, and the agent never reads back from it. Don't mirror sheet/application state into the DB.

- **Single-mechanism insert idempotency — no dedup subsystem.** Dedup is exactly one mechanism: URL canonicalization into a `UNIQUE canonical_url` + `INSERT … ON CONFLICT DO NOTHING`. Because the search prompt requires index-linked ATS URLs, both agents return the same canonical URL for the same req, so this also covers cross-source duplicates — and it is what makes daily/CLI re-runs safe. The stage-2 repost `(title, location)` match and its `duplicate_of` linkage were removed 2026-07-21 (as was the still-earlier Voyage embeddings design) — do not reintroduce a second dedup stage or embeddings; rare reposts surface in Step 3 review and the user handles them.

- **Full-JD capture at insert, not a liveness gate.** Step 2 GETs each surviving posting's public ATS JSON detail record and stores `jd_markdown` (HTML→Markdown) plus the ATS's structured `location` and canonical `title`; the Step 4 `job_posting.md` is built from `jd_markdown`. A failed fetch never excludes a row — the 2026-07-14 no-pipeline-liveness-check decision stands.

- **A/B search alternation.** Even days use Claude Deep Research; odd days use Perplexity Pro Search. Both must return the structured `postings` JSON schema in `prd.md`. Parity is derived from the run date inside the container so one cron entry self-selects.

- **CLI-runnable crons.** The automated Steps 1–2 must also be invocable from the command line with a parameterized time window and search-agent choice.

## Working conventions

- Keep `prd.md` the source of truth. When a design decision is made in conversation, reflect it there; when something is still undecided, it belongs in `TODO.md` as a checkbox, not silently assumed in code.
- Use `AskUserQuestion` liberally for decisions requiring judgment (the user has asked for this globally).