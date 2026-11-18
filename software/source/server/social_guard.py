"""Social call guardrail.

When a voice call app (Discord, WhatsApp, Instagram, Telegram) is running,
FRIDAY must stay completely silent so it never interrupts a personal call.
The app scan is cached in a background thread (psutil scan every few seconds)
so the voice loop never blocks on process enumeration.
"""

from __future__ import annotations

import os
import threading
import time

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


DEFAULT_SOCIAL_APPS = {"discord.exe", "whatsapp.exe", "instagram.exe", "telegram.exe"}


def _env_flag(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip() not in ("0", "false", "off", "no")


def _scan_active(apps):
    if psutil is None:
        return False
    try:
        for proc in psutil.process_iter(attrs=["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in apps:
                return True
    except Exception:
        return False
    return False


class SocialGuard:
    """Background call-aware status feed safe for concurrent reads."""

    def __init__(self):
        self._active = False
        self._stop = threading.Event()
        self._thread = None
        self._apps = set(DEFAULT_SOCIAL_APPS)

    def active(self):
        return self._active

    def active_apps(self):
        return sorted(self._apps)

    def _run(self):
        if not _env_flag("FRIDAY_SOCIAL_GUARD"):
            return
        self._active = _scan_active(self._apps)
        while not self._stop.wait(5.0):
            self._active = _scan_active(self._apps)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="friday-social-guard", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)