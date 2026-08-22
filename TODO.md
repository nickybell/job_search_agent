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
- [ ] **Review the Step 4 tests** (`test_generate.py`, `test_docx_patch.py`,
  and the rewritten packet-rebuild section of `test_refetch.py`) — the same
  standing rule. All stubs (tailor callable, soffice, gws) are hermetic; the
  first *real* `jsa generate` run (live model call, real LibreOffice render,
  real Sheet append) is the verification that matters and hasn't happened yet.

## Added 2026-08-21 (Step 4 → `jsa generate`)

- **Step 4 reshaped into `jsa generate`, a local CLI command** (recorded in
  `prd.md` — Resume Revisions, and the "Why Step 4 is local" note in
  Architecture). Replaces the original slash-command-fans-out-subagents design.
  For each eligible row (`Apply AND added_to_tracker = 0`) it builds the packet
  directory + `job_posting.md` (reusing `jsa packet`), one-shots the tailored
  resume (`.docx`/`.pdf`), writes `resume_changelog.md`, and calls
  `jsa track --id` as its final action. Runs headless with a pinned model and
  effort and no inherited session state (like the search runners), so it can be
  detached rather than watched. Stays local — inputs (`base_resume.docx`),
  outputs, and the `gws` grant are all local. **Implemented 2026-08-21**
  (`src/jsa/generate.py` + `src/jsa/docx_patch.py`); `tailoring_prompt.md` is
  a committed placeholder (conservative guidance, load-bearing output
  contract) until the real tailoring instructions are written.

- **[Resolved 2026-08-21] The mkdir-as-guard framing is reconciled with the
  `added_to_tracker` idempotency model.** CLAUDE.md, `packet.py`'s docstrings,
  and `test_packet.py` carry the revised rule (the standalone fail-if-exists
  `mkdir` is retained by design as a cheap safety on that path only);
  `jsa generate` re-enters an existing bare directory and completes it; and
  `refetch._apply_packet_rebuild` now invokes `generate.run_generate` for the
  drifted row — build-new-before-deleting-old for rename safety, failures
  flagged for a manual `jsa generate --id`, never a rollback.

- **[Resolved 2026-08-21] Tailoring mechanism: the structured patch.**
  `jsa generate`'s model call returns a JSON patch (paragraph id → replacement
  + rationale) that `python-docx` applies deterministically to a copy of
  `base_resume.docx` — reproducible reruns, formatting that cannot break, and
  the changelog rendered from the patch itself. The model call is pinned to
  `claude-opus-4-8` (same tier and reasoning as the Friday sweep). Recorded in
  `prd.md` (Resume Revisions).

- **[Resolved 2026-08-21] Changelog format: rendered from the patch.**
  Structured/addressable per-change falls out of the patch mechanism for free
  — `resume_changelog.md` is generated from the applied patch entries (change
  + rationale), not written as a second model artifact that could drift from
  what actually changed.

- **[Resolved 2026-08-21] A `NULL`-JD Apply row is skipped by generate, with a
  hand-fill escape hatch.** generate never tailors blind: no JD → the
  directory is ensured but tailoring and the closing tracker call are skipped,
  the row is flagged (`jsa refetch --id` is the usual fix) and stays in the
  queue. If the packet directory already holds a `job_posting.md` (hand-pasted
  — the path for unsupported-ATS rows refetch can't reach), generate tailors
  from that file instead. Recorded in `prd.md` (Resume Revisions).

- **[Resolved 2026-08-21] A drifted packet is regenerated by refetch invoking
  `jsa generate`, not by the eligibility queue.** On a title/description change
  to a row with an existing packet, `jsa refetch` deletes the stale directory
  and invokes `jsa generate --id` for that row directly — bypassing the
  `added_to_tracker = 0` queue, so a *tracked* but unapplied row is still
  regenerated. `--id` waives generate's tracker-eligibility (mirrors
  `jsa packet --id`); the closing `jsa track` no-ops on the already-tracked row
  (no second Sheet append). refetch builds the new packet before removing the
  old (rename safety), and a failed generate is flagged for a manual re-run,
  never a DB rollback. Recorded in `prd.md` (Posting Drift and Re-fetch). The
  alternative — a filesystem check in the default queue — was not needed.

