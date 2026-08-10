"""Step 3: the deterministic (no-LLM) fit-review loop.

Rating a posting Apply/Skip and capturing optional free-text feedback is pure
I/O, so this is a plain script rather than an agent conversation: it queries the
backlog of undecided rows, opens each posting's real URL in the browser, and
writes the decision straight back to Turso. Each decision is committed
immediately, so quitting mid-session loses nothing and the next run resumes the
remaining backlog.

**Decisions are revisable.** A judgment often changes while the comment is being
written, so there are two ways back: ``:a`` / ``:s`` at the feedback prompt flip
the decision for the posting in hand (keeping whatever comment follows the
command), and ``b`` at the decision prompt steps back to the previous posting,
showing its recorded decision (a bare Enter keeps it) with its comment
pre-filled for editing. The backlog is
captured once at session start, so a row that was just decided remains
reachable by stepping back even though it no longer matches the ``NULL
decision`` query.

**Editing is real editing.** Prompts go through :mod:`jsa.prompting`, which
supplies arrow keys, word deletion, and an editable pre-filled buffer -- none of
which bare ``input()`` provides.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from . import db, prompting
from .config import Config

_DECISIONS = {"a": "Apply", "s": "Skip"}

# Feedback-prompt commands. `:a`/`:s` flip the decision without abandoning the
# comment (anything after the command is kept as the feedback); `:b` discards
# and returns to the decision prompt for the same posting.
_AMEND_COMMANDS = {":a": "Apply", ":apply": "Apply", ":s": "Skip", ":skip": "Skip"}
_BACK_COMMANDS = {":b", ":back"}

_FEEDBACK_PROMPT = "  Feedback (Enter to skip, :a/:s to change the decision): "

_BANNER = (
    "Keys:  a = Apply   s = Skip   b = back one posting   q = quit\n"
    "       at the feedback prompt, ':a' / ':s' change the decision "
    "(text after it is kept),\n"
    "       ':b' returns to the decision prompt. Arrow keys and word delete work; "
    "Ctrl-X Ctrl-E opens $EDITOR.\n"
)


@dataclass
class Entry:
    """One posting in the session, plus whatever has been decided about it."""

    posting_id: int
    company: str
    title: str
    url: str
    location: str | None
    date_posted: str | None
    decision: str | None = None
    feedback: str | None = None
    recorded: bool = False


def parse_feedback_entry(raw: str) -> tuple[str | None, str | None, bool]:
    """Split a feedback-prompt line into (decision override, feedback, go back). Pure.

    A leading ``:a``/``:s`` (or ``:apply``/``:skip``) overrides the decision and
    everything after it is kept as the comment, so changing one's mind mid-
    sentence costs a Ctrl-A and three keystrokes rather than a retyped comment.
    Only a command in the *first* word is special; a comment that merely
    contains a colon is stored verbatim.
    """
    stripped = raw.strip()
    if stripped.lower() in _BACK_COMMANDS:
        return None, None, True
    head, _, rest = stripped.partition(" ")
    override = _AMEND_COMMANDS.get(head.lower())
    if override is not None:
        return override, rest.strip() or None, False
    return None, stripped or None, False


def _open_in_browser(url: str) -> None:
    """Open the posting in Google Chrome so the user evaluates the live page."""
    try:
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)
    except OSError as exc:
        print(f"  (could not open browser: {exc})")


def _render(entry: Entry, index: int, total: int) -> None:
    """Print the posting header, noting any decision already recorded."""
    marker = " (amending)" if entry.recorded else ""
    print(f"[{index + 1}/{total}]{marker} {entry.company} - {entry.title}")
    if entry.location:
        print(f"  Location: {entry.location}")
    if entry.date_posted:
        print(f"  Posted:   {entry.date_posted}")
    print(f"  URL:      {entry.url}")
    if entry.recorded:
        comment = entry.feedback or "(no feedback)"
        print(f"  Recorded: {entry.decision} - {comment}")


def _ask_decision(entry: Entry, *, can_go_back: bool) -> str:
    """Ask for a decision key; returns 'a', 's', 'b', 'q', or '' to keep."""
    choices = {"a": "Apply", "s": "Skip", "q": "quit"}
    label = "  [a]pply / [s]kip"
    if can_go_back:
        choices["b"] = "back"
        label += " / [b]ack"
    label += " / [q]uit"
    if entry.decision:
        # Amending: a bare Enter keeps what is recorded. The buffer is
        # deliberately NOT pre-filled here, unlike the feedback prompt -- on a
        # single-letter prompt a pre-filled 'a' turns the natural keystroke
        # into 'aa'. Pre-filling helps when the text is meant to be edited; on
        # a choice prompt it only gets in the way.
        choices[""] = "keep"
        label += f" (Enter keeps {entry.decision})"
    label += ": "
    return prompting.ask_choice(label, choices)


def _review_one(client, entry: Entry, *, can_go_back: bool) -> str:
    """Collect and commit one decision. Returns 'next', 'back', or 'quit'."""
    while True:
        choice = _ask_decision(entry, can_go_back=can_go_back)
        if choice == "q":
            return "quit"
        if choice == "b":
            return "back"
        # An empty choice is only offered while amending, and means "keep".
        decision = _DECISIONS[choice] if choice else entry.decision
        assert decision is not None

        restart = False
        feedback = entry.feedback
        while True:
            raw = prompting.ask(_FEEDBACK_PROMPT, default=entry.feedback or "")
            override, parsed, go_back = parse_feedback_entry(raw)
            if go_back:
                restart = True
                break
            if override is not None and override != decision:
                decision = override
                print(f"  decision changed to {decision}.")
            feedback = parsed
            break
        if restart:
            continue

        entry.decision = decision
        entry.feedback = feedback
        db.record_decision(client, entry.posting_id, decision, feedback)
        entry.recorded = True
        print(f"  recorded: {decision}\n")
        return "next"


def _offer_final_amend(entries: list[Entry]) -> bool:
    """At the end of the backlog, offer one more pass at the last entry.

    Without this, the final posting is the one decision a session can never take
    back -- there is no 'next posting' at whose prompt to press ``b``.
    """
    if not entries or not entries[-1].recorded:
        return False
    choice = prompting.ask_choice(
        "Backlog complete. [b] amend the last entry / [Enter] finish: ",
        {"": "finish", "b": "back"},
    )
    return choice == "b"


def run_review(config: Config) -> None:
    """Work through the backlog of postings awaiting a fit decision."""
    client = db.connect(config)
    db.init_db(client)
    try:
        rows = db.pending_review(client)
        if not rows:
            print("No postings awaiting review. \U0001f389")
            return

        entries = [Entry(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]
        total = len(entries)
        print(f"{total} posting(s) awaiting review.\n")
        print(_BANNER)

        index = 0
        while True:
            if index >= total:
                if _offer_final_amend(entries):
                    index = total - 1
                    continue
                break
            entry = entries[index]
            _render(entry, index, total)
            _open_in_browser(entry.url)
            outcome = _review_one(client, entry, can_go_back=index > 0)
            if outcome == "quit":
                decided = sum(1 for e in entries if e.recorded)
                print(f"\nStopping. {decided} decision(s) saved; the rest stay in the queue.")
                return
            index = index - 1 if outcome == "back" else index + 1

        print("Review complete.")
    except prompting.Quit:
        # Ctrl-C / Ctrl-D at a prompt: identical to `q` -- every decision up to
        # this point is already committed.
        print("\nStopping. Decisions already made are saved.")
    finally:
        client.close()
