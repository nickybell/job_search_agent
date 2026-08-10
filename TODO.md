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

- [X] **First live searches.** Run `uv run jsa search --agent claude` and
  `uv run jsa search --agent perplexity` by hand. These are the first real
  validation of Step 1 and of the still-open **B-day liveness parity** question
  (see below). Watch the first few A-day (Opus) runs' spend before trusting the
  cron.
- [ ] **Run the bounded A/B trial (~7 cron days).** For the first week, run
  `uv run jsa search --both` (or point the cron at it) so Claude and Perplexity
  search the *same* window each day and both findings land in `search_findings`
  — the day-of-year alternation confounds agent with day and can't A/B. Review
  as normal, then `uv run jsa ab-report` for coverage / overlap / Apply
  precision. Revert the cron to single-agent alternation afterward. Open
  sub-decisions:
  - [ ] Trial length — 7 days assumed; extend if daily volume is thin.
  - [ ] Run the trial locally by hand vs. point the Fly cron at `--both` for the
    week (doubles daily spend: ~A-day + B-day cost per run, ~$8.13 + ~$4.44 at
    the 7-day-window rates measured 2026-08-06). **Currently the `jsa cron`
    entrypoint runs `--both` on every Mon/Wed/Fri search day** — revert it to the
    single-agent day-of-year alternation (`select_agent_for_date`) when the trial
    ends.
  - [ ] Success metric weighting — pure Apply-precision vs. unique-Apply yield
    (which agent is the *sole* source of good roles) vs. cost-per-Apply.
- [X] **Fly.io deployment.** `fly auth signup`/`login`, `fly launch --no-deploy`
  (reuses the committed `fly.toml`), `fly secrets set --stage` the four secrets
  (`--stage` is required — this app has never gone through a `fly deploy`
  release, so plain `fly secrets set` fails trying to auto-deploy against a
  release that doesn't exist), then smoke-test with a one-off
  `fly machine run . --rm --vm-memory 1024` before creating the scheduled
  machine (`fly machine run . --schedule daily --restart on-fail --vm-memory
  1024`) at your intended ET morning hour. **`--vm-memory 1024` is required** —
  `fly machine run` talks to the Machines API directly and does not read
  `fly.toml`'s `[[vm]]` block, so it silently defaults to `shared-cpu-1x`
  (256MB), which isn't enough for the Claude Agent SDK's bundled CLI subprocess
  (it hangs on `initialize` rather than failing loudly — this is what broke the
  2026-08-10 go-live). The image entrypoint is `jsa cron`, which self-gates to a
  **Mon (72h) / Wed (48h) / Fri (48h)** cadence and no-ops on other days — Fly's
  fuzzy `daily` schedule has no weekday selector, so the weekday+window logic
  lives in the container.
- [ ] **Rotate the leaked credentials before publishing.** The Turso auth
  token, Anthropic API key, and Perplexity API key were pasted in plaintext
  into a Claude Code transcript during setup (2026-08-09). Rotate all three,
  then `fly secrets set` the fresh values:
  - Turso: `turso db tokens invalidate job-search-agent` → `turso db tokens create job-search-agent`
  - Anthropic: revoke the old key in the console, issue a new one
  - Perplexity: revoke the old key in settings, issue a new one

  Do this before the repo goes public (below). Use `fly secrets set --stage`
  (see the Fly.io deployment item above) — plain `fly secrets set` fails on
  this app.
- [X] **2026-08-10 go-live incident.** The local cron
  (`~/.jsa-cron/jsa-golive.sh`, one-time, self-removing) fired correctly at 6am
  ET and created the scheduled machine, but the machine crashed twice
  (`Control request timeout: initialize` from the bundled Claude Code CLI) and
  hit Fly's max-restart count. Root cause: the machine ran at the
  `fly machine run` default of 256MB RAM, not the 1GB in `fly.toml`'s `[[vm]]`
  block, because `fly machine run` doesn't read that block (see above).
  Separately, the script's crontab self-removal failed
  (`crontab: tmp/tmp.9566: Operation not permitted`) because `cron` lacked
  macOS Full Disk Access — granted now. Fixed by rotating secrets with
  `--stage`, destroying the crashed machine, running an `--rm` catch-up job,
  and re-anchoring the go-live cron to the next MWF morning with
  `--vm-memory 1024` baked in.
- [ ] **Publish (optional).** The repo is intended as a public portfolio piece
  once you're ready; push to a public GitHub remote on your personal account
  (not Keywell). `base_resume.docx` stays gitignored.

## Empty PRD sections to fill

- [ ] **Search Prompt Updates from "Ground Truth"** — the mechanism by which `fit_feedback` refines the Step 1 search prompt on the daily cron.

## Cross-cutting gaps not owned by any section

- [ ] **Direct job-add path** — the Database section mentions Nicky providing jobs directly, but the ingestion route (does it run the canonical-URL idempotent insert and the full-JD fetch? where?) is unspecified. *(Deferred out of the Steps 1–3 implementation plan, 2026-07-21; natural shape is a CLI subcommand reusing Step 2's canonicalize → insert → JD-fetch on a user-supplied ATS URL.)*
- [ ] **`.docx`/`.pdf` tooling** for Step 5 (pandoc? docx library + LibreOffice?) — affects local setup.