- [ ] **(Optional, later) Lift the pure tailoring step to Fly.** Only the
  text-in / revisions-out tailoring is cloud-shaped; the packet and
  `.docx`/`.pdf` production and the `gws` write stay local. A genuine
  Steps-1–2 parallel if the itch remains once tailoring is a settled pure
  function — not needed for the app to work, filed for the DevOps-practice
  value. See the "Why Step 4 is local" note in `prd.md`.

## Added 2026-08-22 (resume templates + automated ground-truth loop)

Decisions from the 2026-08-22 design session, recorded in `prd.md` (Resume
Revisions; Search Prompt Updates from "Ground Truth"; Database). The code has
not caught up yet — the checklist below is the gap.

- **[Resolved 2026-08-22] Base model: a curated template library that expands
  outward.** `resume_templates/` (gitignored), one maintained `.docx` per role
  family, formalizing Nicky's nearest-fit practice without copy-of-copy drift.
  The model picks the template per job (pick + rationale recorded in the
  changelog); when no family fits, it starts from the *nearest* template and
  the tailored result is saved back as the new family's template, flagged for
  a curation scrub (it began life tailored to one company). Good
  hand-refinements are folded back into templates by Nicky — explicit
  curation, not copy lineage.
- **[Resolved 2026-08-22] Open Career Format: rejected.** Its one good idea
  (career facts separated from rendered documents) isn't worth owning a
  JSON→docx rendering pipeline for a v0.3 spec; the structured patch exists
  precisely to avoid that layer. The lighter accomplishments-bank variant was
  also declined — tailoring context is the chosen template + the JD, nothing
  more.
- **[Resolved 2026-08-22] The refinement loop is automated, PR-gated, and
  incremental via the database — and the prompt stays standalone.** Weekly
  cron (GitHub Actions), refiner pinned `claude-opus-4-8` at `high` effort,
  output is always a PR (with prd.md synced in the same PR), never a direct
  commit. Scope per run is only ground truth newer than the last run
  (`decided_at` > last `prompt_refinement_runs` row). Hard preference: **no
  watermark or ground-truth reference inside `deep_research_prompt.md`** —
  oscillation across runs is accepted; provenance lives in PR history. The
  refiner treats explicit feedback AND manual adds (recall set) AND implicit
  patterns mined from stored Apply/Skip JDs as first-class inputs. **No
  sentinels and no pinning test** (clarified 2026-08-22): the human PR review
  is the protection for the contract/window/liveness machinery; the
  do-not-touch list stays instructional, in `refine_search_prompt.md`.

Implementation checklist:

- [ ] **Seed `resume_templates/`** from the nine tailored resumes in
  `~/Documents/Job Applications/` — pick the best 2–4 by role family, scrub
  company-specific phrasing, name by family slug. Needs Nicky's judgment;
  agent-assisted consolidation is a good first pass.
- [ ] **Implement the template library in `jsa generate`**: prompt carries
  every template's numbered text; output contract gains `base` (template
  slug) and optional `new_family`; new-family results are saved back into the
  library and flagged in the changelog; `JSA_RESUME_TEMPLATES_DIR` override;
  update `tailoring_prompt.md`'s slots and contract; tests.
- [ ] **Schema migration**: add `postings.decided_at` (plain `ALTER TABLE ADD
  COLUMN` — no CHECK rebuild needed) set on every decision write
  (`record_decision`, `set_decision`, the manual-add INSERT), plus the
  `prompt_refinement_runs` table. Existing decided rows stay `NULL` =
  incorporated by the manual rounds; re-deciding refreshes the timestamp.
- [ ] **Write `refine_search_prompt.md`**: port the interactive refinement
  prompt (2026-08-16/17 rounds) plus the upgrades — the manual-adds recall
  pass (unsupported-ATS misses accumulate toward the ATS-table escape hatch,
  not prompt edits), JD pattern mining, incremental scope fed by the harness,
  PR-body deliverables replacing AskUserQuestion, minimal-diff stance. Keep
  the do-not-touch guardrails (contract, `{{SEARCH_WINDOW}}`, liveness + ATS
  table) instructional — the PR review is the gate; no sentinels, no pinning
  test.
