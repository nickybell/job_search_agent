"""The ground-truth prompt-refinement loop (prd.md, Search Prompt Updates).

Translates newly accumulated Step 3 ground truth into proposed edits to
``deep_research_prompt.md`` — as a pull request, never a direct commit: the
human review of the diff is the guardrail, by explicit choice (no sentinels,
no pinning test). The refiner agent runs headlessly over the repo checkout
with file tools only, pinned to ``claude-opus-4-8`` at ``high`` effort — the
task is bounded synthesis over a small corpus delta, not the exhaustive
source-checking that justifies ``xhigh`` on the search side.

**Incrementality lives in the database, not the prompt** (a hard preference,
2026-08-22): each run considers only rows whose ``decided_at`` is newer than
the last recorded run in ``prompt_refinement_runs``, and exits quietly when
there are none. The search prompt itself stays standalone — no watermarks, no
ground-truth references — with oscillation across runs accepted; provenance
lives in PR history. A run is recorded whether or not its PR merges (a
rejected translation still *considered* its rows; re-deciding a posting is
how it re-enters scope), and a run that errors records nothing, so its rows
are reconsidered next time.

The ground truth handed to the agent is pre-fetched and rendered here — the
new rows in full (decision, feedback, and the stored JD, which is what the
implicit-pattern mining reads) plus a compact historical reference for
confirming recurrence — so the agent needs no database access of its own.

Drives the weekly GitHub Actions workflow (which commits the diff and opens
the PR from the ``.refine_pr_body.md`` this writes) and is equally runnable by
hand: ``jsa refine``.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import db
from .config import Config

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
EFFORT = "high"
_PROMPT_FILENAME = "refine_search_prompt.md"
# Where the agent's final message (the PR body) lands. Gitignored, so the
# workflow's commit of the refined prompt can never sweep it in.
PR_BODY_FILENAME = ".refine_pr_body.md"
_MAX_TURNS = 80

# The refinement step as an injectable callable (prompt -> final message), so
# tests swap the agent without touching the scoping and bookkeeping around it.
RunnerFn = Callable[[str], str]


def _repo_root() -> Path:
    # src/jsa/refine.py -> repo root is two parents up from src/jsa.
    return Path(__file__).resolve().parents[2]


def _default_prompt_path() -> Path:
    """Locate ``refine_search_prompt.md`` (repo root, same pattern as the other prompts)."""
    cwd_candidate = Path.cwd() / _PROMPT_FILENAME
    if cwd_candidate.is_file():
        return cwd_candidate
    return _repo_root() / _PROMPT_FILENAME


def render_ground_truth(rows: list[tuple]) -> str:
    """Render the in-scope rows for the refiner — full detail, JDs included. Pure."""
    blocks: list[str] = []
    for row in rows:
        (
            posting_id,
            company,
            title,
            url,
            location,
            date_posted,
            search_agent,
            decision,
            fit_feedback,
            jd_markdown,
            decided_at,
        ) = row
        lines = [
            f"## id {posting_id} — {company} — {title}",
            f"- decision: {decision} (recorded {decided_at})",
            f"- search_agent: {search_agent}"
            + (
                " — a manual add: the search did NOT surface this posting"
                if search_agent == "manual"
                else ""
            ),
            f"- url: {url}",
        ]
        if location:
            lines.append(f"- location: {location}")
        if date_posted:
            lines.append(f"- date_posted: {date_posted}")
        lines.append(f"- feedback: {fit_feedback}" if fit_feedback else "- feedback: (none)")
        lines.append("")
        lines.append("### Job description")
        lines.append("")
        lines.append(jd_markdown.strip() if jd_markdown else "(no JD captured)")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_history(history: list[tuple]) -> str:
    """Render the compact historical reference — counts plus one line per row. Pure."""
    if not history:
        return "(no previously decided postings)"
    applies = sum(1 for r in history if r[4] == "Apply")
    skips = sum(1 for r in history if r[4] == "Skip")
    lines = [f"{len(history)} decided posting(s) all-time: {applies} Apply, {skips} Skip.", ""]
    for posting_id, company, title, search_agent, decision in history:
        lines.append(f"- id {posting_id} [{decision}] {company} — {title} ({search_agent})")
    return "\n".join(lines)


def load_refine_prompt(*, ground_truth: str, history: str, path: Path | None = None) -> str:
    """Read the refinement instructions and fill the two data slots."""
    path = path or _default_prompt_path()
    text = path.read_text(encoding="utf-8")
    return text.replace("{{GROUND_TRUTH}}", ground_truth).replace("{{HISTORY}}", history)


async def _agent(prompt: str) -> str:
    options = ClaudeAgentOptions(
        model=MODEL,
        effort=EFFORT,
        # File tools over the checkout only: the ground truth is already in
        # the prompt, and the PR gate reviews every edit these tools make.
        allowed_tools=["Read", "Edit", "Grep", "Glob"],
        permission_mode="bypassPermissions",
        cwd=str(_repo_root()),
        max_turns=_MAX_TURNS,
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
                log.info("refinement agent finished ($%.4f)", cost)
    # The final message is the PR body; fall back to the last assistant text.
    return final_text or (assistant_text[-1] if assistant_text else "")


def run_refiner_agent(prompt: str) -> str:
    """One headless SDK run over the checkout; returns the final message."""
    return anyio.run(_agent, prompt)


def _changed_files() -> list[str]:
    """Tracked files with unstaged changes, via git. Best-effort: [] on failure."""
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True,
            text=True,
            cwd=_repo_root(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


@dataclass
class RefineSummary:
    """What one refinement invocation considered and produced."""

    considered: int = 0
    ran: bool = False
    changed_files: list[str] = field(default_factory=list)
    pr_body_path: Path | None = None

    def __str__(self) -> str:
        return f"considered={self.considered} ran={self.ran} changed={len(self.changed_files)}"


def run_refine(
    config: Config,
    *,
    dry_run: bool = False,
    runner: RunnerFn | None = None,
    pr_body_path: Path | None = None,
) -> RefineSummary:
    """Run one refinement pass over the ground truth new since the last run.

    No new ground truth → a quiet no-op that records nothing (the cutoff must
    not advance). ``dry_run`` reports the scope without invoking the agent or
    recording a run. Otherwise: the agent runs, its edits land in the working
    tree for the caller (the CI workflow, or a human) to review and commit,
    its final message is written to ``pr_body_path`` as the PR body, and the
    run is recorded — advancing the cutoff whether or not the PR ever merges.
    """
    summary = RefineSummary()
    runner = runner or run_refiner_agent

    client = db.connect(config)
    db.init_db(client)
    try:
        cutoff = db.refinement_cutoff(client)
        rows = db.rows_for_refinement(client, cutoff)
        summary.considered = len(rows)
        if not rows:
            log.info("no ground truth newer than the last run (%s); nothing to refine", cutoff)
            return summary
        prompt = load_refine_prompt(
            ground_truth=render_ground_truth(rows),
            history=render_history(db.decided_history(client)),
        )
        if dry_run:
            ids = ", ".join(str(row[0]) for row in rows)
            print(
                f"{len(rows)} decided posting(s) would be considered (ids: {ids}); "
                f"cutoff: {cutoff or 'none — first recorded run'}"
            )
            return summary

        # A dirty checkout must not read as the agent's work: only files that
        # change during the run count as proposed changes.
        baseline = set(_changed_files())
        log.info("refining from %d posting(s) (%s, effort=%s)", len(rows), MODEL, EFFORT)
        final_text = runner(prompt)
        summary.ran = True
        summary.changed_files = [f for f in _changed_files() if f not in baseline]

        body = final_text.strip() or "(the refinement agent returned no summary)"
        target = pr_body_path or (_repo_root() / PR_BODY_FILENAME)
        target.write_text(body + "\n", encoding="utf-8")
        summary.pr_body_path = target

        db.record_refinement_run(client, considered=len(rows), changed=bool(summary.changed_files))
        log.info("refinement run recorded: %s", summary)
        return summary
    finally:
        client.close()
