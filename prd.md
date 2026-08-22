# Job Search Agent for Customer Enablement and Education Roles

This document outlines the goals and features of the Job Search Agent using the Claude Agent SDK.

## Purpose

The purpose of this agent is to:

1. Conduct a recurring search for job postings that fit the desired criteria (a daily-waking cron that self-gates to a Mon/Wed/Fri cadence — see Daily Search),
2. Store those job postings (including the full job description, fetched from the posting's own ATS) in a database exactly once — the insert is idempotent, so re-surfaced postings no-op,
3. Obtain feedback on job fit from the user,
4. Generate resume revisions (from a curated library of base templates, one per role family, from which each job's resume branches) for job postings to which the user wishes to apply, and
5. Write the job posting to an application tracker as a Google Sheet (for user's tracking - the Sheet is a human-readable projection of the database plus the user's own workspace columns; see Application Tracker for the relationship).

The agent will also update the prompt in Step 1 as a weekly cron job that reflects the developing "ground truth" from Step 3 — its output is a pull request the user reviews and merges, never a direct commit (see Search Prompt Updates from "Ground Truth").

## Requirements

### Automation

Steps 1 and 2 (the recurring search and writing to database) are automated cron jobs. However, they should be runnable from the command line with a parameterized time window for the search and the search agent (Claude or Perplexity). Step 3 is "human-in-the-loop" with user input on each job via a CLI. Step 4 (resume revisions) is a local CLI command (`jsa generate`) that tailors the resume in one pass and writes a changelog; Step 5 (the tracker write) is a deterministic CLI command (`jsa track`) that `jsa generate` calls as its final action — see Application Tracker.

### Daily Search

The deployed cron runs a weekly cadence **anchored on Perplexity**, which carries the recurring discovery load on a **Monday / Wednesday / Friday** schedule, with per-day look-back windows sized to tile the week without gaps: **Monday looks back 72h** (covering the weekend), **Wednesday and Friday 48h** each. **Friday additionally runs a weekly Claude Deep Research sweep over a 168h window, and on Friday Claude runs first, before that day's Perplexity search.** The windows deliberately overlap, so a missed run is recovered by the next one (and re-inserts no-op on `canonical_url`). The cadence lives in `pipeline.CRON_SCHEDULE` (ET weekday → the ordered `(agent, window_hours)` searches for that day); the cron self-gates against it, and each search is also runnable from the command line with a parameterized time window and agent (`jsa search --agent … --window-hours …`).

**Trial outcome (2026-08-17).** Perplexity surfaced materially more qualifying opportunities than Claude — enough that its higher unique yield outweighs its lower Apply precision — while Claude still surfaced some roles Perplexity missed. So steady state weights Perplexity as the recurring Mon/Wed/Fri search and keeps Claude as a complementary weekly 168h sweep on Fridays, rather than the earlier day-of-year alternation.

**Perplexity Agent API deep research (the recurring Mon/Wed/Fri search).** This requires an SDK-to-SDK connection with the API key supplied as a Fly-managed secret (`fly secrets set PERPLEXITY_API_KEY=…`), injected into the Machine as an environment variable at runtime.

- **API and preset:** the **Agent API** (`POST /v1/agent`) at the **`xhigh`** preset — Perplexity's highest research tier, genuine multi-step *agentic* research. The request is `preset` + `input` + an explicit `web_search` tool, streamed (`stream: true`) with the `postings` contract enforced via `response_format` json_schema. Note `xhigh`'s event vocabulary differs from the reasoning tiers — it loads a skill and searches inside sandboxed code (`response.sandbox.results`), not `response.reasoning.*` events — which the runner's `_StreamState` accounts for.

**Claude Deep Research (the weekly Friday sweep).**

- **Model and effort:** `claude-opus-4-8` with `output_config: {effort: "xhigh"}` and adaptive thinking (`thinking: {type: "adaptive"}`), generous `max_tokens` headroom (≥ 64K). `xhigh` is the documented sweet spot for agentic search (lower effort consolidates/skips tool calls — wrong for a task whose value is exhaustive source-checking). Fable 5 was considered and rejected: 2× Opus pricing, refusal-classifier and data-retention overhead a headless cron doesn't want, and no capability need above Opus here. Since Steps 1–2 run on the Claude Agent SDK (the Claude Code harness), `xhigh` is already its default effort on Opus — implementation mostly amounts to pinning the model string.

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

**Supported-ATS membership is an inclusion criterion, checked via JSON list endpoints — not a per-platform verification effort.** Smoke testing showed the naive index check fails both ways: an unlisted ATS detail page (e.g. a Greenhouse "private"/pulled post) keeps rendering a fully open-looking page with an application form, while the company's public index is a client-side JS shell a plain fetch can't read. The prompt therefore restricts eligible postings to the four ATS platforms with public, unauthenticated JSON list endpoints — Greenhouse (`boards-api.greenhouse.io`), Lever (`api.lever.co`), Ashby (`api.ashbyhq.com/posting-api`), Rippling (`ats.rippling.com/api/v2`) — and verifies liveness by GET-fetching that endpoint (board slug derived from the posting's own URL), with presence-in-list as the only accepted liveness signal and absence (for any reason) meaning exclude. Postings on any other platform — Workday (whose index is a POST behind bot management), custom careers sites, unrecognized ATS — are **out of scope by construction, not investigated case-by-case**: spending research turns probing exotic hosts for verification paths trades away discovery effort, and a posting that can't be resolved to a supported ATS detail record also can't have its liveness established at all. (Such a posting *could* still have its description captured from schema.org JSON-LD — see Direct Job Add — but that is content capture, not liveness, so it does not readmit the posting to the search's scope.) The escape hatch is additive — other ATS with public JSON list endpoints (SmartRecruiters, Workable, Recruitee) can be added to the table if review shows meaningful roles being missed.

**Full-JD capture at insert.** For each posting that survives the idempotent insert (i.e. each genuinely new row), Step 2 GET-fetches the posting's own public ATS JSON *detail* record — the same endpoint family the liveness gate already restricts every surviving posting to — and stores the full job description as `jd_markdown`, converting the description HTML to Markdown (Greenhouse `content`, which arrives HTML-entity-escaped and must be unescaped first; Lever `description`; Ashby `descriptionHtml`; Rippling's v2 job record). The fetch also captures the ATS record's structured `location` and canonical `title`; on success the ATS `title` replaces the search agent's transcription, since the two search agents transcribe titles from different boards inconsistently and the ATS-canonical title is what the tracker row and the application-packet naming should carry. Capture happens at insert time, while the posting is alive, because Step 4 may run days later — after a posting has been pulled. Two deliberate boundaries: (1) this is **content capture, not a liveness gate** — a failed fetch never excludes a row; the row inserts with the search agent's values and a `NULL` `jd_markdown`/`location`. (2) The full posting is deliberately **not** requested from the search agents' structured output: an LLM asked to reproduce a JD "verbatim" paraphrases and truncates, and multi-KB JSON string fields inflate output cost and truncation risk.

### Insert Idempotency

**Approach: URL canonicalization into a `UNIQUE` constraint**

1. Before insert, reduce each posting's `url` to a canonical form: lowercase the host, strip tracking/session parameters (denylist including `utm_*`, `gh_src`, `currentJobId`, and other `ref`/session params), drop URL fragments, and normalize trailing slashes.
2. Store it as `canonical_url` with a `UNIQUE` constraint and write via `INSERT … ON CONFLICT DO NOTHING`, so re-encountering a known posting is a no-op.

This single mechanism is what makes the pipeline safe to re-run — overlapping daily 48-hour search windows, a retried cron, and CLI invocations with wide time windows all re-surface postings already in the DB, and every one no-ops on the constraint — and it carries the cross-source dedup load too, since both search agents converge on the same index-linked ATS URL.

**Storage / schema notes:**

- Derive `normalized_company` and `title_slug` into filesystem-safe form at write time (Step 2), so Step 4 (`jsa generate`, and the `jsa packet` head it builds on) composes its `~/Documents/Job Applications/…` paths directly from the row without re-sanitizing. It also means any `mkdir` failure is a genuine "already exists" rather than a path-hostile character tripping it.

### Direct Job Add

Nicky can hand the agent a posting directly rather than waiting for the daily search to surface it. This is a CLI subcommand, `jsa add <URL>`, and it deliberately **reuses Step 2 wholesale** — canonicalize → idempotent insert → full-JD fetch — rather than opening a second way into the table. A hand-added row is therefore indistinguishable downstream from a searched one: it lands in the same Step 3 review backlog and flows on to Steps 4–5 identically. The only difference is provenance, recorded as `search_agent = 'manual'`.

Because it shares the canonical-URL idempotency key, adding a URL the daily search already found does not duplicate the req — including when the two differ only by tracking parameters.

**A hand-added posting is decided `Apply` on arrival.** Supplying the URL *is* the decision: Nicky found the posting, read it, and chose to add it, so routing it through the Step 3 backlog would ask a question he has just answered. The row is therefore written with `decision = 'Apply'` in the insert itself (never briefly visible as undecided), skips review entirely, and lands directly in the Step 5 tracker queue — reaching the Google Sheet by the same path as a searched posting marked `Apply` in review. `fit_feedback` stays `NULL`; the free-text loop is fed by review, and nothing here forces a note.

Adding a URL that is **already in the table** carries the same intent, so that row is promoted to `Apply` as well — including one previously marked `Skip`, since re-adding it by hand is an explicit reversal. Only the decision moves: any `fit_feedback` written during review is kept (it is still ground truth for the search-prompt loop even though the verdict changed), and `search_agent` is *not* rewritten, since the req really was found by that agent. A row already `Apply` is left untouched, so re-adding an already-tracked posting cannot requeue it for a second Sheet append.

Two deliberate departures from the automated pipeline:

- **No `search_findings` row.** That table is *search* telemetry. A posting Nicky supplied is not a search result, and crediting it to an agent would inflate that agent's coverage and distort Apply precision.
- **Supported-ATS membership gates neither the insert nor the capture.** The four-ATS rule is an inclusion criterion for what the *search* may return — there it doubles as the liveness proxy for postings an agent found on its own and cannot otherwise vouch for. Here Nicky has already vouched, so liveness is settled and the only remaining question is whether the description can be retrieved. It usually can: Google requires schema.org `JobPosting` JSON-LD for a posting to surface in Google Jobs, so it is widely embedded server-side even on platforms whose human-facing pages are JavaScript shells (verified 2026-08-11 — a Workday detail page and an Ashby page both served a full `JobPosting` block to a plain GET with no JS). Capture therefore runs **supported ATS fetcher → JSON-LD → `NULL`**, and an unsupported URL only lands a `NULL jd_markdown` when both fail.

  **A JSON-LD capture is evidence of content, never of liveness.** A pulled or unlisted req can still render perfectly good JSON-LD, so nothing may read a successful extraction as proof a posting is open — which is precisely the failure mode the Daily Search section's smoke testing found. This is why the *search* path deliberately does **not** use the fallback: a searched URL outside the four platforms is a prompt violation whose liveness is unverified, and enriching it with a description would disguise that. The fallback exists only where a human has already vouched for the URL.

`company` and `title` are `NOT NULL`, so they are resolved in precedence order: an explicit `--company` / `--title` flag, then the ATS detail record's canonical title and the board slug (the board slug *is* the company, just lowercased and hyphenated), then — in an interactive session — whatever Nicky edits into a pre-filled prompt. An explicit flag is never overwritten by the ATS transcription, which is the one place this path departs from Step 2's "the ATS title wins" rule: a deliberate override outranks a transcription. A non-interactive add (`--no-input`) that can derive neither field fails loudly rather than inventing a placeholder.

### Posting Drift and Re-fetch

Step 2 captures the job description once, at insert time, and never looks again. That is deliberate and still correct — Step 4 may run days later, after a posting has been pulled, so the capture must happen while the posting is alive. But it assumes a posting is immutable once captured, and it is not: **employers edit reqs in place.** Capture-once plus edit-in-place means a row can silently drift from its source with no path back — and the drift reaches the tracker row and the Step 4 packet naming, since both are built from `title` and `title_slug`.

`jsa refetch` is that path back. It re-resolves the ATS record for stored postings and applies the same rule the insert does: the ATS-canonical `title` wins, `title_slug` is re-derived from it, and `jd_markdown`/`location` are refreshed alongside. Two constraints shape it:

- **A failed fetch leaves the row completely untouched.** This is the same degrade-don't-destroy stance as the pipeline, applied in the direction that matters here: the pipeline degrades to a `NULL` description rather than dropping a row, and re-fetch must never trade a good stored capture for a network blip or a pulled posting. A posting that has disappeared from its board is *reported*, not deleted — the stored JD is then the only surviving record of it.
- **The scope is the postings where a change could still change Nicky's actions.** Eligible rows are `decision = 'Apply'` **and** (absent from the tracker Sheet **or** present with a blank `Date Applied`) — the second condition is explicitly an OR, so being in the tracker does not exempt a job that has not actually been applied to. Drift on an undecided or Skipped row changes nothing, and once the application is out the door a correction is moot: an applied row is skipped entirely and its Sheet copy stays frozen as the record of what was submitted. "Already applied" exists only in the Sheet's `Date Applied` column, so the scope check reads the tracker index (`ID` → row, `Date Applied`); if that read fails, the command fails loudly rather than guessing at scope. `--all` widens to every stored row and `--id` targets one — both select unconditionally, so for them the index read is best-effort (it only enables the Title refresh below).
- **A corrected title is propagated to the tracker row.** Under the projection model (see Application Tracker), a tracked-but-unapplied row's `Title` cell is a view of the database row just corrected, so refetch refreshes it — one `values.update` on the single Title cell, matched by the `ID` column. Ordering is DB first, Sheet second, and a failed Sheet write degrades to a flagged hand-fix rather than blocking or reversing the reconciliation. A location-only change is not flagged: the Sheet carries no location column, so nothing there is stale.
- **A stale packet directory is regenerated, not just invalidated.** The application packet is a derived artifact: `job_posting.md` projects the database row, and the Step 4 resume is a function of the chosen base template plus the JD — regenerable by construction. So when a refetched change touches what the packet reflects — the title (which names the directory) or the description — refetch does not merely blank the packet; it **rebuilds it and regenerates the resume** to match the corrected row, in two parts with distinct owners:

  1. **refetch owns the delete.** Once the DB title is updated, only refetch still knows the packet's *old* directory name — anything reading the updated row (`jsa generate` included) composes only the *new* name — so the removal of the stale directory has to live here. It targets the exact expected old path, never clobbers a distinct directory already sitting at the new name (flagged for a hand look instead), and a location-only change touches nothing.
  2. **`jsa generate` owns the rebuild.** refetch invokes `jsa generate --id` for that row, which (re)creates the directory at the possibly-renamed new path with a fresh `job_posting.md`, tailors a new resume from the corrected title/JD, writes the changelog, and calls `jsa track`. Invoking `jsa generate --id` directly bypasses the `added_to_tracker = 0` eligibility queue, so a *tracked* but unapplied row — which the queue would skip — is still regenerated; `--id` waives generate's tracker-eligibility (mirroring `jsa packet --id`), and the closing `jsa track` no-ops on the already-tracked row, so no second Sheet append occurs (refetch's Title propagation, above, already kept the Sheet current).
  
  Regeneration is the **default**: a tracked-but-unapplied row drifting is the *modal* refetch condition — a job spends most of its refetch-eligible life packeted and tracked but not yet submitted, the same window in which employers edit the req — so the modal outcome should be a fresh resume matching the corrected posting, not a bare directory awaiting a manual step. (This discards any hand-refinements made to the prior resume during interactive review — consistent with the packet being a derived artifact: a resume tailored to the old JD is stale, and re-tailoring from the corrected JD is the point.) It fires **only when a packet already exists** for the drifted row, so the blast radius is exactly the jobs already invested in. The only other mode is `--dry-run`, which reports drift and writes nothing; there is deliberately no "reconcile now, regenerate later" middle mode, since on a title change that would orphan the old-named directory (a deferred rebuild, reading the corrected row, knows only the new name). Refetch never *creates* a packet where none existed; it only regenerates one already there.

  **Ordering and failure.** refetch builds the new packet before removing the old one, so a failed regeneration on a rename leaves the previous packet intact rather than deleting it with nothing to replace it. Either way a failure — a `jsa generate` error or a filesystem error — is **flagged, never a rollback**: the DB stays corrected and refetch reports the row as needing a manual `jsa generate --id`, the same degrade-don't-destroy stance as a failed Sheet write (nothing irreplaceable is lost — `job_posting.md` projects the DB and the resume is regenerable).

