# PRD TODO — before planning-ready

Gaps to close in `prd.md` before it's ready for implementation planning.

## Implementation status (2026-07-21)

**Steps 1–3 are implemented** on the `implement-steps-1-3` branch (Python, uv,
`src/jsa/`): the A/B search runners, canonical-URL idempotent insert, full-JD
capture from all four supported ATS platforms (verified live), the deterministic
review loop, the `jsa` CLI, and the Fly.io deployment (Dockerfile + fly.toml).

## Added 2026-08-10 (`feat/tracker-manual-add-review-ux`)

- **Step 5, the tracker write** — `jsa track` appends `Apply` postings to the
  Google Sheet via the local `gws` CLI and sets `added_to_tracker = 1`, only
  ever after the Sheets API confirms the append. `--dry-run` previews, `--id`
  narrows to one row. Step 4 will call it as its final action.
- **The direct job-add path** — `jsa add <URL>` (see the resolved gap below).
- **Review UX** — decisions are now revisable (`:a`/`:s` at the feedback prompt,
  `b` to reopen the previous posting, plus an end-of-backlog amend offer), and
  the prompts have real line editing via `prompt_toolkit`.
- **`jsa refetch`** — reconciles stored postings against their ATS record.
  Prompted by a real case: a Stepful req was retitled in place (GTM → Sales)
  under an unchanged URL and `publishedAt`, confirmed against an Internet
  Archive snapshot. Capture-once is still right (Step 4 runs days later, so the
  JD must be captured while the posting is alive) but needed a way back.
- **Tracker append switched to `insertDataOption = OVERWRITE`.** `INSERT_ROWS`
  was silently destroying the sheet: each appended row landed outside the
  `Status` validation and conditional-format ranges (losing the dropdown) and
  shifted those ranges down one, dragging them permanently off the data — after
  two appends the rules covered rows 4–1002 while the data sat in rows 2–3. The
  live sheet has been repaired (ranges re-anchored to row 2, dropdown restored
  on rows 2–3, inherited header styling cleared).
- **A `pytest` suite** (81 tests) covering Steps 3 and 5, the manual-add path,
  and the `search_agent` CHECK migration. Hermetic: throwaway `file:` SQLite, a
  stubbed ATS fetch, and a stub `gws` binary via `JSA_GWS_BIN`.

**Deferred, tracked for later:**
- Step 4 (per-job resume revisions) — blocked on the TK tailoring instructions
  and the `.docx`/`.pdf` toolchain decision (below).
- The ground-truth prompt-refinement cron (mechanism still undesigned — see below).
- Tests for the Step 1–2 search runners — the only substantial untested area
  left; exercising them means real API spend, so they stay manual for now.

## Added 2026-08-16 (`feat/packet-cmd-refetch-scope-tracker-id`)

- **`jsa packet`** — Step 4's steps 1–2 (the fail-if-exists `mkdir` guard and
  `job_posting.md`) as a deterministic CLI command. Default queue is Apply +
  untracked; `--id` waives the tracker condition (the interim track-on-Apply
  trigger tracks rows before packets exist) but never the Apply one.
- **`jsa refetch` rescoped** to postings where drift could still change an
  action: `Apply` rows that are absent from the tracker Sheet OR have no
  `Date Applied` there. A failed index read aborts the default scope;
  `--id`/`--all` select unconditionally, so for them the read is best-effort.
- **The Sheet relationship reframed (superseding "write-only"/"append-only").**
  Nicky's articulation: two concepts had been conflated — where authority
  lives vs. which operations are permitted. The principle is that the database
  is the source of truth for posting data and the tracker is its human-readable
  projection plus his workspace columns (`Date Applied`, `Status`, which only
  he writes). Consequences implemented: refetch reads the Sheet index to scope
  itself, and **propagates a corrected Title to the tracker row** when the job
  sits there unapplied (DB first, Sheet second; a failed Sheet write degrades
  to a flagged hand-fix). Recorded in `prd.md` (Application Tracker).
- **Stale packets are rebuilt, not archived.** When refetch changes a title or
  JD on a row with an existing packet directory, the directory (resume
  included) is deleted and rebuilt — packets are derived artifacts, the job
  "no longer exists as it was packeted" (Nicky), and the missing resume is
  Step 4's regenerate signal. An `Archived/` tree was considered and rejected.
  Guards: exact-path delete only, never clobbers a distinct dir at the new
  name, location-only changes touch nothing, dry-run only reports.
- **Tracker sheet gained an `ID` column** (now A:H, ID first) so Sheet rows
  join back to `postings.id` without URL comparison. The live sheet was
  restructured in place: column inserted, header formatted, existing rows
  backfilled by URL match; data validation / conditional formatting verified
  intact after the shift.
- [ ] **Review the new/updated tests** (`test_packet.py`, refetch scope tests,
  tracker A:H tests) — same standing rule as the suite below: agent-authored
  tests are provisional until you've read them.

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
- [X] **Re-authorize `gws`.** *Done 2026-08-11.* The grant had expired
  (`invalid_grant`, exit 2, affecting every `gws` call, not just Sheets); after
  `gws auth login` the live tracker append is verified end-to-end. Expect this
  to recur: the personal OAuth client is in **External / Testing** publishing
  status, where Google expires refresh tokens after 7 days.
- [ ] **Review the unit tests.** The `pytest` suite was written by the agent in
  one pass, which makes it a mirror of what the agent already believed rather
  than an independent check. Read through `tests/` and push back on anything
  that asserts the wrong thing, over-fits the implementation, or misses a case
  worth pinning. *(This also closes the "deferred by choice" note above: the
  suite landed during the 2026-08-11 feature work, which reversed that
  deferral — it should have been asked about first.)*
- [ ] **Publish the OAuth consent screen** in the `job-search-agent-502402` GCP
  project ("In production"), so the `gws` refresh token stops expiring weekly
  and `jsa track` doesn't need re-auth before most runs. Until then, a
  `gws auth login` is the fix whenever `jsa track` reports an expired grant.
- [ ] **Publish (optional).** The repo is intended as a public portfolio piece
  once you're ready; push to a public GitHub remote on your personal account
  (not Keywell). `base_resume.docx` stays gitignored.

## Empty PRD sections to fill

- [ ] **Search Prompt Updates from "Ground Truth"** — the mechanism by which `fit_feedback` refines the Step 1 search prompt on the daily cron.

## Cross-cutting gaps not owned by any section

- [X] **Direct job-add path** — *Resolved 2026-08-10.* Specified in `prd.md`
  ("Direct Job Add") and implemented as `jsa add <URL>` in `src/jsa/manual.py`.
  It reuses Step 2's canonicalize → idempotent insert → JD-fetch rather than
  forking it, so a hand-added row is indistinguishable downstream apart from
  `search_agent = 'manual'`. Two deliberate departures: no `search_findings`
  row (that table is A/B search telemetry, and a supplied posting would inflate
  an agent's coverage), and an unsupported ATS is *not* a rejection (the
  four-ATS rule is a liveness proxy for postings an agent found on its own —
  the user has already vouched for this one, so it inserts with a `NULL
  jd_markdown`).
- [ ] **`.docx`/`.pdf` tooling** for Step 5 (pandoc? docx library + LibreOffice?) — affects local setup.