- [ ] **The GitHub Actions workflow**: weekly schedule; repo secrets for
  Turso + Anthropic (set the *rotated* keys — see the credential-rotation
  item below — never the leaked ones); run the refiner, open the PR via
  `gh`; record the `prompt_refinement_runs` row. Pick the day/hour at setup.

## What needs you (setup before the first run)

- [X] **First live searches.** Run `uv run jsa search --agent claude` and
  `uv run jsa search --agent perplexity` by hand. These are the first real
  validation of Step 1 and of the still-open **B-day liveness parity** question
  (see below). Watch the first few A-day (Opus) runs' spend before trusting the
  cron.
- [X] **Bounded A/B trial — concluded 2026-08-17.** The trial ran Claude and
  Perplexity over the *same* window (`jsa search --both`, since removed) so both
  findings landed in `search_findings`, with `jsa ab-report` joining
  `search_findings` → `postings` for coverage / overlap / Apply precision.
  **Outcome:** Perplexity surfaced materially more qualifying roles — its higher
  unique yield outweighs its lower Apply precision — while Claude still found some
  roles Perplexity missed. So the cron now runs **Perplexity on Mon/Wed/Fri plus
  a weekly Claude 168h sweep on Fridays (Claude first)**, encoded in
  `pipeline.CRON_SCHEDULE`; the day-of-year alternation and the `--both`
  scaffolding are gone. `search_findings` / `jsa ab-report` stay, since Friday
  still runs both agents (over different windows) and the telemetry keeps
  accruing.
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
  2026-08-10 go-live). The image entrypoint is `jsa cron`, which self-gates against
  `pipeline.CRON_SCHEDULE` — **Perplexity Mon (72h) / Wed (48h) / Fri (48h), plus
  a weekly Claude 168h sweep on Fridays** — and no-ops on other days — Fly's
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
- [ ] **Backfill packets for already-tracked Apply rows** once `jsa generate`
  lands. The interim track-on-Apply trigger put most Apply rows in the Sheet
  before any packet existed, and generate's default queue is Apply +
  *untracked* — so those rows are invisible to it. Run `jsa generate --id
  <row>` once for each tracked-but-unapplied row you still intend to submit.
- [ ] **Publish the OAuth consent screen** in the `job-search-agent-502402` GCP
  project ("In production"), so the `gws` refresh token stops expiring weekly
  and `jsa track` doesn't need re-auth before most runs. Until then, a
  `gws auth login` is the fix whenever `jsa track` reports an expired grant.
- [ ] **Publish (optional).** The repo is intended as a public portfolio piece
  once you're ready; push to a public GitHub remote on your personal account
  (not Keywell). `base_resume.docx` stays gitignored.

## Empty PRD sections to fill

- [ ] **The tailoring instructions (`tailoring_prompt.md`).** The committed
  file is a placeholder: conservative guidance (never invent experience,
  prefer rewording, length-preserving changes) plus the structured-patch
  output contract, which is load-bearing and must survive the rewrite. The
  real instructions are TK.

- [X] **Search Prompt Updates from "Ground Truth"** — *section filled
  2026-08-22.* The mechanism is now fully designed in `prd.md`: an automated
  weekly PR-gated loop (GitHub Actions, `claude-opus-4-8` at `high`),
  incremental via `decided_at` / `prompt_refinement_runs`, with the human PR
  review as the guardrail (no sentinels, no pinning test — by choice).
  Implementation is the 2026-08-22 checklist above.
  - Both fit-criteria initially deferred from this round were **resolved 2026-08-16**
    after Nicky confirmed they're discernible from the JD's "who we are" front
    matter: a **company-type filter** (product companies only; consulting /
    services / PE-holdco excluded) and a **hands-on-vs-teaching** reframe
    (content-production / Ivory-Tower roles demoted to a negative signal, direct
    customer-adoption work made the lead positive). See the refinement-round
    record in `prd.md`.

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
- **[Resolved 2026-08-21] `.docx`/`.pdf` tooling for Step 4** — `python-docx`
  applies the structured patch; LibreOffice headless (`soffice --headless
  --convert-to pdf`, already installed at `/opt/homebrew/bin/soffice`) renders
  the PDF. Word/`docx2pdf` was considered and rejected: higher fidelity in
  principle, but it drives Word over AppleScript, which is flaky headless.
