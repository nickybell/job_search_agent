"""Apply a structured tailoring patch to a ``.docx``, deterministically.

prd.md (Resume Revisions, decided 2026-08-21): the model never edits
``base_resume.docx`` itself. It sees the base resume as numbered paragraphs,
returns a JSON patch (paragraph id → replacement text + rationale), and this
module applies that patch to an in-memory copy of the document with
python-docx. The split is what buys reproducible reruns, formatting that
cannot break (only text inside existing paragraphs changes), and a changelog
rendered *from the applied patch* rather than a second model artifact that
could drift from what actually changed.

Everything here is pure logic over python-docx's in-memory ``Document``
objects — no network, no database, no filesystem — so it is directly testable
(per the repo convention of keeping pure logic I/O-free).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .search.parse import extract_json_text


class PatchError(ValueError):
    """The model's patch cannot be applied to the base resume as returned."""


class PatchChange(BaseModel):
    """One paragraph replacement, as the tailoring model returns it."""

    paragraph: int
    replacement: str
    rationale: str


class TailoringPatch(BaseModel):
    """The model's whole answer: an optional summary plus the change list.

    An empty ``changes`` list is a valid answer — the prompt explicitly allows
    \"no change is clearly better\" — and yields an unmodified copy of the base
    resume plus a changelog saying so.
    """

    summary: str | None = None
    changes: list[PatchChange] = Field(default_factory=list)


def parse_patch(raw: str) -> TailoringPatch:
    """Parse and validate the model's raw output into a ``TailoringPatch``.

    Tolerates markdown fences and surrounding prose (the same stance as the
    search-output parser); anything structurally wrong raises rather than
    letting a malformed patch half-apply.
    """
    try:
        data = json.loads(extract_json_text(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        raise PatchError(f"the tailoring output is not valid JSON: {exc}") from exc
    return TailoringPatch.model_validate(data)


def iter_paragraphs(document):
    """Every paragraph in the document, body first, then table cells.

    Resumes are frequently laid out in tables, which ``document.paragraphs``
    does not descend into — so table-cell paragraphs are walked too. Merged
    cells repeat the same underlying element across a row; deduplicate on the
    element's identity so a paragraph is numbered exactly once. (Tables nested
    inside cells are not descended into — not a layout resumes use.)
    """
    yield from document.paragraphs
    seen: set[int] = set()
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                key = id(cell._tc)
                if key in seen:
                    continue
                seen.add(key)
                yield from cell.paragraphs


def render_numbered_text(document) -> str:
    """The document as ``[P<n>] <text>`` lines — what the model sees.

    Empty paragraphs (spacers) are not rendered but keep their index, so the
    ids the model can target are exactly the ids shown, and they map straight
    back onto ``iter_paragraphs`` order in ``apply_patch``.
    """
    lines: list[str] = []
    for i, paragraph in enumerate(iter_paragraphs(document)):
        text = paragraph.text.strip()
        if text:
            lines.append(f"[P{i}] {text}")
    return "\n".join(lines)


@dataclass(frozen=True)
class AppliedChange:
    """One change as actually applied — the changelog's raw material."""

    paragraph: int
    before: str
    after: str
    rationale: str


def _replace_text(paragraph, new_text: str) -> None:
    """Swap a paragraph's text, keeping its leading run's character formatting.

    Assigning ``paragraph.text`` would replace every run with one unformatted
    run, resetting bold headers and the like to the style default. Writing the
    first run and blanking the rest preserves the leading run's formatting;
    mid-paragraph formatting collapses to it, which is acceptable for resume
    body text.
    """
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(new_text)
        return
    runs[0].text = new_text
    for run in runs[1:]:
        run.text = ""


def apply_patch(document, patch: TailoringPatch) -> list[AppliedChange]:
    """Apply ``patch`` to ``document`` in place; return what actually changed.

    A change targeting an id that was never offered — out of range, or an
    empty spacer paragraph — raises ``PatchError`` rather than guessing. A
    change whose replacement restates the paragraph unchanged is dropped
    silently (the contract forbids them, but a no-op is not worth failing a
    whole resume over).
    """
    paragraphs = list(iter_paragraphs(document))
    applied: list[AppliedChange] = []
    for change in patch.changes:
        if not 0 <= change.paragraph < len(paragraphs):
            raise PatchError(
                f"the patch targets paragraph {change.paragraph}, but the base resume "
                f"has paragraphs 0–{len(paragraphs) - 1}"
            )
        target = paragraphs[change.paragraph]
        before = target.text
        if not before.strip():
            raise PatchError(
                f"the patch targets empty paragraph {change.paragraph}, which was never "
                "offered in the numbered base text"
            )
        if change.replacement == before:
            continue
        _replace_text(target, change.replacement)
        applied.append(
            AppliedChange(
                paragraph=change.paragraph,
                before=before,
                after=change.replacement,
                rationale=change.rationale,
            )
        )
    return applied


def render_changelog(
    *,
    company: str,
    title: str,
    model: str,
    date_generated: str,
    summary: str | None,
    applied: list[AppliedChange],
) -> str:
    """Render ``resume_changelog.md`` from the applied patch. Pure.

    One addressable entry per change, each carrying its rationale — the
    artifact that lets an interactive review accept/reject change #N instead
    of re-deriving the whole diff by hand.
    """
    lines = [
        f"# Resume changelog — {company} — {title}",
        "",
        f"Tailored {date_generated} by `jsa generate` ({model}, structured patch "
        "applied to `base_resume.docx`).",
        "",
    ]
    if summary:
        lines += [summary, ""]
    if not applied:
        lines += [
            "The tailoring pass proposed no changes — the base resume was emitted unchanged.",
            "",
        ]
        return "\n".join(lines)
    lines += [f"## Changes ({len(applied)})", ""]
    for n, change in enumerate(applied, start=1):
        lines += [
            f"### {n}. Paragraph {change.paragraph}",
            "",
            f"- **Before:** {change.before}",
            f"- **After:** {change.after}",
            f"- **Why:** {change.rationale}",
            "",
        ]
    return "\n".join(lines)