### Database

The Turso (libSQL) database holds the structured search output, the insert-idempotency signal (`canonical_url`), and the user's fit feedback — everything the "ground truth" learning loop needs. It deliberately does *not* store application state: that lives in the Google Sheet tracker's user columns (Step 5), and mirroring status or resume-branch data into Turso would duplicate the user's workspace into a copy nothing needs — the application consults the Sheet's `Date Applied` only transiently, to scope `jsa refetch`, never to store. A single core `postings` table covers everything the DB is responsible for.

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
| `search_agent` | Which agent *first* inserted the posting (`claude` / `perplexity`), or `manual` for a posting Nicky added directly (see Direct Job Add). Because both agents converge on the same `canonical_url`, this column alone can't show overlap — full per-agent attribution lives in `search_findings` (below). |
| `first_seen_at` | Timestamp the posting entered the DB. |
| `decision` | Enum of user's application decision (`Apply` / `Skip`). Nullable until feedback is given. |
| `fit_feedback` | Optional free-text feedback from user. |
| `decided_at` | Timestamp the current `decision` was written (added 2026-08-22) — set on every decision write: the review loop, an in-session amendment, a manual add's INSERT, and a re-add promotion. This is what lets the prompt-refinement loop scope each run to ground truth that is *new since the last run* instead of re-reviewing the whole corpus. Nullable; rows decided before the column existed keep `NULL`, meaning "already incorporated by the manual refinement rounds" — re-deciding a row refreshes the timestamp and returns it to scope. |
| `added_to_tracker` | Boolean. `1` (True) indicates that the row has been added to the application tracker in Google Sheets.

