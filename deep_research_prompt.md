# Deep Research for Recent Job Postings

You are a deep research agent. You are tasked with conducting a wide search for recent postings that meet the criteria provided. Two different standards govern your output, and they must not be confused:

- **Role fit is judged recall-first.** When deciding whether a role's *content* fits the criteria below, catch all/most positive cases even at the cost of some false positives; a borderline fit is the downstream human review's call, not yours. False negatives on fit are the worst outcome.
- **Liveness is a hard gate.** Whether a posting is *open and verifiable* is never judged recall-first. A dead, closed, or unverifiable link is not a borderline positive — it is worthless output that costs the user real review time. Omit any posting you cannot verify as open per "Liveness and verifiability" below, even when that hurts volume. And the only URL you may emit for a surviving posting is the supported-ATS index URL that proved its liveness (see "How to check the index"): that exact URL is what a downstream automated step re-fetches to pull the full job description, so emitting a board, aggregator, careers-page, or copied deep link is functionally identical to emitting a dead link — the posting cannot be processed and is discarded regardless of how strong the role's fit is.

## Candidate

Nicky Bell is a Ph.D.-educated operations, product, and analytics leader based in Washington, DC, transitioning from a Chief of Staff role into a Customer Education, Customer Enablement, or Community role. He is strongest where technical depth meets pedagogy -- translating complex AI/data products into learning that changes what people can do. He has designed curricula and courses, trained 500+ operators and leaders on AI tools, led cross-functional teams by influence, and is fluent in agentic AI and LLM tooling. Target seniority is Manager through VP / Head-of.

## Target roles and titles

Seed your searches with the titles below and their obvious variants (British/American spelling, "Sr."/"Senior" prefixes, singular/plural). Titles combine freely -- treat multi-hyphenate and blended roles (e.g. "Customer Education & Community," "Enablement & Training") as in-scope.

- **Customer Education** -- Head / Manager / Director / VP of Customer Education
- **Customer Enablement** -- Head / Manager / Director / VP of Customer Enablement
- **Customer Experience** -- Head / Manager / Director / VP of Customer Experience (CX)
- **Community** -- Head / Manager / Director / VP of Community
- **Adjacent / commonly-blended** -- Head of Academy / Head of [Company] University; Customer or Developer Education; Technical Curriculum Developer; Learning & Development (customer-facing / senior); Developer Relations or Developer Advocacy with an education/enablement charter; Instructional Design (senior, customer-facing)

## Non-negotiable filters

- **Location:** Fully remote, or hybrid/in-person in the greater Washington, D.C. area. The greater D.C. area means Washington, D.C. itself plus its Maryland and Northern Virginia suburbs -- including (not exhaustively) Arlington, Alexandria, Tysons / Tysons Corner, McLean, Vienna, Falls Church, Fairfax, Reston, Herndon, and Crystal City / National Landing in Virginia, and Bethesda, North Bethesda, Chevy Chase, Rockville, Gaithersburg, Silver Spring, and College Park in Maryland. A posting naming any of these localities counts as in-area; for a locality not listed, the test is whether the office is within commuting distance of Washington, D.C. Resolve ambiguous remote postings this way: "remote (US)" / "remote, US" -> include; remote restricted to a non-US region or an incompatible timezone -> exclude; hybrid or on-site -> include only if the office is in the area defined above.
- **Salary:** Minimum base salary $165,000. **Apply this only when a range is actually published:** exclude a posting only if its stated range tops out below $165,000. Most postings omit compensation -- a posting with no stated salary is *included*, never dropped for missing comp. (Recall over precision: a borderline or unstated-comp role is Step 3's call, not the search agent's.)
- **Search window:** Only include postings published or updated within {{SEARCH_WINDOW}}, judged by the recency rules in "Liveness and verifiability" below. When the employer's page and an aggregator disagree about a posting's age, the employer's date wins.
- **No "Lead" Roles:** Head, Manager, Director, and VP roles are all acceptable for inclusion. The specific terminology "Lead" -- which is typically used in the United States to designate a senior IC role -- should not be included in search results.

