"""Sync newly tailored resumes into the bullet ground-truth library.

``resume_bullets.csv`` — living in the packets directory (the user's vault)
beside the resumes it indexes; ``JSA_BULLETS_DOC`` to override — is the
reconciled universe of claims across every resume actually sent: one row per
bullet, organized by employer role, with near-duplicate wordings collapsed to
a ``best`` version per claim and substantively different framings kept in
full as ``variant`` rows tagged by role category. ``jsa generate``
interpolates it into the tailoring prompt as ground truth, so new resumes
reuse canonical, vetted claims instead of re-paraphrasing them per run.

``jsa bullets`` keeps that CSV current. Like the prompt-refinement loop, its
incrementality lives in the database: each run considers only resume ``.docx``
files modified since the last recorded ``bullet_sync_runs`` row (on the
first-ever run, every resume) and exits quietly when there are none. The
in-scope resumes are pre-rendered to text here — bold preserved as ``**…**``,
the same inline convention the patch contract uses — and handed to a headless
vault-editing agent (``claude-sonnet-5`` at ``medium`` effort, Read/Edit
tools only: the task is classification and transcription against explicit
rules, not synthesis) that folds missing bullets into the CSV under the
reconciliation rules in ``bullet_sync_prompt.md``. A run records itself only
on success — an errored run records nothing, so its resumes are reconsidered
next time — and ``--baseline`` records the current state as synced without
invoking the model, the seed step right after the library is first built (or
hand-curated).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from docx import Document

from . import db
from .agent import prompt_path, run_agent
from .config import Config
from .docx_patch import iter_paragraphs
from .packet import packets_dir, vault_file

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
EFFORT = "medium"
BULLETS_FILENAME = "resume_bullets.csv"
_PROMPT_FILENAME = "bullet_sync_prompt.md"
# Headroom to read the CSV, edit per resume, and re-read to verify the result.
_MAX_TURNS = 60

# The sync step as an injectable callable (prompt -> final message), so tests
# swap the agent without touching the scoping and bookkeeping around it.
RunnerFn = Callable[[str], str]


class BulletSyncError(RuntimeError):
    """A precondition failed loudly, or the headless sync run ended in an error."""


def bullet_library_path() -> Path:
    """The bullet ground-truth CSV — in the vault beside the resumes it indexes."""
    return vault_file("JSA_BULLETS_DOC", BULLETS_FILENAME)


def is_resume_docx(name: str) -> bool:
    """Whether a packet-directory file name looks like a tailored resume. Pure."""
    if name.startswith("~$"):  # Word lock files
        return False
    if not name.lower().endswith(".docx"):
        return False
    return "coverletter" not in name.replace(" ", "").replace("_", "").lower()


def _parse_cutoff(cutoff: str) -> datetime:
    # bullet_sync_runs.run_at is SQLite's datetime('now'): UTC, second precision.
    return datetime.strptime(cutoff, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def resumes_since(cutoff: str | None) -> list[Path]:
    """Resume files in the vault modified since the last sync run, sorted.

    The vault is scanned one level deep (each packet directory), the layout
    ``jsa packet`` creates; a missing vault is an empty scope, not an error.
    No cutoff — the first-ever run — puts every resume in scope.
    """
    root = packets_dir()
    if not root.is_dir():
        return []
    paths = sorted(p for p in root.glob("*/*.docx") if is_resume_docx(p.name))
    if cutoff is None:
        return paths
    threshold = _parse_cutoff(cutoff)
    return [p for p in paths if datetime.fromtimestamp(p.stat().st_mtime, tz=UTC) > threshold]


def render_resume_text(path: Path) -> str:
    """One resume's paragraphs as plain text, bold preserved as ``**…**``.

    Walks table-cell paragraphs too (``iter_paragraphs`` — resumes are often
    laid out in tables) and merges adjacent runs of equal boldness so a bullet
    reads as one span, not per-run fragments.
    """
    lines: list[str] = []
    for paragraph in iter_paragraphs(Document(str(path))):
        segments: list[tuple[str, bool]] = []
        for run in paragraph.runs:
            bold = bool(run.bold)
            if segments and segments[-1][1] == bold:
                segments[-1] = (segments[-1][0] + run.text, bold)
            else:
                segments.append((run.text, bold))
        text = "".join(
            f"**{chunk}**" if bold and chunk.strip() else chunk for chunk, bold in segments
        ).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def render_resume_blocks(paths: list[Path]) -> str:
    """Every in-scope resume rendered for the sync agent, under its packet name."""
    blocks = [
        f"## {path.parent.name}\n\n(file: {path.name})\n\n{render_resume_text(path)}"
        for path in paths
    ]
    return "\n\n".join(blocks)


def load_bullet_sync_prompt(*, library_path: Path, resumes: str, path: Path | None = None) -> str:
    """Read the sync instructions and fill the library-path and resumes slots."""
    path = path or prompt_path(_PROMPT_FILENAME)
    text = path.read_text(encoding="utf-8")
    return text.replace("{{BULLET_LIBRARY_PATH}}", str(library_path)).replace(
        "{{RESUMES}}", resumes
    )


def run_sync_agent(prompt: str) -> str:
    """One headless SDK run over the vault; returns the final message.

    An error result raises ``BulletSyncError`` before the run is recorded, so
    its resumes are reconsidered on the next invocation.
    """
    options = ClaudeAgentOptions(
        model=MODEL,
        effort=EFFORT,
        # File tools over the vault only: the resumes are already rendered
        # into the prompt, so the agent reads and edits just the CSV.
        allowed_tools=["Read", "Edit"],
        permission_mode="bypassPermissions",
        cwd=str(bullet_library_path().parent),
        max_turns=_MAX_TURNS,
    )
    return run_agent(prompt, options, BulletSyncError, "bullet sync agent")


@dataclass
class BulletSyncSummary:
    """What one sync invocation considered and did."""

    considered: int = 0
    ran: bool = False
    changed: bool = False
    baseline: bool = False

    def __str__(self) -> str:
        return f"considered={self.considered} ran={self.ran} changed={self.changed}"


def run_bullets(
    config: Config,
    *,
    dry_run: bool = False,
    baseline: bool = False,
    runner: RunnerFn | None = None,
) -> BulletSyncSummary:
    """Fold bullets from resumes new since the last sync into the library.

    ``dry_run`` lists the scope without invoking the model or recording a run.
    ``baseline`` records the current scope as synced, also without the model —
    the seed step after the library is first built or hand-curated, so the
    next real run starts incremental. Otherwise: no new resumes is a quiet
    no-op that records nothing; a successful agent run is recorded whether or
    not it changed the CSV (considered ≠ changed, mirroring the refinement
    loop), and an agent error propagates before anything is recorded.
    """
    summary = BulletSyncSummary()
    runner = runner or run_sync_agent

    client = db.connect(config)
    db.init_db(client)
    try:
        cutoff = db.bullet_sync_cutoff(client)
        paths = resumes_since(cutoff)
        summary.considered = len(paths)
        if dry_run:
            label = cutoff or "none — first run, every resume is in scope"
            print(f"{len(paths)} resume(s) newer than the last sync (cutoff: {label})")
            for path in paths:
                print(f"  {path.parent.name} / {path.name}")
            return summary
        if baseline:
            summary.baseline = True
            db.record_bullet_run(client, considered=len(paths), changed=False)
            log.info("baseline recorded: %d resume(s) marked as already synced", len(paths))
            return summary
        if not paths:
            log.info("no resumes modified since the last sync (%s); nothing to fold", cutoff)
            return summary
        library = bullet_library_path()
        if not library.is_file():
            raise BulletSyncError(
                f"bullet library not found at {library} — build it first and record it "
                "with `jsa bullets --baseline` (see prd.md, Resume Revisions), or set "
                "JSA_BULLETS_DOC."
            )
        before = library.read_text(encoding="utf-8")
        prompt = load_bullet_sync_prompt(library_path=library, resumes=render_resume_blocks(paths))
        log.info("syncing %d resume(s) into %s (%s, effort=%s)", len(paths), library, MODEL, EFFORT)
        runner(prompt)
        summary.ran = True
        summary.changed = library.read_text(encoding="utf-8") != before
        db.record_bullet_run(client, considered=len(paths), changed=summary.changed)
        log.info("bullet sync recorded: %s", summary)
        return summary
    finally:
        client.close()
