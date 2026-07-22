# Job Search Agent for Customer Education and Community Roles

This document outlines the goals and features of the Job Search Agent using the Claude Agent SDK.

## Purpose

The purpose of this agent is to:

1. Conduct a daily search for job postings that fit the desired criteria,
2. Store those job postings (including the full job description, fetched from the posting's own ATS) in a database exactly once — the insert is idempotent, so re-surfaced postings no-op,
3. Obtain feedback on job fit from the user,
4. Generate resume revisions (from a canonical "base" from which each job's resume branches) for job postings to which the user wishes to apply, and
5. Write the job posting to an application tracker as a Google Sheet (for user's tracking - there is never a read from the sheet by the agent).

The agent will also update the prompt in Step 1 as a daily cron job to reflect the developing "ground truth" from Step 3.

## Requirements

### Automation

Steps 1 and 2 (daily search and writing to database) are automated cron jobs. However, they should be runnable from the command line with a parameterized time window for the search and the search agent (Claude or Perplexity). Step 3 is "human-in-the-loop" with user input on each job via a CLI. Steps 4 and 5 are triggered in Claude Code using a slash command.

### Daily Search

Each morning, run a search for new job postings in the last 48 hours (or from the command line with a parameterized time window and search agent). The search will alternate by day:

**A Day:** Conduct the search using Claude Deep Research.

- **Model and effort:** `claude-opus-4-8` with `output_config: {effort: "xhigh"}` and adaptive thinking (`thinking: {type: "adaptive"}`), generous `max_tokens` headroom (≥ 64K). Rationale: long-horizon agentic web research with hard liveness-verification gates rewards Opus-tier instruction-following and thoroughness; at ~15 A-day runs/month the cost delta vs. Sonnet is negligible, and latency is irrelevant on a cron. `xhigh` is the documented sweet spot for agentic search (lower effort consolidates/skips tool calls — wrong for a task whose value is exhaustive source-checking). Fable 5 was considered and rejected: 2× Opus pricing, refusal-classifier and data-retention overhead a headless cron doesn't want, and no capability need above Opus here. Since Steps 1–2 run on the Claude Agent SDK (the Claude Code harness), `xhigh` is already its default effort on Opus — implementation mostly amounts to pinning the model string.

**B Day:** Conduct the search using Perplexity Pro Search. This will require an SDK-to-SDK connection with the API key supplied as a Fly-managed secret (`fly secrets set PERPLEXITY_API_KEY=…`), injected into the Machine as an environment variable at runtime.

- **Model and mode:** `sonar-pro` with Pro Search enabled — `web_search_options: {search_type: "pro"}`, which **requires `stream: true`** (a non-streaming request silently falls back to standard single-step search, so the runner must stream and assemble the response). Rationale: the B-day task is a broad discovery sweep with a narrow structured output, and Pro Search mode supplies the capability that actually matters — model-orchestrated multi-step `web_search` plus `fetch_url_content`, which the liveness gate's ATS list-endpoint checks depend on. `sonar-deep-research` was considered and rejected: it is built for deep synthesis of one topic (a long cited report — wasted work for a job whose entire output is a JSON array), and its pricing stacks metered layers the caller doesn't control — $5/1k searches with the model deciding how many to run, plus citation tokens (~$2/1M) and reasoning tokens (~$3/1M) — yielding reported real-world costs of ~$0.30–$1.30+ per query with high variance, which compounds on a daily cron. `sonar-pro` bills predictably: $3/$15 per 1M input/output tokens plus a per-1k-request fee tiered by search context size ($6 low / $10 medium / $14 high). Whether Pro Search's `fetch_url_content` reliably performs the exact ATS index-membership check is still open — see "B-day liveness parity" in `TODO.md`.

Both API calls should require a structured JSON output:

```
{
  "type": "object",
  "properties": {
    "postings": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "company": {
            "type": "string"
          },
          "title": {
            "type": "string"
          },
          "url": {
            "type": "string"
          },
          "date_posted": {
            "type": "string"
          }
        },
        "required": [
          "company",
          "title",
          "url"
        ]
      }
    }
  },
  "required": [
    "postings"
  ]
}
```

The deep research prompt for both agents is stored as a .md file (currently, `deep_research_prompt.md`). The file is a **template**: it contains a `{{SEARCH_WINDOW}}` placeholder that the cron (or CLI invocation) interpolates with the parameterized time window before the prompt is sent (default: the last 48 hours). The prompt also carries the full output-format contract inline (the `postings` JSON schema and field semantics), since Claude Deep Research has no server-side structured-output parameter — Perplexity's structured output is enforced at the API layer as well, but both read the same in-prompt contract.

**Liveness is enforced in the prompt, not the pipeline.** The search prompt treats "open and verifiable on the employer's own careers/ATS job index" as a hard gate (its recall-first framing applies only to role-fit judgment), requires index-linked URLs over copied deep links, and forbids guessed `date_posted` values.

**Supported-ATS membership is an inclusion criterion, checked via JSON list endpoints — not a per-platform verification effort.** Smoke testing showed the naive index check fails both ways: an unlisted ATS detail page (e.g. a Greenhouse "private"/pulled post) keeps rendering a fully open-looking page with an application form, while the company's public index is a client-side JS shell a plain fetch can't read. The prompt therefore restricts eligible postings to the four ATS platforms with public, unauthenticated JSON list endpoints — Greenhouse (`boards-api.greenhouse.io`), Lever (`api.lever.co`), Ashby (`api.ashbyhq.com/posting-api`), Rippling (`ats.rippling.com/api/v2`) — and verifies liveness by GET-fetching that endpoint (board slug derived from the posting's own URL), with presence-in-list as the only accepted liveness signal and absence (for any reason) meaning exclude. Postings on any other platform — Workday (whose index is a POST behind bot management), custom careers sites, unrecognized ATS — are **out of scope by construction, not investigated case-by-case**: spending research turns probing exotic hosts for verification paths trades away discovery effort, and a posting that can't be resolved to a supported ATS detail record also can't be processed by Step 2's full-JD fetch. The escape hatch is additive — other ATS with public JSON list endpoints (SmartRecruiters, Workable, Recruitee) can be added to the table if review shows meaningful roles being missed; see the coverage question in `TODO.md`.

**Full-JD capture at insert.** For each posting that survives the idempotent insert (i.e. each genuinely new row), Step 2 GET-fetches the posting's own public ATS JSON *detail* record — the same endpoint family the liveness gate already restricts every surviving posting to — and stores the full job description as `jd_markdown`, converting the description HTML to Markdown (Greenhouse `content`, which arrives HTML-entity-escaped and must be unescaped first; Lever `description`; Ashby `descriptionHtml`; Rippling's v2 job record). The fetch also captures the ATS record's structured `location` and canonical `title`; on success the ATS `title` replaces the search agent's transcription, since the two search agents transcribe titles from different boards inconsistently and the ATS-canonical title is what the tracker row and the application-packet naming should carry. Capture happens at insert time, while the posting is alive, because Step 4 may run days later — after a posting has been pulled. Two deliberate boundaries: (1) this is **content capture, not a liveness gate** — a failed fetch never excludes a row (the 2026-07-14 no-pipeline-liveness-check decision stands); the row inserts with the search agent's values and a `NULL` `jd_markdown`/`location`. (2) The full posting is deliberately **not** requested from the search agents' structured output: an LLM asked to reproduce a JD "verbatim" paraphrases and truncates, and multi-KB JSON string fields inflate output cost and truncation risk.

### Insert Idempotency

**Approach: URL canonicalization into a `UNIQUE` constraint**

1. Before insert, reduce each posting's `url` to a canonical form: lowercase the host, strip tracking/session parameters (denylist including `utm_*`, `gh_src`, `currentJobId`, and other `ref`/session params), drop URL fragments, and normalize trailing slashes.
2. Store it as `canonical_url` with a `UNIQUE` constraint and write via `INSERT … ON CONFLICT DO NOTHING`, so re-encountering a known posting is a no-op.

This single mechanism is what makes the pipeline safe to re-run — overlapping daily 48-hour search windows, a retried cron, and CLI invocations with wide time windows all re-surface postings already in the DB, and every one no-ops on the constraint — and it carries the cross-source dedup load too, since both search agents converge on the same index-linked ATS URL.

**Storage / schema notes:**

- Derive `normalized_company` and `title_slug` into filesystem-safe form at write time (Step 2), so the Step 4 subagent composes its `~/Documents/Job Applications/…` paths directly from the row without re-sanitizing. This is also what lets the Step 4 `mkdir` guard treat *any* failure as "already exists": no path-hostile character can reach it to cause a spurious failure.

### Database

The Turso (libSQL) database holds the structured search output, the insert-idempotency signal (`canonical_url`), and the user's fit feedback — everything the "ground truth" learning loop needs. It deliberately does *not* store application state: the Google Sheet is the application tracker (Step 5), and since the agent never reads back from that sheet, mirroring status or resume-branch data locally would be write-once, never-read duplication. A single core `postings` table covers everything the DB is responsible for.

**`postings` columns (indicative):**

| Column | Purpose |
| --- | --- |
| `id` | Primary key. |
| `company`, `title`, `url`, `date_posted` | Fields from the structured search output (see Daily Search schema). `title` is overwritten with the ATS record's canonical title when the full-JD fetch succeeds; the others stay verbatim. |
| `canonical_url` | URL after tracking-param stripping; `UNIQUE`. The idempotency key: `INSERT … ON CONFLICT DO NOTHING` no-ops re-encountered postings. |
| `normalized_company` | Title case and suffix-stripped company name. Rendered filesystem-safe (path-hostile characters stripped/replaced), since it forms part of the Step 4 application-packet directory and filenames. |
| `title_slug` | A filesystem-safe rendering of `title` (path separators and other reserved characters stripped/replaced, length-bounded), consumed by the Step 4 directory/filename builder. Derived at insert time from the search agent's `title`, then **re-derived from the ATS-canonical `title` when the full-JD fetch overwrites it** — so the packet naming carries the canonical title, consistent with the tracker row. |
| `jd_markdown` | Full job description, fetched from the ATS JSON detail endpoint at insert time and converted HTML → Markdown. Source text for the Step 4 `job_posting.md`; `NULL` if the fetch failed. |
| `location` | Structured location from the ATS record, kept as review/packet context. `NULL` if the fetch failed. |
| `search_agent` | Which agent surfaced the posting (`claude` / `perplexity`) — lets us compare source quality over time. |
| `first_seen_at` | Timestamp the posting entered the DB. |
| `decision` | Enum of user's application decision (`Apply` / `Skip`). Nullable until feedback is given. |
| `fit_feedback` | Optional free-text feedback from user. |
| `added_to_tracker` | Boolean. `1` (True) indicates that the row has been added to the application tracker in Google Sheets.

**Free-text feedback and ground truth.** The `fit_feedback` field is optional but central to the system's learning loop. While `decision` gives a filterable signal, the free-text captures *why* a posting does or doesn't fit — nuance the ordinal rating can't. This qualitative feedback is the primary raw material the daily prompt-updating cron job (the "ground truth" refinement of the Step 1 search prompt, fed by Step 3 feedback) draws on to refine future searches. Capturing it verbatim, even when sparse, materially improves search quality over time; it should never be required, but it should always be easy to add during the Step 3 review session.

### Job Fit Feedback

Each row in the `postings` table must be given a value for the `decision` field by the user. There are only two possible values: `Apply` or `Skip`. If the user decides to `Skip` a job posting, the value is recorded and the process ends at Step 3 - no resume revisions are proposed nor is the job posting written to the application tracker.

**Review is a deterministic CLI loop, not an agent conversation.** Rating a posting `Apply`/`Skip` and capturing optional free-text feedback is pure I/O — it requires no model reasoning — so routing it through the Claude Agent SDK would spend tokens per posting for no benefit. Step 3 is therefore a plain script (no LLM in the loop) that:

1. Queries Turso for rows with a `NULL decision`. There is no review-pending notification: the headless daily search on Fly keeps accumulating new postings regardless of review cadence, and the user reviews the outstanding backlog whenever they choose — the `NULL decision` query surfaces whatever has piled up since the last session.
2. For each, opens the posting's `url` in Google Chrome (`chrome`) so the user evaluates the real posting rather than a stored snapshot.
3. Prompts in the terminal for the decision and optional `fit_feedback`, and writes both straight back to Turso.

Because it is deterministic, it is invoked directly from within the interactive Claude Code session with the `!` prefix (e.g. `! python -m review`) or in a shell session. It is deliberately **not** a slash command, since a slash command would expand into a prompt the agent processes and reintroduce the per-posting token cost this design avoids.

### Resume Revisions

Step 4 produces a per-job resume tailored from the user's canonical `base_resume.docx`. It runs **asynchronously to Step 3**: deciding to `Apply` in the review loop does not trigger resume work inline — it only records the decision and lets the row accumulate in a queue. Revisions are drafted later, in a separate interactive Claude Code session, whenever the user chooses to work through the `Apply` backlog. This keeps the deterministic, no-LLM review loop cheap and decoupled from the token-spending generative step.

**Invocation: an agentic slash command.** Unlike Step 3, resume revision *is* model work (reading a JD, tailoring prose), so it is exposed as a slash command inside Claude Code rather than a `!`-prefixed script. The command:

1. **Identifies newly eligible rows.** Queries Turso for rows with an `Apply` decision but have not yet been added to the application tracker (`added_to_tracker` is not `1`). These are the jobs to which the user has decided to apply but not yet had a resume drafted.
2. **Fans out subagents in parallel.** The command **must spawn one subagent per eligible row** so multiple jobs are drafted concurrently rather than serially. Each subagent owns exactly one posting end-to-end. (The idempotency guard against an already-drafted row lives inside the subagent's first action — see below — not in a pre-dispatch scan.)

**What each subagent does.** For its assigned row, a subagent:

1. **Creates its directory first, as the idempotency guard.** As its very first action, runs an atomic, fail-if-exists `mkdir` (no `-p` clobber) at `~/Documents/Job Applications/{normalized_company} - {title_slug}` (from the row's already filesystem-safe `normalized_company` and `title_slug`). If the directory already exists the `mkdir` fails and the subagent stops immediately — before any resume work — so a row already drafted on a prior run (e.g. one where the tracker write succeeded but the `added_to_tracker` DB write did not) costs one bare `mkdir`, not a wasted tailoring pass. `added_to_tracker` is the primary completion signal; this failed-`mkdir` early-exit is the cheap fallback that keeps the command idempotent against a partial write.
2. Writes `job_posting.md` inside that directory, containing the row's `jd_markdown` (the full job description captured at insert time), so the tailored resume and the source JD live together.
3. Revises `base_resume.docx` for this specific posting according to the tailoring instructions, using the JD as context.
4. Emits the tailored resume in both formats into the same directory:
   - `NicholasBell_Resume_{title_slug}_{normalized_company}.docx`
   - `NicholasBell_Resume_{title_slug}_{normalized_company}.pdf`

The resulting directory is the self-contained application packet for one job: the source posting plus a `.docx`/`.pdf` pair ready to submit. Once the packet exists and the row is written to the application tracker (Step 5), `added_to_tracker` is set to `1`.

**Tailoring instructions (TK):**

```
TK
```

### Application Tracker (Google Sheets)

Step 5 appends one row to a Google Sheet for each job that reaches the application stage. The Sheet is the **write-only application tracker**: the agent only ever appends to it and **never reads back from it**, so application state (status, dates the user fills in) lives in the Sheet and is deliberately *not* mirrored into Turso (see Database). It is the user's manual workspace for tracking outcomes.

**When the write happens.** The tracker write is the tail of Step 4/5, not Step 3. A row is appended **when the resume packet is generated** — i.e. as the final action of a Step 4 subagent, after the `.docx`/`.pdf` pair exists. Deciding to `Apply` in Step 3 does *not* write to the tracker; only jobs the user has actually committed to (a resume was drafted) appear there. On a successful append, the subagent sets `added_to_tracker = 1` on the row. Because `added_to_tracker` is the primary completion signal and the packet-directory `mkdir` is the idempotency guard (see Resume Revisions), a re-run never double-appends a job whose packet already exists.

**Connection: the local `gws` CLI over local OAuth.** The write goes through the `gws` Google Workspace CLI, which holds a local OAuth token (with a refresh token) for the user's **personal** Google account, `nicky.bell@gmail.com`. This keeps the credential entirely local and off the Fly.io server — consistent with the cloud/local split (Steps 3–5 are local Claude Code sessions; Google Sheets OAuth stays local). Using the already-authenticated `gws` CLI avoids provisioning a second Google client library or auth flow.

**The tracker Sheet.**

- **Title:** `Job Search Application Tracker` (tab: `Applications`).
- **Spreadsheet ID (config):** `1DQNix3tZ9oFqfA9R2r0Npj1UWvAu2cEg_RJVf6SGki4` — the agent needs this ID to target the append; it is fixed configuration, not something the agent discovers at runtime.
- **Header row** is bolded and frozen (`frozenRowCount = 1`).

**Columns (in order).** The first four are written verbatim from the `postings` row; the last three are set/filled as noted:

| Column | Source |
| --- | --- |
| `Company` | `normalized_company` |
| `Title` | `title` |
| `URL` | `url` |
| `Date Posted` | `date_posted` |
| `Date Added` | Set by the agent at append time (the tracker-write date). |
| `Date Applied` | **Left blank; filled by the user** when they submit the application. |
| `Status` | **Left blank; set by the user** from a dropdown. |

**`Status` column.** A strict data-validation dropdown with seven enum values, each backed by a conditional-formatting color rule so the sheet is scannable at a glance: `No response`, `Rejected`, `Interview(s)`, `Final Round`, `Offer`, `Decided to pass`, `Accepted`. A blank Status (freshly appended row) matches no rule and stays uncolored until the user picks a value — intended.

**Append semantics.** The write is a single `sheets.spreadsheets.values.append` call against range `Applications!A:G` with `valueInputOption = USER_ENTERED` and `insertDataOption = INSERT_ROWS`, so a new row is added at the bottom without any row-index bookkeeping or read-modify-write. Concretely:

```
gws sheets spreadsheets values append \
  --params '{"spreadsheetId":"1DQNix3tZ9oFqfA9R2r0Npj1UWvAu2cEg_RJVf6SGki4","range":"Applications!A:G","valueInputOption":"USER_ENTERED","insertDataOption":"INSERT_ROWS"}' \
  --json   '{"values":[["<normalized_company>","<title>","<url>","<date_posted>","<date_added>","",""]]}'
```

### Search Prompt Updates from "Ground Truth"

<!--
**Data Sources:**

You have access to two data sources that can assist you in making your determinations about job postings to which Nicky should consider applying.

1. `base_resume.docx`: This is the generalized version of Nicky's resume that lays out his background and skills. A post worth applying to will fit Nicky's career narrative and/or skill set.
2. JD database: as part of this work, you will create a SQLite database to hold job descriptions that you identify or that Nicky provides directly. You will regularly get feedback from Nicky on the fit of the job descriptions, which will allow you to fine-tune your search over time as the "ground truth" becomes more refined.
-->

## Architecture

The system is split between a headless cloud runtime and interactive local sessions, coordinated through a single hosted database.

**Language/runtime: Python (decided 2026-07-21).** One language across cloud and local: the cloud cron uses the Claude Agent SDK for Python plus the Perplexity and Turso (libSQL) clients and the HTML→Markdown step; the local Steps 3–5 tooling (review CLI, `.docx`/`.pdf` generation) is also Python — the `.docx`/`.pdf` ecosystem is what tipped the choice.

**Hosting: Fly.io (cron) + Turso (database).**

- **Fly.io** runs the automated Steps 1–2 (daily search, idempotent insert, full-JD fetch) and the prompt-refinement cron. The agent runs as a container (Claude Agent SDK plus the Perplexity client, the ATS fetch + HTML→Markdown step, and `.docx` handling), so a Fly Machine wakes on schedule for each run and stops after, keeping cost to pennies per month. Scheduling is via Fly Machines' native scheduled start. The A/B-day alternation is derived from the run date (day-of-year parity) inside the container so a single cron entry self-selects Claude vs. Perplexity. API keys and the Turso auth token are injected via `fly secrets set`.

- **Turso** (hosted libSQL) holds the JD database. libSQL is SQLite-compatible, so the storage design above is unchanged, but both the cloud cron and the local interactive sessions connect to a single hosted copy — eliminating the divergence that a cron-writes-here / user-reviews-there split would otherwise create. This is what lets the daily search run even when the laptop is off while keeping Steps 3–5 as local Claude Code sessions.

**Division of responsibilities.**

- *Cloud (Fly.io):* Steps 1–2 and the prompt-refinement cron; writes to Turso.
- *Local (Claude Code over the same Turso DB):* Step 3 fit review, Step 4 (resume revisions, including `.docx`/`.pdf` generation), and Step 5 (the Google Sheet write). Google Sheets OAuth credentials live locally, keeping that credential off the server.

