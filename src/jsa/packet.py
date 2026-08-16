"""Steps 1–2 of the Step 4 application packet: create and seed the directory.

Step 4 proper (the per-job resume tailoring) is still unbuilt — blocked on the
TK tailoring instructions and the ``.docx``/``.pdf`` toolchain — but the
subagent's first two actions are deterministic file I/O, so they are a plain
CLI command (``jsa packet``) on the same reasoning as review and track: no
model in the loop.

Per prd.md (Resume Revisions), the directory creation *is* the idempotency
guard: an atomic, fail-if-exists ``mkdir`` (no ``-p`` clobber) at
``~/Documents/Job Applications/{normalized_company} - {title_slug}``, composed
directly from the row's already filesystem-safe fields. If the directory
exists the row is skipped before any other work, so a packet already built —
or half-built by an interrupted run — is never clobbered. ``job_posting.md``
is then written inside from the row's captured ``jd_markdown``, so the
tailored resume and the source JD will live together. A row with no captured
JD still gets its directory; the gap is reported, not fatal (the same
degrade-don't-destroy stance as the pipeline).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import Config

DEFAULT_PACKETS_DIR = Path.home() / "Documents" / "Job Applications"


def packets_dir() -> Path:
    """The packet root, overridable via ``JSA_PACKETS_DIR`` (tests/scratch)."""
    override = os.environ.get("JSA_PACKETS_DIR")
    return Path(override) if override else DEFAULT_PACKETS_DIR


def packet_dir_name(normalized_company: str, title_slug: str) -> str:
    """The prd.md packet-directory name. Pure; both inputs are already fs-safe."""
    return f"{normalized_company} - {title_slug}"


@dataclass
class PacketResult:
    """What happened (or would happen) for one posting's packet directory."""

    posting_id: int
    company: str
    title: str
    path: Path
    status: str = ""  # "created" | "exists" | "would_create"
    has_jd: bool = False


@dataclass
class PacketSummary:
    """Counts from one packet pass, for the closing log line."""

    eligible: int = 0
    created: int = 0
    skipped_existing: int = 0
    missing_jd: int = 0

    def __str__(self) -> str:
        return (
            f"eligible={self.eligible} created={self.created} "
            f"skipped_existing={self.skipped_existing} missing_jd={self.missing_jd}"
        )


def run_packet(
    config: Config,
    *,
    posting_id: int | None = None,
    dry_run: bool = False,
) -> tuple[PacketSummary, list[PacketResult]]:
    """Create the packet directory (and ``job_posting.md``) for each queued row.

    The queue is ``db.pending_packets``: Apply-and-untracked by default, or one
    explicit Apply row via ``posting_id``. In ``dry_run`` nothing is touched;
    a would-be-skipped existing directory is still reported as such.
    """
    summary = PacketSummary()
    results: list[PacketResult] = []

    client = db.connect(config)
    db.init_db(client)
    try:
        rows = db.pending_packets(client, posting_id)
    finally:
        client.close()
    summary.eligible = len(rows)
    if not rows:
        return summary, results

    base = packets_dir()
    if not dry_run:
        # Creating the shared root is setup, not the guard — the guard is the
        # per-packet fail-if-exists mkdir below.
        base.mkdir(parents=True, exist_ok=True)

    for row in rows:
        pid, normalized_company, title_slug, company, title, jd_markdown = row
        result = PacketResult(
            posting_id=int(pid),
            company=company,
            title=title,
            path=base / packet_dir_name(normalized_company, title_slug),
            has_jd=bool(jd_markdown),
        )
        results.append(result)
        if dry_run:
            result.status = "exists" if result.path.exists() else "would_create"
        else:
            try:
                result.path.mkdir()  # atomic fail-if-exists: the idempotency guard
            except FileExistsError:
                result.status = "exists"
            else:
                result.status = "created"
                if jd_markdown:
                    text = jd_markdown if jd_markdown.endswith("\n") else jd_markdown + "\n"
                    (result.path / "job_posting.md").write_text(text, encoding="utf-8")
        if result.status == "exists":
            summary.skipped_existing += 1
        else:
            summary.created += 1
            if not result.has_jd:
                summary.missing_jd += 1
    return summary, results


def describe(result: PacketResult) -> str:
    """One human-readable line (or few) per queued posting."""
    label = f"[id {result.posting_id}] {result.company} — {result.title}"
    if result.status == "exists":
        return f"  = {label}: directory already exists, skipped\n      {result.path}"
    verb = "would create" if result.status == "would_create" else "created"
    lines = [f"  + {label}: {verb}", f"      {result.path}"]
    if result.has_jd:
        lines.append("      job_posting.md from the captured JD")
    else:
        lines.append(
            "      no captured JD — job_posting.md not written (try `jsa refetch "
            f"--id {result.posting_id}` first if the posting is still up)"
        )
    return "\n".join(lines)