**`search_findings` (search telemetry).** A second, append-only table records one row per `(run_date, agent, canonical_url)` for *every* posting an agent returns — including postings whose `postings` insert no-ops — because `search_agent` on the posting row records only the *first* inserter and therefore cannot show overlap. This is what `jsa ab-report` joins back to `postings` for per-agent coverage, overlap, and Apply precision; it keeps accruing under the weekly cadence, since Friday still runs both agents. It is search telemetry, not application state — which is why the direct job-add path deliberately writes no row here (see Direct Job Add).

**`prompt_refinement_runs` (the refinement loop's cursor).** One row per prompt-refinement run that considered ground truth (added 2026-08-22): `run_at`, plus counts of what was considered/proposed. The next run's scope is rows with `decided_at > MAX(run_at)`. A run is recorded whether or not its PR is merged — a rejected translation is still *considered* ground truth, and the way to resurface a posting is to re-decide it. This table, not the prompt, is where incrementality lives: the search prompt itself stays standalone (see Search Prompt Updates from "Ground Truth").

**Free-text feedback and ground truth.** The `fit_feedback` field is optional but central to the system's learning loop. While `decision` gives a filterable signal, the free-text captures *why* a posting does or doesn't fit — nuance the ordinal rating can't. This qualitative feedback is the primary raw material the weekly prompt-updating cron job (the "ground truth" refinement of the Step 1 search prompt, fed by Step 3 feedback) draws on to refine future searches. Capturing it verbatim, even when sparse, materially improves search quality over time; it should never be required, but it should always be easy to add during the Step 3 review session.

### Job Fit Feedback

Each row in the `postings` table must be given a value for the `decision` field by the user. There are only two possible values: `Apply` or `Skip`. If the user decides to `Skip` a job posting, the value is recorded and the process ends at Step 3 - no resume revisions are proposed nor is the job posting written to the application tracker.

**Review is a deterministic CLI loop, not an agent conversation.** Rating a posting `Apply`/`Skip` and capturing optional free-text feedback is pure I/O — it requires no model reasoning — so routing it through the Claude Agent SDK would spend tokens per posting for no benefit. Step 3 is therefore a plain script (no LLM in the loop) that:

1. Queries Turso for rows with a `NULL decision`. There is no review-pending notification: the headless daily search on Fly keeps accumulating new postings regardless of review cadence, and the user reviews the outstanding backlog whenever they choose — the `NULL decision` query surfaces whatever has piled up since the last session.
2. For each, opens the posting's `url` in Google Chrome (`chrome`) so the user evaluates the real posting rather than a stored snapshot.
3. Prompts in the terminal for the decision and optional `fit_feedback`, and writes both straight back to Turso.

Because it is deterministic, it is invoked directly from within the interactive Claude Code session with the `!` prefix (e.g. `! python -m review`) or in a shell session. It is deliberately **not** a slash command, since a slash command would expand into a prompt the agent processes and reintroduce the per-posting token cost this design avoids.

**Decisions are revisable within a session.** A judgment frequently changes *while the comment is being written* — articulating why a role is a Skip is often what reveals it is an Apply. A loop that commits the decision before the comment and then moves on makes that correction impossible, so there are two routes back:

1. **`:a` / `:s` at the feedback prompt** flip the decision for the posting in hand. Anything typed after the command is kept as the comment, so changing one's mind mid-sentence does not cost the sentence. `:b` discards and returns to the decision prompt for the same posting.
2. **`b` at the decision prompt** steps back to the *previous* posting and reopens it, showing its recorded decision (a bare Enter keeps it) with its comment pre-filled for editing. Pre-filling is applied to the comment but deliberately *not* to the decision prompt: on a single-letter choice, a pre-filled `a` turns the natural keystroke into `aa` — pre-filling helps only where the text is meant to be edited. The backlog list is captured once at session start, so a row that was just decided stays reachable even though it no longer matches the `NULL decision` query. At the end of the backlog the same offer is made once more, since the final posting is otherwise the one decision a session could never take back.

Every decision is still written the moment it is made — revision is an `UPDATE` of the same row, not a deferred commit — so quitting (or Ctrl-C) at any point still loses nothing.

**The comment field must be genuinely editable.** `fit_feedback` is the primary raw material of the ground-truth learning loop, so anything that discourages writing it is a design defect. Python's built-in `input()` reads a raw line: arrow keys, ⌥+delete and ^W arrive as escape bytes and land *in the comment as garbage*, which in practice means a typo can only be fixed by deleting back to it. The review prompts therefore go through a line-editing layer (`prompt_toolkit`) that restores the normal editing keys, supports the pre-filled editable buffer the amend flow above depends on, and offers Ctrl-X Ctrl-E to open `$EDITOR` for a long comment. This is local-terminal-only concern; it has no bearing on the headless cloud runtime, which never prompts.

### Resume Revisions

Step 4 produces a per-job resume tailored from a **curated template library** (decided 2026-08-22, superseding the earlier single canonical `base_resume.docx`), alongside a changelog recording what the tailoring changed and why.

**The template library.** `resume_templates/` (gitignored — personal data; `JSA_RESUME_TEMPLATES_DIR` to override) holds one maintained `.docx` per role family — e.g. `customer-education.docx`, `ai-enablement.docx` — with the filename slug as the template's id. The design formalizes Nicky's actual practice, which was nearest-fit selection from prior tailored resumes: the right starting point is the version already aimed at the job's *role family*, so per-job deltas stay small and the replace-only patch never has to restructure across families. Curating a small library (rather than patching from whichever prior leaf is nearest) is what avoids that practice's failure modes: copies-of-copies drift, per-company phrasing leaking between applications, facts needing fixes in N leaves, and refetch's regenerate-by-construction guarantee breaking because the starting point was whatever the corpus held that day. Two maintenance rules:

- **The library expands outward; a template is never force-fit.** When a JD belongs to a role family no template covers, the model says so, starts from the *nearest* existing template, and the resulting tailored resume is saved back into `resume_templates/` as the new family's template. The changelog flags every such creation for a curation pass — the new template began life tailored to one company, so it should be scrubbed of company-specific phrasing before its next use.
- **Good hand-refinements are folded back into the template by the user** — an explicit curation moment, not implicit copy-lineage — so improvement compounds in ~a handful of maintained files instead of a growing tree of leaves.

The library is seeded by a one-time consolidation of the best of the existing tailored resumes in `~/Documents/Job Applications/` (see `TODO.md`). (Open Career Format — a candidate-owned master-career-record schema — was evaluated 2026-08-22 and rejected: its useful idea is separating career facts from rendered documents, but adopting it means owning a JSON→recruiter-grade-docx rendering pipeline, exactly the layer the structured patch exists to avoid, for a v0.3 spec with no renderer ecosystem. An accomplishments-bank file was also considered and declined; the tailoring context is the chosen template plus the JD, nothing more.) It runs **asynchronously to Step 3**: deciding to `Apply` in the review loop does not trigger resume work inline — it only records the decision and lets the row accumulate in a queue that Step 4 drains on demand.

**Invocation: A local CLI command.** A plain CLI command, `jsa generate`, on the same footing as the Steps 1–2 search runners — it drives the Claude Agent SDK **headlessly, with a pinned model and effort and no inherited session state**, emits a progress trace to the log rather than to a watched terminal, and can be run detached. Like the other local commands it takes `--id` to target a single row and `--dry-run` to preview the queue; `--id` waives the `added_to_tracker = 0` eligibility condition below (mirroring `jsa packet --id`) but never the `Apply` one — which is what lets `jsa refetch` hand a tracked-but-unapplied row back to generate (see Posting Drift and Re-fetch). It stays **local**: its inputs (`base_resume.docx`) and outputs (the packet, the submittable `.docx`/`.pdf`) live on the disk, and its final action needs the local `gws` grant.

For each eligible row (`decision = 'Apply' AND added_to_tracker = 0`), `jsa generate`:

1. **Ensures the packet directory and `job_posting.md`.** This reuses the *pure helpers* `jsa packet` is built from — the path builder (`~/Documents/Job Applications/{normalized_company} - {title_slug}`) and the `job_posting.md` writer — but **not** `jsa packet`'s standalone fail-if-exists behavior. Run alone, `jsa packet` does a fail-if-exists `mkdir` and, on an existing directory, skips without touching it — it checks only whether the `mkdir` succeeded, never whether the packet is *complete* (it won't even rewrite `job_posting.md`). `jsa generate` instead **ensures**: it creates the directory if absent, accepts it if present, and (re)writes `job_posting.md` so the JD on disk matches the row (from a non-`NULL` `jd_markdown` only — a `NULL` one leaves any existing file untouched, see the no-JD rule below) — then leaves the completion judgment to the resume-and-tracker check below, not to the directory's existence. So a bare or half-built packet (an interrupted run, or a `jsa refetch` rebuild) is re-entered and completed rather than skipped.
2. **Selects the base template and revises it for this posting in one pass**, using the JD as context and the tailoring instructions (below), and emits the tailored resume in both formats into the directory. The mechanism (decided 2026-08-21) is a **structured patch, applied deterministically**: the model call — the Claude Agent SDK, headless, pinned to **`claude-opus-4-8`** (the same tier and reasoning as the Friday search sweep: quality-critical output, low volume, no capability need above Opus) — never touches the `.docx` itself. The single call receives *every* template's numbered paragraph text plus the JD, and its JSON output names the chosen base (`base`: a template slug — decided 2026-08-22: **the model picks the template**, and the pick plus its rationale land in the changelog where a wrong pick is visible and correctable on a re-run), optionally declares a new role family (`new_family`: triggers the library-expansion rule above), and carries the patch (paragraph id → replacement text + rationale, ids referencing the chosen template's numbering) that `python-docx` applies to a copy of that template. This buys reproducible reruns, formatting that cannot break (only text inside existing paragraphs changes), and a changelog that falls out of the patch for free. The `.pdf` is then rendered from the tailored `.docx` via LibreOffice headless (`soffice --headless --convert-to pdf`) — fully scriptable and detachable, no UI automation, fonts resolved from the local machine (Word/`docx2pdf` was considered and rejected: higher fidelity in principle, but it drives Word over AppleScript, which is flaky headless). Output files:
   - `NicholasBell_Resume_{title_slug **(without spaces)**}_{normalized_company **(without spaces)**}.docx`
   - `NicholasBell_Resume_{title_slug **(without spaces)**}_{normalized_company **(without spaces)**}.pdf`
   - **Space characters are removed from the resume `.docx`/`.pdf` file names, but not from the containing directory name.**