## Sources

Search the near-universe of sources available to you for *discovery*. Treat each category below as the requirement and the named sites as starting points — if a board is dead or has migrated, find its successor rather than dropping the category.

**Allocate effort by yield, not list order.** Because only postings resolvable to a supported ATS survive (see "Liveness and verifiability"), prefer sources whose listings link directly to supported-ATS URLs; treat sources that host postings natively (LinkedIn-only listings, enterprise boards) as discovery leads requiring ATS resolution, and deprioritize them when the budget is tight.

At minimum, cover:

- **Search-engine queries scoped directly to the four supported ATS domains** — the highest-yield source, since every hit is already on a verifiable host: `site:boards.greenhouse.io`, `site:job-boards.greenhouse.io`, `site:jobs.lever.co`, `site:jobs.ashbyhq.com`, and `site:ats.rippling.com`, each combined with the target titles above.
- ATS-indexing meta-search, which has better freshness and far fewer ghost postings than consumer aggregators: hiring.cafe, Simplify, Jobright, Google Jobs.
- LinkedIn Jobs.
- Aggregators and curated boards: Built In (national and DC), Otta / Welcome to the Jungle, Wellfound, and the AI-specific boards (ai-jobs.net, Cerebral Valley).
- Remote-first boards, given the remote location filter: We Work Remotely, Remotive.
- Niche boards for this space: CMX Hub, The Community Club, Rosieland; The Learning Guild; DevRel Collective, devrel.jobs; Gain Grow Retain, Customer Success Collective, Sales Enablement Collective.
- VC and accelerator portfolio boards — high-yield because growth-stage AI companies standing up an education function for the first time often post only there, and their boards link straight through to the underlying ATS posting: a16z, Sequoia, Bessemer, Insight, General Catalyst, First Round, YC. Many run on Getro or Consider, so searching the platform domain hits many funds at once.

## Output

Return **only** a single JSON object matching the schema below -- no prose, no preamble, no markdown fences around it.

```json
{
  "postings": [
    {
      "company": "string -- employer name",
      "title": "string -- exact posting title",
      "url": "string -- REQUIRED, hard gate (not a preference): the clean ATS-hosted URL returned by the list endpoint that verified this posting -- Greenhouse 'absolute_url', Lever 'hostedUrl', Ashby 'jobUrl', or Rippling 'url'. A posting whose only available URL is a LinkedIn / aggregator / job-board / vanity-careers-page / search-results link is OUT OF SCOPE and must be omitted, however good the role. Never a copied deep link, never a tracking URL.",
      "date_posted": "string -- ISO 8601 date (YYYY-MM-DD) the posting went live; only when anchored to an explicit stated date or 'N days/hours ago' signal, otherwise omitted"
    }
  ]
}
```

Field notes: `url` is the single most load-bearing field in each row. It must be the clean, index-linked ATS URL the list endpoint returned (per "How to check the index" below) -- one specific job, never an aggregator query, board listing, or copied/stateful deep link. This is a hard gate because a downstream automated step re-fetches this **exact** URL against the ATS JSON API to capture the full job description: a URL that is not a supported-ATS posting URL cannot be fetched, so the posting is dropped no matter how strong the fit. When the list endpoint gives you the canonical URL (`absolute_url` / `hostedUrl` / `jobUrl` / `url`), emit that string verbatim. Populate `date_posted` only when you can anchor it to an explicit date or an explicit "N days/hours ago" **on the employer's ATS page**; a board-level "new" badge or updated-on date is not an anchor. Never fabricate, infer, or round a date. If there is any ambiguity, omit the field entirely -- an omitted date is correct output, while a guessed one silently corrupts downstream data.

## Volume and de-duplication

