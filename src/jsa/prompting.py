"""Line-edited terminal input for the interactive local commands (Steps 3+).

Python's built-in ``input()`` reads a raw line: arrow keys, ⌥+delete, ^W and ^A
all arrive as escape bytes and land in the string as garbage, which makes
correcting a typo in a review comment effectively impossible. This module wraps
``prompt_toolkit`` to give those keys their usual meaning, and — the part the
review loop actually depends on — to pre-fill the buffer with an existing value
so amending a prior answer means *editing* it rather than retyping it.

The dependency is soft: if ``prompt_toolkit`` is missing, or stdin is not a TTY
(a piped test, the headless Fly container), everything degrades to ``input()``.
That keeps the cloud image working even though it never prompts.
"""

from __future__ import annotations

import sys
from typing import Any

try:  # soft dependency — see module docstring
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
except ImportError:  # pragma: no cover - exercised only without the dependency
    PromptSession = None  # type: ignore[assignment]
    InMemoryHistory = None  # type: ignore[assignment]


class Quit(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised when the user ends input with Ctrl-C / Ctrl-D.

    Callers treat it exactly like an explicit quit command, so abandoning a
    session mid-prompt never loses already-committed work.
    """


_session: Any = None


def _interactive() -> bool:
    """True when a real terminal is attached and prompt_toolkit is importable."""
    if PromptSession is None:
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # detached/closed streams
        return False


def _get_session() -> Any:
    """Lazily build the shared session (constructing one needs a live terminal)."""
    global _session
    if _session is None:
        _session = PromptSession(
            history=InMemoryHistory(),
            # Ctrl-X Ctrl-E hands the buffer to $EDITOR, for comments long
            # enough that inline editing stops being pleasant.
            enable_open_in_editor=True,
        )
    return _session


def ask(message: str, *, default: str = "") -> str:
    """Read one line, pre-filled with ``default`` and fully editable.

    Returns the stripped input. ``default`` is placed *in the buffer* (not shown
    as a bracketed hint), so the user can edit or clear it with the same keys
    they'd use anywhere else in their shell.
    """
    if not _interactive():
        return _ask_plain(message, default)
    try:
        return _get_session().prompt(message, default=default).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise Quit from exc


def _ask_plain(message: str, default: str) -> str:
    """Fallback path: stdlib ``input()``, with ``default`` offered as a hint.

    Importing ``readline`` is what gives even this path arrow keys and word
    deletion; it patches ``input()`` globally as an import side effect, so the
    import must happen here rather than being pruned as unused.
    """
    try:
        import readline  # noqa: F401  (imported for its side effect on input())
    except ImportError:  # pragma: no cover - readline is absent on some builds
        pass
    hint = f"[{default}] " if default else ""
    try:
        entered = input(f"{message}{hint}").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise Quit from exc
    return entered or default


def ask_choice(message: str, choices: dict[str, str], *, default: str = "") -> str:
    """Read one line until it matches a key in ``choices``; return that key.

    ``choices`` maps the accepted single-letter input to a human label used only
    in the retry message. Matching is case-insensitive.
    """
    while True:
        entered = ask(message, default=default).lower()
        if entered in choices:
            return entered
        labels = ", ".join(key or "Enter" for key in choices)
        print(f"  Please enter one of: {labels}.")