3. **Writes `resume_changelog.md`** into the directory — what the tailoring changed relative to the base and why. This is the artifact that makes the optional interactive-review phase tractable: the user reviews and refines *particular* changes rather than re-deriving the whole diff by hand. Because the tailoring is a structured patch, the changelog is rendered *from the applied patch* — one addressable entry per change, each carrying its rationale — rather than being a second model-written artifact that could drift from what actually changed. It also records **which template was chosen and why**, and flags a new-template creation for the curation pass described above.
4. **Appends the row to the tracker** by calling `jsa track --id <row>` as its final action, which sets `added_to_tracker = 1` on a confirmed append (Step 5). That call is the seam between Steps 4 and 5.

**A row with no captured JD is never tailored blind.** When `jd_markdown` is `NULL` (the capture failed — e.g. a hand-added posting on an unsupported ATS where JSON-LD also failed), generate still ensures the directory but skips the tailoring **and** the closing tracker call — the tracker write happens only for a job whose resume was actually drafted — and flags the row (`jsa refetch --id N` is the usual fix); the row stays in the queue for the next run. One escape hatch: if the packet directory already contains a `job_posting.md`, generate tailors **from that file** instead of skipping. That is the path for a posting `refetch` can never reach (no supported ATS record to re-read): paste the JD into `job_posting.md` by hand and re-run. This is why step 1 above never (re)writes `job_posting.md` from a `NULL` `jd_markdown` — a hand-filled file is input, and generate must not blank it.