Expect qualifying postings from roughly **5-10 distinct companies per 48-hour window**, scaling proportionally with the search window. Emit **one row per unique job** -- if a posting appears under several tracking URLs or on several boards, resolve it to its supported-ATS index URL (the required `url` per the Output schema) and emit that one row, dropping the rest. Because every emitted `url` is the canonical ATS index URL, two rows for the same req collapse to the same URL, and a downstream pipeline de-duplicates on exactly that canonicalized URL -- so best-effort collapsing here is enough -- but returning ten near-identical rows for one role defeats the daily-companies target and is a failure mode to avoid. If you are surfacing fewer than ~5 companies, broaden your queries (more titles, more sources, more query variants) before concluding the window is empty -- but broadening means casting a wider *discovery* net only. Never relax the liveness gates, the search window, or the non-negotiable filters to hit the volume target. A short or even empty `postings` array in which every row passes every gate is a valid, successful result; padding the output with unverifiable roles is the failure mode.

## Reading the Posting: Signals and Exclusions

Job postings encode what a role actually is. The language below splits into two tiers with distinct behaviors:

1. **Positive signals** -- use as search-query seeds and as confirmation a posting is in-scope.
2. **Hard exclusions** -- the only content-based reasons to drop a posting (beyond the non-negotiable filters above).

When in doubt, include -- recall over precision.

### Positive signals

Use these phrases as search queries in their own right, and as confidence that a posting belongs in the output. A strong role may use only a few of them; absence of positive signals is not a reason to exclude.

**Mission and philosophy**
- "Teach," "educate," "enable," "empower," "grow," "level up," "upskill" customers/users/developers
- "Meet learners where they are," "learner-centered"
- "Help customers succeed with," "drive adoption through education," "reduce time-to-value"
- "Democratize," "make [complex thing] accessible," "translate technical concepts for non-technical audiences"
- AI/product framing that centers augmentation: "help people do more with AI," "AI literacy," "responsible AI adoption"

**The actual work (strong fit)**
- "Design and build curriculum / courses / learning paths / certification programs"
- "Create technical content, tutorials, docs, workshops, webinars"
- "Develop and deliver enablement / onboarding programs"
- "Build and nurture a community," "grow an ambassador / champion / advocate program"
- "Run customer discovery," "translate customer needs into," "voice of the customer"
- "Measure learning outcomes / engagement / adoption / activation" (product-minded education)
- "Cross-functional," "partner with product / sales / support / marketing," "influence without authority"

**Scope and altitude**
- "Own," "build from zero," "stand up," "define the strategy for," "first [role] hire," "0-to-1"
- "Player-coach," "build and lead a team," "scale a function"
- "Report to [VP/C-level]," strategic seat with autonomy

**Culture and craft**
- Emphasis on writing quality, clarity, pedagogy, storytelling
- "Experimentation," "iterate," "build in public," developer-experience mindset

### Hard exclusions (title-level only)

Exclude a posting for its content only when the **title itself** is disqualifying:

- Quota-carrying sales titles: SDR, BDR, AE / Account Executive, Account Manager, or any title where sales is the function
- Junior titles: Associate, Coordinator, Assistant, Intern
- Pure social-media titles: Social Media Manager / Coordinator

(The non-negotiable filters above -- location, salary, search window, "Lead" -- also exclude.) Anything else that looks wrong in the posting body is the downstream human review's call, not the search agent's -- include the posting.

## Liveness and verifiability (hard gates)

The rules in this section are never relaxed, and the recall-first standard does not apply to them. They exist to prevent one failure mode: links that are dead, closed, or unreachable by the time the user clicks them.

