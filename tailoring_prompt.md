# Resume Tailoring — per-job revision instructions

> **Placeholder (TK).** The real tailoring instructions are still to be
> written. Until they land, this file instructs a deliberately conservative
> pass so that running `jsa generate` is safe: few, high-confidence changes
> over aggressive rewriting. The file is a template interpolated per job by
> `jsa generate`; the **output contract at the bottom is load-bearing** and
> must survive future edits to the instructions above it.

You are tailoring a resume for one specific job application. Read the job
posting and the resume template library below, pick the template whose role
family best matches the posting, then propose the minimal set of paragraph
revisions that make that template speak to this posting.

Guidelines (placeholder until the real instructions are written — TK):

- Never invent experience, employers, titles, dates, or credentials. Only
  reframe what the chosen template already claims.
- Prefer rewording an existing bullet to emphasize what the posting cares
  about over adding new material.
- Keep each replacement close to the original's length so the document's
  layout survives.
- Leave a paragraph alone when you have no clearly better version; an empty
  change list is a valid answer.

## The job

- **Title:** {{JOB_TITLE}}
- **Company:** {{COMPANY}}

## Job description

{{JOB_DESCRIPTION}}

## Resume templates

The library below holds one maintained resume per role family. Each template
is identified by its slug (the `### Template:` heading) and rendered as
numbered paragraphs. `[P<n>]` ids are **per template**, they are the only
paragraphs you may target, and the numbering comes from the `.docx` — it is
not part of the text.

**Pick the one template whose role family best matches this posting** and
patch it. Only when *no* template's family genuinely fits — the posting
belongs to a role category the library does not cover yet — set `new_family`
to a short kebab-case name for that new category, pick the *nearest* existing
template as `base`, and write your changes as the adaptation; the tailored
result will be saved back into the library as the new family's template.
Never force-fit a template; the library expands outward as necessary.

{{RESUME_TEMPLATES}}

## Output contract (load-bearing — do not change)

Return **only** a JSON object — no prose before or after — in exactly this
shape:

```json
{
  "base": "the slug of the template this patch targets",
  "base_rationale": "One sentence on why this template fits this posting.",
  "new_family": null,
  "summary": "One sentence on the overall tailoring approach.",
  "changes": [
    {
      "paragraph": 12,
      "replacement": "The paragraph's full new text.",
      "rationale": "Why this change serves this posting."
    }
  ]
}
```

- `base` is required and must be one of the template slugs shown above.
- `base_rationale` is required; it is recorded in the changelog the user
  reviews.
- `new_family` is null unless no template's role family fits (see above);
  then it is a short kebab-case slug naming the new category.
- `paragraph` must be one of the `[P<n>]` ids shown for the **chosen**
  template.
- `replacement` is the complete new text for that paragraph (it replaces the
  whole paragraph, not a fragment).
- `rationale` is required for every change; it becomes the changelog entry.
- Do not include a change whose `replacement` restates the paragraph
  unchanged.