The resulting directory is the self-contained application packet for one job: the source posting, the tailored `.docx`/`.pdf` pair, and the changelog. Each eligible row is a fully independent unit of work — its own directory, its own SDK call, no shared state — so `jsa generate` **processes the queue in parallel** (a bounded worker pool, not one row at a time). The pool is capped (default 3 workers, `JSA_GENERATE_WORKERS` to override) so a large backlog cannot fan out unbounded model calls; `--id` collapses to a single row. The closing tracker appends are serialized across workers, preserving Step 5's one-row-at-a-time append semantics.

A `jsa refetch` rebuild also produces a resume-less directory, but it does **not** rely on this queue to fill it: refetch invokes `jsa generate` for the drifted row directly (see Posting Drift and Re-fetch), so a *tracked* but unapplied row — which the `added_to_tracker = 0` queue would skip — is still regenerated. The queue's re-entry of a bare directory therefore covers only the interrupted-run case.

**Tailoring instructions (TK).** These live in a versioned template file paralleling `deep_research_prompt.md` (the search prompt) — `tailoring_prompt.md` — so they are diffable, reviewable, and eventually reachable by the ground-truth loop. The file is a template, interpolated per job with the posting's title, company, and JD, and it carries the structured-patch output contract inline, the same pattern as the search prompt carrying its own output contract. The instructions themselves are still to be written — the committed file is a placeholder (TK).