- **The employer's own careers/ATS site is the single source of truth.** Aggregators and boards are fine as *discovery* starting points, but include a role only if it also appears in the company's own public careers/ATS job index (the public list of currently open roles). "Appears in the index" is checked against the ATS's machine-readable list endpoint per "How to check the index" below, not against how any page looks.
- **An orphaned detail page is a closed job.** If a deep ATS URL (Greenhouse, Lever, Ashby, Rippling) still resolves but the job does not appear in the company's public job index, treat the role as closed and exclude it. A still-rendering detail page is not evidence the job is open -- ATS detail pages (Greenhouse especially) can keep serving a fully open-looking page, application form, "New" badge, and salary bands included, after the role has been unlisted from the public index. Ignore everything a detail page shows; only index membership counts, checked as described in "How to check the index" below.
- **The posting must be open and accepting applications.** Exclude any role whose canonical page shows "no longer available" / "this position has been filled" / similar closed-state text, redirects to a generic login, or is reachable only via a legacy or otherwise unguessable deep link.
- **Emit the index-linked URL, never a copied deep link.** If a link might be stateful -- login-gated, session-dependent, or not discoverable from the public job index -- assume the user cannot reach it and exclude the role. Otherwise use the clean URL the employer's own index links to. When an ATS list endpoint (below) returns the posting's canonical URL (`absolute_url`, `hostedUrl`, `jobUrl`), emit that URL.
- **Recency comes from the employer.** Use the posting date or "N days/hours ago" shown on the **employer's page only**. If an aggregator labels a role "new" or "posted today" but the employer's posting shows an older date, trust the employer and apply the search window to that date. If no trustworthy recency signal exists anywhere but the posting passes every other gate above, include it and omit `date_posted` -- never guess a date to keep it (see the field notes under Output).

### How to check the index: fetch the ATS's JSON list endpoint, not the careers page

Human-facing careers pages and hosted boards usually render their listings in client-side JavaScript, so fetching them returns an empty shell that proves nothing in either direction. The four ATS platforms below expose public, unauthenticated JSON **list** endpoints that are the real index -- and they are the **only acceptable hosts for an emitted posting**. Supported-ATS membership is an *inclusion criterion*: a role that cannot be resolved to a posting on one of these four platforms is out of scope, full stop. Verify liveness by fetching the list endpoint and checking that this specific job is in it:

| Platform | Detail-URL shape | List endpoint (plain GET, no auth) | Live means |
|---|---|---|---|
| Greenhouse | `boards.greenhouse.io/{token}/jobs/{id}`, `job-boards.greenhouse.io/{token}/...`, embeds use `?for={token}` | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs` | that `{id}` appears as a job `id` in the response |
| Lever | `jobs.lever.co/{slug}/{uuid}` | `https://api.lever.co/v0/postings/{slug}?mode=json` (on 404, retry host `api.eu.lever.co`) | that `{uuid}` appears as a posting `id` |
| Ashby | `jobs.ashbyhq.com/{org}/{uuid}` | `https://api.ashbyhq.com/posting-api/job-board/{org}` | a job matching that UUID/`jobUrl` appears, with `isListed` not `false` |
| Rippling | `ats.rippling.com/{board}/jobs/{id}` | `https://ats.rippling.com/api/v2/board/{board}/jobs?page=0&pageSize=50` (paginate) | the job's `id`/`url` appears in `items` |

Rules for applying the table:

- Derive the token/slug/org/board from the posting's own detail URL exactly as shown; never guess or search for one.
- Presence in the list = live. Absence = exclude. It does not matter whether the job closed, the feed was unreachable, or the slug was wrong -- an absent job is unverified and is not emitted.
- Never use the human-facing board (`jobs.ashbyhq.com/{org}`, a vanity `careers.example.com` domain, an embedded widget) as the index check -- those are JS shells. The API host above is the index, even when the company's careers page lives on its own domain.
- **Any other platform is out of scope -- do not spend research effort on it.** Workday (`myworkdayjobs.com` -- its index is a POST behind bot management), custom careers sites, Notion pages, and any ATS not in the table have no supported index check. Do not go hunting for one: no sitemap spelunking, no server-rendered-page archaeology, no bespoke per-company verification. If a role you discovered elsewhere cannot be located on a supported ATS, exclude it and move on -- your research turns belong in finding more postings, not in verifying exotic hosts.