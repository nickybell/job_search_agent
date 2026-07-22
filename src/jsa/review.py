"""Step 3: the deterministic (no-LLM) fit-review loop.

Rating a posting Apply/Skip and capturing optional free-text feedback is pure
I/O, so this is a plain script rather than an agent conversation: it queries the
backlog of undecided rows, opens each posting's real URL in the browser, and
writes the decision straight back to Turso. Each decision is committed
immediately, so quitting mid-session loses nothing and the next run resumes the
remaining backlog.
"""

from __future__ import annotations

import subprocess

from . import db
from .config import Config

_DECISIONS = {"a": "Apply", "s": "Skip"}


def _open_in_browser(url: str) -> None:
    """Open the posting in Google Chrome so the user evaluates the live page."""
    try:
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)
    except OSError as exc:
        print(f"  (could not open browser: {exc})")


def _prompt_decision() -> str | None:
    """Ask for a decision; return 'Apply'/'Skip', or None to quit."""
    while True:
        choice = input("  [a]pply / [s]kip / [q]uit: ").strip().lower()
        if choice == "q":
            return None
        if choice in _DECISIONS:
            return _DECISIONS[choice]
        print("  Please enter a, s, or q.")


def run_review(config: Config) -> None:
    """Work through the backlog of postings awaiting a fit decision."""
    client = db.connect(config)
    db.init_db(client)
    try:
        rows = db.pending_review(client)
        if not rows:
            print("No postings awaiting review. \U0001f389")
            return

        print(f"{len(rows)} posting(s) awaiting review.\n")
        for index, row in enumerate(rows, start=1):
            posting_id, company, title, url, location, date_posted = row
            print(f"[{index}/{len(rows)}] {company} — {title}")
            if location:
                print(f"  Location: {location}")
            if date_posted:
                print(f"  Posted:   {date_posted}")
            print(f"  URL:      {url}")
            _open_in_browser(url)

            decision = _prompt_decision()
            if decision is None:
                print("\nStopping. Remaining postings stay in the queue.")
                return
            feedback = input("  Optional feedback (Enter to skip): ").strip() or None
            db.record_decision(client, int(posting_id), decision, feedback)
            print(f"  ✓ recorded: {decision}\n")

        print("Review complete.")
    finally:
        client.close()