### Application Tracker (Google Sheets)

Step 5 appends one row to a Google Sheet for each job that reaches the application stage. It is the user's manual workspace for tracking outcomes. **The database is the source of truth for posting data, and the Sheet is its human-readable projection plus the user's workspace** — and the operating rules follow from it:

- **Agent-written columns** (`ID`, `Company`, `Title`, `URL`, `Date Posted`, `Date Added`) are a view of the `postings` row. The agent appends them at Step 5 and may *refresh* them when the database changes — today that is `jsa refetch` updating the `Title` cell of a not-yet-applied row (see Posting Drift and Re-fetch). Authority never flows the other way: nothing read from the Sheet ever corrects the database.
- **User columns** (`Date Applied`, `Status`) are written only by the user. They are the one application state the system keeps, deliberately *not* mirrored into Turso (see Database). The agent reads them only where they inform its own scope — refetch's `Date Applied` lookup — and never writes them.
- **Step 5's idempotency stays entirely DB-side** (`added_to_tracker`): the Sheet is never consulted to decide whether to append.

**When the write happens.** The tracker write is the tail of Step 4/5, not Step 3. A row is appended **when the resume packet is generated** — i.e. as the final action of `jsa generate`, after the `.docx`/`.pdf` pair and the changelog exist. Deciding to `Apply` in Step 3 does *not* write to the tracker; only jobs the user has actually committed to (a resume was drafted) appear there. On a successful append, `jsa generate` sets `added_to_tracker = 1` on the row. Because `added_to_tracker` is the completion signal and is set only after the append is confirmed (see Resume Revisions), a re-run never double-appends a job already tracked.

**The write itself is a deterministic command, not model work.** Rendering the row's eight columns and shelling out to `gws` requires no reasoning, so Step 5 is `jsa track` — a plain CLI command, on the same reasoning as the Step 3 review loop. `jsa generate` calls `jsa track --id <row>` as its final action; that is the seam between the two steps.

