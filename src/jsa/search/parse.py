"""Tolerantly extract the ``postings`` JSON object from a search agent's output.

Claude Deep Research has no server-side structured-output parameter, so its
response is free text that usually contains the JSON object (sometimes wrapped
in markdown fences or with stray prose). This module strips fences, isolates the
outermost JSON value, and validates it through the shared pydantic model — a
malformed response raises rather than silently inserting garbage.
"""

from __future__ import annotations

import json

from ..models import SearchOutput


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the closing fence.
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _extract_json(text: str) -> str:
    """Return the outermost JSON object or array substring in ``text``."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON object or array found in search output")
    start = min(starts)
    open_char = text[start]
    close_char = "}" if open_char == "{" else "]"
    end = text.rfind(close_char)
    if end < start:
        raise ValueError("unbalanced JSON in search output")
    return text[start : end + 1]


def parse_search_output(raw: str) -> SearchOutput:
    """Parse and validate a search agent's raw output into a ``SearchOutput``.

    Accepts either the full ``{"postings": [...]}`` object or a bare postings
    array (which is wrapped before validation).
    """
    candidate = _extract_json(_strip_fences(raw))
    data = json.loads(candidate)
    if isinstance(data, list):
        data = {"postings": data}
    return SearchOutput.model_validate(data)
