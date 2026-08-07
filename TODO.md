# PRD TODO — before planning-ready

Gaps to close in `prd.md` before it's ready for implementation planning.

## Implementation status (2026-07-21)

**Steps 1–3 are implemented** on the `implement-steps-1-3` branch (Python, uv,
`src/jsa/`): the A/B search runners, canonical-URL idempotent insert, full-JD
capture from all four supported ATS platforms (verified live), the deterministic
review loop, the `jsa` CLI, and the Fly.io deployment (Dockerfile + fly.toml).

**Deferred, tracked for later:**
- Steps 4–5 (per-job resume revisions; write-only Google Sheet tracker).
- The ground-truth prompt-refinement cron (mechanism still undesigned — see below).
- The direct job-add path (below).
- Test suite + CI hardening: pure logic (`canonicalize`, `naming`, `parse`,
  `ats.resolve`) was written I/O-free specifically so a `pytest` suite drops in
  without refactoring; deferred by choice to get a dev version running first.

## What needs you (setup before the first run)

- [ ] **First live searches.** Run `uv run jsa search --agent claude` and
  `uv run jsa search --agent perplexity` by hand. These are the first real
  validation of Step 1 and of the still-open **B-day liveness parity** question
  (see below) — compare output quality via the `search_agent` column. Watch the
  first few A-day (Opus) runs' spend before trusting the cron.
- [ ] **Fly.io deployment.** `fly auth signup`/`login`, `fly launch --no-deploy`
  (reuses the committed `fly.toml`), `fly secrets set` the four secrets, then
  smoke-test with a one-off `fly machine run . --rm` before creating the
  scheduled machine (`fly machine run . --schedule daily`) at your intended ET
  morning hour.
- [ ] **Publish (optional).** The repo is intended as a public portfolio piece
  once you're ready; push to a public GitHub remote on your personal account
  (not Keywell). `base_resume.docx` stays gitignored.

## Empty PRD sections to fill

- [ ] **Search Prompt Updates from "Ground Truth"** — the mechanism by which `fit_feedback` refines the Step 1 search prompt on the daily cron.

## Cross-cutting gaps not owned by any section

- [ ] **Direct job-add path** — the Database section mentions Nicky providing jobs directly, but the ingestion route (does it run the canonical-URL idempotent insert and the full-JD fetch? where?) is unspecified. *(Deferred out of the Steps 1–3 implementation plan, 2026-07-21; natural shape is a CLI subcommand reusing Step 2's canonicalize → insert → JD-fetch on a user-supplied ATS URL.)*
- [ ] **`.docx`/`.pdf` tooling** for Step 5 (pandoc? docx library + LibreOffice?) — affects local setup.