**Idempotency is `added_to_tracker`, enforced at the query.** The backlog query *is* the guard: an already-appended row drops out of it and cannot be appended twice. Rows are appended one at a time rather than batched into a single call, so the flag is written per row and one rejected posting cannot strand the rest of the backlog in an ambiguous state. The flag is set **only after the Sheets API itself confirms an updated row** — any ambiguity (non-zero `gws` exit, unparseable output, a response reporting no appended row) is treated as failure and leaves the posting in the backlog for the next run. The alternative, optimistically flagging on a zero exit code, would silently drop a job out of the tracker forever, which is the one failure this step must not have.

**Credential failure mode.** `gws` exits 2 when its stored OAuth grant is missing or invalid. The common cause is Google expiring the refresh token, which it does after 7 days for an OAuth client left in "Testing" publishing status — so this recurs weekly until the consent screen is published. `jsa track` reports that case as an instruction to re-run `gws auth login` rather than surfacing a raw `invalid_grant`.

**The tracker Sheet.**

- **Title:** `Job Search Application Tracker` (tab: `Applications`).
- **Spreadsheet ID (config):** `1DQNix3tZ9oFqfA9R2r0Npj1UWvAu2cEg_RJVf6SGki4` — the agent needs this ID to target the append; it is fixed configuration, not something the agent discovers at runtime.
- **Header row** is bolded and frozen (`frozenRowCount = 1`).

**Columns (in order).** `ID` and the next four are written verbatim from the `postings` row; the last three are set/filled as noted:

| Column | Source |
| --- | --- |
| `ID` | `id` — the database primary key, written at append time (added 2026-08-16). The join key that lets `jsa refetch`'s scope lookup match a Sheet row back to its `postings` row without comparing URLs. |
| `Company` | `normalized_company` |
| `Title` | `title` |
| `URL` | `url` |
| `Date Posted` | `date_posted` |
| `Date Added` | Set by the agent at append time (the tracker-write date). |
| `Date Applied` | **Left blank; filled by the user** when they submit the application. |
| `Status` | **Left blank; set by the user** from a dropdown. |

**`Status` column.** A strict data-validation dropdown with seven enum values, each backed by a conditional-formatting color rule so the sheet is scannable at a glance: `No response`, `Rejected`, `Interview(s)`, `Final Round`, `Offer`, `Decided to pass`, `Accepted`. A blank Status (freshly appended row) matches no rule and stays uncolored until the user picks a value — intended.

**Append semantics.** The write is a single `sheets.spreadsheets.values.append` call against range `Applications!A:H` with `valueInputOption = USER_ENTERED` and `insertDataOption = OVERWRITE`, so a new row is added at the bottom without any row-index bookkeeping or read-modify-write. Concretely:

```
gws sheets spreadsheets values append \
  --params '{"spreadsheetId":"1DQNix3tZ9oFqfA9R2r0Npj1UWvAu2cEg_RJVf6SGki4","range":"Applications!A:H","valueInputOption":"USER_ENTERED","insertDataOption":"OVERWRITE"}' \
  --json   '{"values":[["<id>","<normalized_company>","<title>","<url>","<date_posted>","<date_added>","",""]]}'
```

**`OVERWRITE`, not `INSERT_ROWS` (corrected 2026-08-11).** This section originally specified `INSERT_ROWS`, on the reasoning that appending at the bottom avoids row-index bookkeeping and read-modify-write. That reasoning is sound but the option was wrong: `INSERT_ROWS` *inserts* a row, and measurement on the live sheet after two real appends showed two consequences the original reasoning missed.

1. **The `Status` dropdown is lost.** The inserted row lands outside the data-validation and conditional-formatting ranges, so it gets neither the enum dropdown nor the per-status colours. Worse, inserting above those ranges shifts them *down by one each time*, so every append drags the covered region further from the data. After two appends the rules covered rows 4–1002 while the data sat in rows 2–3. The sheet never self-corrects.
2. **The row inherits the header's formatting.** A row inserted directly beneath the bold grey header comes out bold and grey, and each later append inherits from that row, propagating indefinitely.

`OVERWRITE` writes into the sheet's already-existing blank rows rather than inserting. Sheets still locates the table server-side and writes after its last row, so the actual requirement above — no bookkeeping, no read-modify-write — still holds, while the written row keeps the validation, conditional formatting and default styling those rows already carry.

### Search Prompt Updates from "Ground Truth"

The search prompt is refined from Step 3's accumulated ground truth by an **automated weekly cron whose output is a pull request, never a direct commit** (decided 2026-08-22; the first rounds were run by hand in an interactive session, and that remains the escape hatch — the instructions live versioned in the repo as `refine_search_prompt.md`, runnable either way). The refiner is pinned to **`claude-opus-4-8` at `high` reasoning effort** — the task is bounded synthesis over a small corpus delta, not the exhaustive source-checking that justifies `xhigh` on the search side.

**Scope: only what is new.** Each run considers the decided rows with `decided_at` later than the last recorded run (`prompt_refinement_runs`), and exits quietly when there are none. It does not re-review the whole ground-truth corpus; aggregate SQL over history (counts, co-occurrence checks to confirm a pattern is really recurring) is fine, wholesale re-reading is not.

**Inputs, three of them first-class:**

1. **Explicit feedback** — `decision` + `fit_feedback` on searched postings, translated by the taxonomy below.
2. **Manual adds as the recall set.** Every `jsa add` (`search_agent = 'manual'`, decided Apply, usually no feedback text) is a role the user found that the search did not. For each, the refiner judges whether the prompt as written would have surfaced it and classifies the miss: a title-vocabulary gap, a source gap, an over-tight negative signal — or an unsupported ATS, which is *not* a prompt defect (out of scope by construction) but accumulates as evidence for the four-ATS table's additive escape hatch (see Daily Search).
3. **Implicit patterns mined from the JDs.** The database stores every posting's full `jd_markdown`, so the decisions themselves are labeled documents. The refiner pattern-matches across the JDs of Apply vs. Skip rows in scope to surface regularities the user never articulated — signals appearing implicitly in the decisions that no feedback comment names.

