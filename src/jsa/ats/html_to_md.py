"""HTML → Markdown conversion for captured job descriptions.

Greenhouse's ``content`` arrives HTML-entity-escaped and must be unescaped
before conversion; the other platforms serve real HTML.
"""

from __future__ import annotations

import html

from markdownify import markdownify as _markdownify


def html_to_markdown(raw_html: str, *, unescape: bool = False) -> str:
    """Convert an HTML job description to Markdown.

    Set ``unescape=True`` for Greenhouse ``content`` (entity-escaped HTML).
    """
    if unescape:
        raw_html = html.unescape(raw_html)
    return _markdownify(raw_html, heading_style="ATX").strip()
