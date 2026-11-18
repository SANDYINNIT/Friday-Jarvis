"""Automatic memory scratchpad.

Runs after each completed assistant turn: when FRIDAY wrote code or the user
described a project idea, it appends a compact summary to
``memory/project_notes.md``.  Idea-shaped requests also earn a spoken
acknowledgement.  Everything is best-effort and never blocks the turn.
"""

from __future__ import annotations

import os
import re
from collections import deque

from .friday_services import record_project_note


_IDEA_MARKERS = (
    "project idea",
    "idea",
    "note this down",
    "remember this project",
    "keep track of this idea",
    "build me",
    "make me a",
    "create a project",
    "start a new project",
)
_CODE_MARKERS = (
    "created",
    "implemented",
    "added",
    "fixed",
    "wrote",
    "new file",
    "refactored",
    "updated",
    "debugged",
)
_last_notes = deque(maxlen=8)


def _env_flag(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip() not in ("0", "false", "off", "no")


def _looks_like_idea(user_text):
    lower = str(user_text or "").lower()
    return any(marker in lower for marker in _IDEA_MARKERS)


def _looks_like_code_work(turn):
    actions = turn.get("actions") or []
    if any(action.get("tool") != "markdown" for action in actions):
        return True
    snippet = str(turn.get("assistant_snippet") or "").lower()
    return any(marker in snippet for marker in _CODE_MARKERS)


def _summary_line(turn):
    user = str(turn.get("user") or "").strip()[:160]
    snippet = str(turn.get("assistant_snippet") or "").strip().replace("\n", " ")[:240]
    if user and snippet:
        return f"{user} — {snippet}"
    return f"{user or 'FRIDAY turn'} — {snippet or 'completed a task.'}"


def maybe_record_project_note(turn):
    """Return ``"idea"`` (recorded + spoken), ``"code"`` (recorded silently), or None."""
    if not turn or not _env_flag("FRIDAY_NOTE_TAKER"):
        return None
    line = _summary_line(turn)
    if not line or line in _last_notes:
        return None
    is_idea = _looks_like_idea(turn.get("user"))
    is_code = _looks_like_code_work(turn)
    if not (is_idea or is_code):
        return None
    _last_notes.append(line)
    try:
        record_project_note(line)
    except Exception:
        return None
    return "idea" if is_idea else "code"