**The translation taxonomy** (how ground truth becomes prompt edits):

- **Explicit directives in feedback become hard exclusions.** Where feedback literally instructs a search change ("exclude Developer Relations roles"), encode it as one.
- **Objective, posting-verifiable criteria become filters.** A skip that failed a criterion directly and incontrovertibly checkable on the posting itself (a salary floor) is encoded as a filter with the same verifiability framing as the existing ones.
- **Repeated patterns — explicit or mined — become negative signals (deprioritize, never exclude) or sharpened target language.** The prompt's design principle holds: borderline fit is the user's call in review, not the agent's.
- **One-off judgment calls stay out entirely.** A single observation or role-specific critique is review-time judgment working as designed, not a search defect.

**The prompt stays standalone — a hard preference (2026-08-22).** `deep_research_prompt.md` is downstream of the ground truth but never refers to it: no watermark, no incorporated-through marker, no provenance annotations in the prompt text. Consequence, accepted: without in-prompt memory of which feedback produced which line, successive runs can oscillate wording. Provenance lives where it belongs — each run is one PR with its rationale, so git history traces every rule to the round that introduced it and a rule that hurt yield is revertible.

**The guardrail is the PR — deliberately no sentinels, no pinning test (decided 2026-08-22).** The refiner's edit lands as a pull request carrying the rationale and diff, and the user merges; that human review *is* the protection for the load-bearing machinery, and adding mechanical guards on top of it was considered and declined. The refiner's instructions (`refine_search_prompt.md`) carry the do-not-touch list — the JSON output contract, the `{{SEARCH_WINDOW}}` placeholder, the liveness/verifiability section and its ATS index-check table — and the diff is small enough that a violation is obvious at review. Two further instructional guardrails: **recall-first survives every round** (edits reduce wasted review time; they never create false negatives on roles the user would want), and genuine contradictions or ambiguities — which an interactive session would resolve with a clarifying question — go into the PR body as open questions, since the cron has no one to ask; anything too ambiguous to encode goes to `TODO.md` as a checkbox with reasoning.

**Each PR also syncs this document** — `prd.md` is the source of truth and must not drift from the prompt, so the criteria-describing sections here are updated in the same PR.

**Runtime.** The refiner needs a repo checkout and the ability to open a PR — none of which the Fly search image has — so it runs as a scheduled GitHub Actions workflow (weekly), with the Turso and Anthropic credentials as repository secrets. The Fly cron keeps Steps 1–2; this loop is deliberately a separate runtime shaped around the PR.

## Architecture

The system is split between a headless cloud runtime and interactive local sessions, coordinated through a single hosted database.

**Language/runtime: Python.** One language across cloud and local: the cloud cron uses the Claude Agent SDK for Python plus the Perplexity and Turso (libSQL) clients and the HTML→Markdown step; the local Steps 3–5 tooling (review CLI, `.docx`/`.pdf` generation) is also Python — the `.docx`/`.pdf` ecosystem (`python-docx` for the patch application, LibreOffice headless for PDF rendering) is what tipped the choice.

**Hosting: Fly.io (cron) + Turso (database).**

- **Fly.io** runs the automated Steps 1–2 (daily search, idempotent insert, full-JD fetch) and the prompt-refinement cron. The agent runs as a container (the Claude Agent SDK plus the Perplexity client and the ATS fetch + HTML→Markdown step), so a Fly Machine wakes on schedule for each run and stops after, keeping cost to pennies per month. **Scheduling logic lives in the container, not in Fly.** Fly Machines' native scheduled start offers only fuzzy `hourly`/`daily`/`weekly`/`monthly` intervals (no weekday selector, no per-run args), so the machine wakes on a plain `--schedule daily` and the `jsa cron` entrypoint self-gates against `pipeline.CRON_SCHEDULE`: it maps the ET weekday to that day's ordered `(agent, window_hours)` searches — Perplexity on Mon (72h) / Wed (48h) / Fri (48h), plus a weekly Claude 168h sweep on Friday that runs first — and exits quietly on off days, so a single cron entry drives the whole weekly cadence. API keys and the Turso auth token are injected via `fly secrets set`.

- **Turso** (hosted libSQL) holds the JD database. libSQL is SQLite-compatible, so the storage design above is unchanged, but both the cloud cron and the local interactive sessions connect to a single hosted copy — eliminating the divergence that a cron-writes-here / user-reviews-there split would otherwise create. This is what lets the daily search run even when the laptop is off while keeping Steps 3–5 as local Claude Code sessions.

**Division of responsibilities.**

- *Cloud (Fly.io):* Steps 1–2 and the prompt-refinement cron; writes to Turso.
- *Local (Claude Code over the same Turso DB):* Step 3 fit review, Step 4 (`jsa generate` — resume revisions, including `.docx`/`.pdf` generation), and Step 5 (the Google Sheet write). Google Sheets OAuth credentials live locally, keeping that credential off the server.

**Why Step 4 is local, not a second cloud job.** Steps 1–2 earn their place on Fly because they are scheduled and must run with the laptop closed, and because they are pure functions — text in, structured JSON out — writing only to the hosted DB. Step 4 is neither: it is triggered by the user's decision to apply (no schedule), and it reads local binaries (the `resume_templates/` library), writes local binaries (the `.docx`/`.pdf` the user submits), renders a PDF whose fidelity depends on the locally installed fonts, and finishes on the local `gws` grant. Running it on Fly would mean baking the resume templates into the image, mirroring the weekly-expiring `gws` token into `fly secrets`, and rendering the submittable PDF with substituted fonts — accidental complexity in service of a placement whose inputs and outputs are all local anyway.