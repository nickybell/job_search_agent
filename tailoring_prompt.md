# Resume Tailoring — per-job revision instructions

> **Placeholder (TK).** The real tailoring instructions are still to be
> written. Until they land, this file instructs a deliberately conservative
> pass so that running `jsa generate` is safe: few, high-confidence changes
> over aggressive rewriting. The file is a template interpolated per job by
> `jsa generate`; the **output contract at the bottom is load-bearing** and
> must survive future edits to the instructions above it.

You are tailoring a resume for one specific job application. Read the job
posting and the base resume below, then propose the minimal set of paragraph
revisions that make the resume speak to this posting.

Guidelines (placeholder until the real instructions are written — TK):

- Never invent experience, employers, titles, dates, or credentials. Only
  reframe what the base resume already claims.
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

## Base resume

Each paragraph is numbered `[P<n>]`. These ids are the only paragraphs you may
target; the numbering comes from the `.docx` and is not part of the text.

{{BASE_RESUME}}

## Output contract (load-bearing — do not change)

Return **only** a JSON object — no prose before or after — in exactly this
shape:

```json
{
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

- `paragraph` must be one of the `[P<n>]` ids shown above.
- `replacement` is the complete new text for that paragraph (it replaces the
  whole paragraph, not a fragment).
- `rationale` is required for every change; it becomes the changelog entry.
- Do not include a change whose `replacement` restates the paragraph
  unchanged.
