# Bullet Library Sync — instructions

You maintain the **bullet ground-truth library**: a CSV recording every bullet
used across all of the candidate's sent resumes, so future tailoring reuses
canonical, verified claims instead of re-paraphrasing them. Your job in this
run: fold the bullets from the new/modified resumes below into the library.

## The library

`{{BULLET_LIBRARY_PATH}}`

Read it first, in full. Columns:

- `employer_role` — the employer + position the bullet belongs to, matching
  the resume's role headers (e.g. `Keywell — Customer Success Manager & Chief
  of Staff`). Rows are grouped by role, in resume (reverse-chronological)
  order.
- `claim` — a short label for the underlying achievement, shared by every
  wording of it.
- `version` — `best` (the canonical wording of the claim) or `variant` (a
  substantively different framing of the same claim).
- `categories` — which role categories the wording has served,
  semicolon-separated: `enablement`, `education`, `customer-success`,
  `cs-ops`, `other`.
- `bullet` — the full bullet text, verbatim, with `**bold**` markers
  preserved.
- `source_resumes` — the company portion of each source packet's directory
  name, semicolon-separated (e.g. `Affirm`); when one company has several
  resumes, a short parenthetical disambiguates (e.g. `GitLab (Manager CS)`).
- `notes` — reconciliation notes (e.g. what distinguishes a variant).

## The resumes to fold in

Each block below is one resume, headed by its packet directory name
("Company — Title" — infer the resume's role category from the title).

{{RESUMES}}

## How to fold

For each bullet in each resume (experience bullets only — skip the summary
paragraph, the skills list, contact lines, section and role headers, and
education/credential lines):

1. **Find its claim** in the library: same employer role, same underlying
   achievement.
2. **Exact or near-exact match** (wording differences are trivial): do not
   add a row. Append this resume's packet name to the matched row's
   `source_resumes`, and add the resume's category to `categories` if new.
3. **Same claim, substantively different framing** (a different facet,
   emphasis, or audience — even on the same proof points): add a `variant`
   row under the same `claim`, with a one-line `notes` on what distinguishes
   it. Only if the new wording is clearly stronger *for the same framing* may
   you instead swap it into the `best` row and demote the old wording to a
   `variant` — do this sparingly.
4. **New claim** (no row covers the achievement): add a `best` row inside the
   right employer-role group.

Rules:

- **Never delete rows, never invent bullets, never merge distinct claims.**
- Preserve the CSV's validity (RFC-4180 quoting — bullets contain commas) and
  its role-major row order; new rows go inside their role's group.
- Preserve `**bold**` markers exactly as they appear in the resume text.
- Edit **only** the library CSV — no other file.

Finish with a short summary: rows added (by role), rows updated, and anything
ambiguous you left alone.
