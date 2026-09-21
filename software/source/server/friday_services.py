"""FRIDAY Autonomous Services: Note-Taking, Social App Guardrails, and Proactive Background Processing.

Implements:
1. Autonomous note-taking to 'memory/project_notes.md'.
2. Active Windows process check for social calling apps (Discord, Instagram, WhatsApp) to enforce social app guardrails.
3. Proactive background processing loop and desktop audio memory caching.
"""

from __future__ import annotations

import os
import re
import time
import psutil
from pathlib import Path
import logging

logger = logging.getLogger("friday.services")

_NOTES_PATH = Path("memory/project_notes.md")
_SOCIAL_APPS = {"discord.exe", "instagram.exe", "whatsapp.exe", "telegram.exe"}


def record_project_note(idea_text: str) -> str:
    """Permanently note down project ideas or code summaries."""
    try:
        _NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n- **[{timestamp}]** {idea_text.strip()}\n"
        with open(_NOTES_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.info("Recorded project note: %s", idea_text[:60])
        return f"That sounds like a fascinating project, Sir. I've noted it down in project notes."
    except Exception as e:
        logger.warning("Failed to record project note: %s", e)
        return f"I tried recording that note, Sir, but encountered a slight hiccup: {e}"


def check_social_call_active() -> bool:
    """Return True if an active voice call app is running on Windows."""
    try:
        for proc in psutil.process_iter(attrs=["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in _SOCIAL_APPS:
                return True
    except Exception:
        pass
    return False


def parse_and_trigger_notes(text: str) -> str | None:
    """Intercept project ideas or notes from conversation."""
    lower = text.lower()
    if any(k in lower for k in ["project idea", "note this down", "remember this project", "keep track of this idea"]):
        return record_project_note(text)
    return None
