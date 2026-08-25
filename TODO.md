# TODO

Open items only — this file is triage, not history. Decisions and their
reasoning live in `prd.md`; what happened lives in the git log.

## Before the first real `jsa generate` run

- [ ] **Review the seeded resume templates** (`resume_templates/*.docx`).
  `ai-enablement.docx` needs the closest read — it was derived, not copied —
  and check the Skills lines and summaries of `customer-education.docx` and
  `customer-success.docx`. Two Apply rows are queued and waiting (GitLab
  id 59, Sigma Computing id 62).
## One-time setup

- [ ] **Set `JSA_TRACKER_SPREADSHEET_ID` in `.env`.** The tracker Sheet id
  was removed from the code and `prd.md` for publication (it names a personal
  spreadsheet); every Sheet-touching command now requires the env var and
  fails with a pointer until it is set.

- [ ] **Publish the OAuth consent screen** in the GCP project behind the
  `gws` OAuth client ("In production") so the `gws` refresh token stops expiring
  every 7 days. Until then, `gws auth login` is the fix whenever a command
  reports an expired grant.
- [ ] **Backfill packets for already-tracked Apply rows.** Rows tracked
  before their packets existed are invisible to generate's default (Apply +
  untracked) queue — run `jsa generate --id <row>` once for each
  tracked-but-unapplied row you still intend to submit.
- [ ] **Rotate Claude auth from the API key to the subscription OAuth token.**
  Every Claude service drives the Claude Agent SDK (the deep-research search,
  `jsa generate`, `jsa refine`), so all read `CLAUDE_CODE_OAUTH_TOKEN` (the
  `claude setup-token` output) instead of `ANTHROPIC_API_KEY`. Setting both is
  a trap — the CLI prefers `ANTHROPIC_API_KEY` and 401s on an OAuth value — so
  **unset the old key wherever you set the new one**. Perplexity is untouched
  (`PERPLEXITY_API_KEY` stays).
  - [ ] Local `.env`: replace the `ANTHROPIC_API_KEY` line with
    `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…`.
  - [ ] Fly.io cron: `fly secrets set CLAUDE_CODE_OAUTH_TOKEN=…`, then
    `fly secrets unset ANTHROPIC_API_KEY`.
  - [ ] GitHub Actions: add the `CLAUDE_CODE_OAUTH_TOKEN` repo secret and
    delete `ANTHROPIC_API_KEY` (the refine workflow already reads the new name).

## Testing

- [ ] **Write the test suite — you or an adversarial agent, never the
  implementing agent.** The repo deliberately ships without tests: a suite
  developed in the same loop as the code only mirrors what that loop already
  believed, so it must be authored separately to be an objective record of
  whether the code works. The seams are in place: pure I/O-free logic
  modules, `TURSO_DATABASE_URL=file:…`, `JSA_GWS_BIN`, `JSA_SOFFICE_BIN`,
  `JSA_PACKETS_DIR`, `JSA_RESUME_TEMPLATES_DIR`, and the injectable
  `tailor`/`runner` callables in `generate.py`/`refine.py`.

## Search recall — unsupported-platform evidence

Manual adds the search could not have emitted because the posting lives off
the four supported ATS platforms. Accumulates as evidence for the four-ATS
table's additive escape hatch (see `prd.md` Daily Search); not a prompt
defect, so never encoded as a prompt edit.

- [ ] **Agave — Customer Solutions Engineer** (id 48, manual add, decided
  Apply). Supplied URL is a Y Combinator jobs-board page
  (`ycombinator.com/companies/agave/jobs/…`), not Greenhouse / Lever / Ashby /
  Rippling, so it is out of scope for the search by construction. If YC-hosted
  startup listings keep recurring as misses, check whether YC's board exposes
  a usable public JSON list endpoint worth adding to the ATS table. (Secondary,
  unencoded: "Customer Solutions Engineer" is an implementation/CS/solutions
  hybrid — the JD explicitly states no technical skills required — and is not
  one of the seeded target titles. Left unencoded to avoid pulling in the
  technical solutions/field-engineer roles the user consistently skips, e.g.
  MinIO id 32.)

## Later / optional

- [ ] **Give the refiner the data its adherence audit's version caveat
  needs.** The audit asks the refiner to weigh whether a row was decided
  under a prompt version that predated the rule it seems to violate, but the
  rendered ground truth carries no found-date and the agent has no git
  access — so today that judgment is a guess. Fix is small: include each
  row's `first_seen`/created date in `refine.py`'s ground-truth rendering
  (and possibly the prompt's recent edit dates in the refine template's
  interpolation). Deferred from the adherence-audit revision — prompt edits
  shipped first.

- [ ] **Wire location into the emit-time verification step, if location
  misses recur.** The prompt states the location filter up top but leaves it
  disconnected from the ATS list-endpoint fetch the agent actually performs
  for liveness — so an unambiguously out-of-area role can slip through
  (Ocrolus "Mortgage Technical Enablement Manager", id 64, surfaced despite a
  clear "New York, United States" location that the Greenhouse list record
  carried). Surgical fix when there's a pattern: make confirming the
  `location`/offices field on that same record — and applying the location
  filter — an explicit emit-time step in `deep_research_prompt.md`. Not acted
  on now: n=1, and adding words rarely fixes a model ignoring an already-clear
  rule. Let `jsa refine` accumulate the signal first; this is the lever if it
  keeps happening.

- [ ] **Publish the repo** as a public portfolio piece on your personal
  GitHub account — after the credential rotation above. `base_resume.docx`,
  `resume_templates/`, and `IDEAS.md` stay untracked.
