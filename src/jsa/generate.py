"""Step 4: tailor a per-job resume and complete the application packet.

``jsa generate`` drains the Step 4 queue (``decision = 'Apply' AND
added_to_tracker = 0``; ``--id`` waives the tracker condition, never the Apply
one). For each row it ensures the packet directory and ``job_posting.md`` —
reusing the pure helpers ``jsa packet`` is built from, but with *ensure*
semantics rather than that command's standalone fail-if-exists skip: a bare
directory left by an interrupted run or a ``jsa refetch`` rebuild is re-entered
and completed, because the completion guard is ``added_to_tracker``, not
directory-exists (prd.md, revised 2026-08-21). It then tailors the resume in
one pass, renders the ``.docx``/``.pdf`` pair, writes ``resume_changelog.md``,
and finishes by invoking the Step 5 tracker append for that row — the seam
between Steps 4 and 5.

The tailoring mechanism (decided 2026-08-21) is a **structured patch**: the
model call — the Claude Agent SDK, headless, pinned to ``claude-opus-4-8``, no
inherited session state — sees the base resume as numbered paragraphs and
returns a JSON patch that ``docx_patch`` applies deterministically with
python-docx. The PDF is rendered with LibreOffice headless (``soffice``).

A row with no captured JD is never tailored blind: its directory is ensured,
but the tailoring and the tracker call are skipped and the row stays in the
queue (``jsa refetch --id`` is the usual fix). The escape hatch: a
``job_posting.md`` already in the packet directory (hand-filled — the path for
unsupported-ATS rows refetch can never reach) is used as the JD instead, and a
NULL ``jd_markdown`` never overwrites it.

Each eligible row is an independent unit of work — its own directory, its own
model call — so the queue runs on a bounded worker pool (default 3,
``JSA_GENERATE_WORKERS`` to override); the closing tracker appends are
serialized across workers to keep Step 5's one-row-at-a-time append semantics.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from docx import Document

from . import db
from .config import Config
from .docx_patch import apply_patch, parse_patch, render_changelog, render_numbered_text
from .packet import packet_dir_name, packets_dir, write_job_posting
from .search.prompt import EASTERN
from .tracker import TrackerError, run_tracker

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
_PROMPT_FILENAME = "tailoring_prompt.md"
_DEFAULT_WORKERS = 3
_SOFFICE_TIMEOUT = 180

# The tailoring step as an injectable callable (prompt -> raw model output), so
# tests — and any future alternative backend — swap the model without touching
# the surrounding file work.
TailorFn = Callable[[str], str]


class GenerateError(RuntimeError):
    """A precondition failed loudly (missing base resume, prompt, or soffice)."""


def _repo_root() -> Path:
    # src/jsa/generate.py -> repo root is two parents up from src/jsa.
    return Path(__file__).resolve().parents[2]


def _default_prompt_path() -> Path:
    """Locate ``tailoring_prompt.md`` (repo root, same pattern as the search prompt)."""
    cwd_candidate = Path.cwd() / _PROMPT_FILENAME
    if cwd_candidate.is_file():
        return cwd_candidate
    return _repo_root() / _PROMPT_FILENAME


def base_resume_path() -> Path:
    """``base_resume.docx`` — gitignored at the repo root, ``JSA_BASE_RESUME`` to override."""
    override = os.environ.get("JSA_BASE_RESUME")
    if override:
        return Path(override)
    return _repo_root() / "base_resume.docx"


def soffice_binary() -> str:
    """The LibreOffice CLI (overridable so tests can stub the PDF render)."""
    return os.environ.get("JSA_SOFFICE_BIN") or "soffice"


def _worker_count() -> int:
    raw = os.environ.get("JSA_GENERATE_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            log.warning("ignoring non-integer JSA_GENERATE_WORKERS=%r", raw)
    return _DEFAULT_WORKERS


def resume_file_stem(normalized_company: str, title_slug: str) -> str:
    """The resume file name (no extension). Pure.

    Per prd.md: space characters are removed from the resume file names, but
    not from the containing directory name.
    """
    title_part = title_slug.replace(" ", "")
    company_part = normalized_company.replace(" ", "")
    return f"NicholasBell_Resume_{title_part}_{company_part}"


def load_tailoring_prompt(
    *,
    title: str,
    company: str,
    jd_markdown: str,
    base_resume_text: str,
    path: Path | None = None,
) -> str:
    """Read the tailoring template and fill its per-job slots."""
    path = path or _default_prompt_path()
    text = path.read_text(encoding="utf-8")
    return (
        text.replace("{{JOB_TITLE}}", title)
        .replace("{{COMPANY}}", company)
        .replace("{{JOB_DESCRIPTION}}", jd_markdown)
        .replace("{{BASE_RESUME}}", base_resume_text)
    )


async def _query_model(prompt: str) -> str:
    options = ClaudeAgentOptions(
        model=MODEL,
        # A pure text→JSON pass: no tools, one turn, nothing interactive.
        allowed_tools=[],
        permission_mode="bypassPermissions",
        max_turns=1,
    )
    final_text = ""
    assistant_text: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    assistant_text.append(block.text)
        elif isinstance(message, ResultMessage):
            final_text = getattr(message, "result", "") or ""
            cost = getattr(message, "total_cost_usd", None)
            if cost is not None:
                log.info("tailoring call finished ($%.4f)", cost)
    return final_text or "\n".join(assistant_text)


def run_tailoring_model(prompt: str) -> str:
    """One headless SDK call, pinned model, no inherited session state."""
    return anyio.run(_query_model, prompt)


def render_pdf(docx_path: Path) -> Path:
    """Render ``docx_path`` to a sibling PDF via LibreOffice headless.

    soffice writes ``<stem>.pdf`` into ``--outdir``, which is already the
    packet directory, so no rename is needed. Raises unless the process exits
    zero *and* the PDF exists — soffice has been known to exit 0 on failure.
    """
    pdf_path = docx_path.with_suffix(".pdf")
    completed = subprocess.run(
        [
            soffice_binary(),
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(docx_path.parent),
            str(docx_path),
        ],
        capture_output=True,
        text=True,
        timeout=_SOFFICE_TIMEOUT,
    )
    if completed.returncode != 0 or not pdf_path.is_file():
        detail = (completed.stderr or completed.stdout).strip()
        raise GenerateError(f"PDF render failed (soffice exited {completed.returncode}): {detail}")
    return pdf_path


@dataclass
class GenerateResult:
    """What happened (or would happen) for one queued posting."""

    posting_id: int
    company: str
    title: str
    path: Path
    # "generated" | "skipped_no_jd" | "failed" | "would_generate" | "would_skip_no_jd"
    status: str = ""
    jd_source: str | None = None  # "db" | "packet" (a hand-filled job_posting.md)
    changes: int = 0
    tracked: bool = False  # appended to the Sheet in this run
    already_tracked: bool = False  # the closing track call no-opped (already there)
    track_error: str | None = None
    error: str | None = None


@dataclass
class GenerateSummary:
    """Counts from one generate pass, for the closing log line."""

    eligible: int = 0
    generated: int = 0
    skipped_no_jd: int = 0
    failed: int = 0
    tracked: int = 0
    track_failed: int = 0

    def __str__(self) -> str:
        return (
            f"eligible={self.eligible} generated={self.generated} "
            f"skipped_no_jd={self.skipped_no_jd} failed={self.failed} "
            f"tracked={self.tracked} track_failed={self.track_failed}"
        )


def run_generate(
    config: Config,
    *,
    posting_id: int | None = None,
    dry_run: bool = False,
    tailor: TailorFn | None = None,
) -> tuple[GenerateSummary, list[GenerateResult]]:
    """Tailor a resume packet for each queued row and track it (Step 4 → 5).

    The queue is ``db.pending_packets`` — the same eligibility ``jsa packet``
    uses (Apply + untracked by default; ``posting_id`` waives the tracker
    condition but never the Apply one). ``dry_run`` reports what each row
    would do without touching the filesystem, the model, or the Sheet.
    """
    summary = GenerateSummary()
    tailor = tailor or run_tailoring_model

    client = db.connect(config)
    db.init_db(client)
    try:
        rows = db.pending_packets(client, posting_id)
    finally:
        client.close()
    summary.eligible = len(rows)
    results: list[GenerateResult] = []
    if not rows:
        return summary, results

    base = packets_dir()

    if dry_run:
        for row in rows:
            results.append(_preview_one(row, base))
    else:
        # Preflight, loud and up front: half a queue failing row by row on a
        # missing binary or file helps nobody.
        resume_path = base_resume_path()
        if not resume_path.is_file():
            raise GenerateError(
                f"base resume not found at {resume_path} — put base_resume.docx at the "
                "repo root or set JSA_BASE_RESUME."
            )
        prompt_path = _default_prompt_path()
        if not prompt_path.is_file():
            raise GenerateError(f"tailoring prompt not found at {prompt_path}.")
        if shutil.which(soffice_binary()) is None:
            raise GenerateError(
                f"the {soffice_binary()!r} CLI was not found on PATH — rendering the "
                "PDF needs LibreOffice (headless)."
            )
        base.mkdir(parents=True, exist_ok=True)

        track_lock = threading.Lock()

        def worker(row: tuple) -> GenerateResult:
            return _generate_one(row, config, resume_path, prompt_path, tailor, track_lock)

        max_workers = min(_worker_count(), len(rows))
        if max_workers <= 1:
            results = [worker(row) for row in rows]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(worker, rows))

    for result in results:
        if result.status in ("generated", "would_generate"):
            summary.generated += 1
        elif result.status in ("skipped_no_jd", "would_skip_no_jd"):
            summary.skipped_no_jd += 1
        else:
            summary.failed += 1
        if result.tracked:
            summary.tracked += 1
        if result.track_error:
            summary.track_failed += 1
    return summary, results


def _preview_one(row: tuple, base: Path) -> GenerateResult:
    pid, normalized_company, title_slug, company, title, jd_markdown = row
    directory = base / packet_dir_name(normalized_company, title_slug)
    result = GenerateResult(posting_id=int(pid), company=company, title=title, path=directory)
    if jd_markdown:
        result.jd_source = "db"
        result.status = "would_generate"
    elif (directory / "job_posting.md").is_file():
        result.jd_source = "packet"
        result.status = "would_generate"
    else:
        result.status = "would_skip_no_jd"
    return result


def _generate_one(
    row: tuple,
    config: Config,
    resume_path: Path,
    prompt_path: Path,
    tailor: TailorFn,
    track_lock: threading.Lock,
) -> GenerateResult:
    pid, normalized_company, title_slug, company, title, jd_markdown = row
    directory = packets_dir() / packet_dir_name(normalized_company, title_slug)
    result = GenerateResult(posting_id=int(pid), company=company, title=title, path=directory)
    try:
        # 1. Ensure the packet: create-or-accept, never fail-if-exists. A bare
        # directory is resumable work, not a done job.
        directory.mkdir(parents=True, exist_ok=True)
        jd = jd_markdown
        if jd:
            write_job_posting(directory, jd)
            result.jd_source = "db"
        else:
            # The escape hatch: a hand-filled job_posting.md is *input* — read
            # it, never overwrite it from a NULL jd_markdown.
            hand_filled = directory / "job_posting.md"
            if hand_filled.is_file():
                jd = hand_filled.read_text(encoding="utf-8").strip() or None
                if jd:
                    result.jd_source = "packet"
        if not jd:
            result.status = "skipped_no_jd"
            return result

        # 2. The one-shot tailoring pass: numbered base text in, structured
        # patch out, applied deterministically to this in-memory copy.
        document = Document(str(resume_path))
        prompt = load_tailoring_prompt(
            title=title,
            company=company,
            jd_markdown=jd,
            base_resume_text=render_numbered_text(document),
            path=prompt_path,
        )
        log.info(
            "[id %d] tailoring resume for %s — %s (%s)", result.posting_id, company, title, MODEL
        )
        patch = parse_patch(tailor(prompt))
        applied = apply_patch(document, patch)
        result.changes = len(applied)

        stem = resume_file_stem(normalized_company, title_slug)
        docx_path = directory / f"{stem}.docx"
        document.save(str(docx_path))
        render_pdf(docx_path)

        # 3. The changelog, rendered from the applied patch.
        changelog = render_changelog(
            company=company,
            title=title,
            model=MODEL,
            date_generated=datetime.now(EASTERN).date().isoformat(),
            summary=patch.summary,
            applied=applied,
        )
        (directory / "resume_changelog.md").write_text(changelog, encoding="utf-8")
    except Exception as exc:
        log.warning("generate failed for id %d: %s", result.posting_id, exc)
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.status = "generated"

    # 4. The seam to Step 5: append this row to the tracker. Serialized so
    # parallel workers keep the one-row-at-a-time append semantics. A row
    # already tracked (a refetch-driven regenerate) no-ops here by design.
    try:
        with track_lock:
            track_summary = run_tracker(config, posting_id=result.posting_id)
    except TrackerError as exc:
        result.track_error = str(exc)
        return result
    if track_summary.eligible == 0:
        result.already_tracked = True
    elif track_summary.appended:
        result.tracked = True
    else:
        result.track_error = "the tracker append failed; the row stays in the Step 5 backlog"
    return result


def describe(result: GenerateResult) -> str:
    """One human-readable block per queued posting."""
    label = f"[id {result.posting_id}] {result.company} — {result.title}"
    if result.status == "would_generate":
        source = "the captured JD" if result.jd_source == "db" else "the hand-filled job_posting.md"
        return f"  + {label}: would tailor from {source}\n      {result.path}"
    if result.status == "would_skip_no_jd":
        return (
            f"  - {label}: would skip — no JD (run `jsa refetch --id {result.posting_id}`, "
            f"or paste the JD into job_posting.md)\n      {result.path}"
        )
    if result.status == "skipped_no_jd":
        return (
            f"  - {label}: skipped — no JD to tailor from. Run `jsa refetch --id "
            f"{result.posting_id}` (or paste the JD into {result.path / 'job_posting.md'}) "
            "and re-run; the row stays in the queue."
        )
    if result.status == "failed":
        return f"  ! {label}: failed — {result.error}\n      the row stays in the queue"
    lines = [
        f"  ✓ {label}: resume tailored ({result.changes} change(s))",
        f"      {result.path}",
    ]
    if result.jd_source == "packet":
        lines.append("      (JD read from the hand-filled job_posting.md)")
    if result.tracked:
        lines.append("      appended to the tracker Sheet (added_to_tracker = 1)")
    elif result.already_tracked:
        lines.append("      already in the tracker Sheet; append skipped")
    elif result.track_error:
        lines.append(f"      NOTE: tracker append failed — {result.track_error}")
    return "\n".join(lines)
