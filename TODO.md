# TODO

Open items only — this file is triage, not history. Decisions and their
reasoning live in `prd.md`; what happened lives in the git log.

## Before the first real `jsa generate` run

- [ ] **Review the seeded resume templates** (`resume_templates/*.docx`).
  `ai-enablement.docx` needs the closest read — it was derived, not copied —
  and check the Skills lines and summaries of `customer-education.docx` and
  `customer-success.docx`. Two Apply rows are queued and waiting (GitLab
  id 59, Sigma Computing id 62).
- [ ] **Write the real tailoring instructions** in `tailoring_prompt.md`. The
  committed guidance is a deliberately conservative placeholder; the
  structured-patch output contract at the bottom is load-bearing and must
  survive the rewrite.

## One-time setup

- [ ] **Rotate the leaked credentials.** The Turso auth token, Anthropic API
  key, and Perplexity API key were pasted in plaintext into a Claude Code
  transcript during setup. Rotate all three, then set the fresh values with
  `fly secrets set --stage` (plain `fly secrets set` fails on this app — it
  tries to auto-deploy against a release that doesn't exist):
  - Turso: `turso db tokens invalidate job-search-agent` → `turso db tokens create job-search-agent`
  - Anthropic: revoke the old key in the console, issue a new one
  - Perplexity: revoke the old key in settings, issue a new one
- [ ] **Wire up the refinement workflow on GitHub.** Add the repo secrets
  `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and `ANTHROPIC_API_KEY` (the
  rotated values, never the leaked ones); enable Settings → Actions → "Allow
  GitHub Actions to create and approve pull requests"; adjust the cron in
  `.github/workflows/refine-search-prompt.yml` if Sunday 13:00 UTC isn't the
  slot you want. Smoke-test from the Actions tab via workflow_dispatch —
  with no new ground truth it exits quietly, so a real exercise needs a
  freshly decided posting.
- [ ] **Publish the OAuth consent screen** in the `job-search-agent-502402`
  GCP project ("In production") so the `gws` refresh token stops expiring
  every 7 days. Until then, `gws auth login` is the fix whenever a command
  reports an expired grant.
- [ ] **Backfill packets for already-tracked Apply rows.** Rows tracked
  before their packets existed are invisible to generate's default (Apply +
  untracked) queue — run `jsa generate --id <row>` once for each
  tracked-but-unapplied row you still intend to submit.

## Testing

- [ ] **Write the test suite — you or an adversarial agent, never the
  implementing agent.** The repo deliberately ships without tests: a suite
  developed in the same loop as the code only mirrors what that loop already
  believed, so it must be authored separately to be an objective record of
  whether the code works. The seams are in place: pure I/O-free logic
  modules, `TURSO_DATABASE_URL=file:…`, `JSA_GWS_BIN`, `JSA_SOFFICE_BIN`,
  `JSA_PACKETS_DIR`, `JSA_RESUME_TEMPLATES_DIR`, and the injectable
  `tailor`/`runner` callables in `generate.py`/`refine.py`.

## Later / optional

- [ ] **Publish the repo** as a public portfolio piece on your personal
  GitHub account (not Keywell) — after the credential rotation above.
  `base_resume.docx` and `resume_templates/` stay gitignored.
- [ ] **Lift the pure tailoring step to Fly.** Only the text-in /
  revisions-out tailoring is cloud-shaped; the packet, `.docx`/`.pdf`
  production, and the `gws` write stay local. Not needed for the app to work
  — filed for the DevOps-practice value. See "Why Step 4 is local" in
  `prd.md`.
