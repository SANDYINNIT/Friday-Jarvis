"""FRIDAY's mission schedule (HUD calendar) — tiny JSON-backed store.

Voice journeys like "put something in my calendar tomorrow 10 to 11" land
here deterministically, so the Calendar tab in the HUD shows the SAME
events FRIDAY announced — instead of the model printing garbage code.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".friday", "schedule.json")

_lock = threading.Lock()
_store = None
_store_guard = threading.Lock()

_three_lock = None  # reserved for future cross-process locking


def _parse_natural_time(text, now=None):
    """Very small NL time parser: returns (start:datetime, duration_min) or None.

    Understands: "tomorrow", "today", "tonight"; "at 3", "at 3pm", "10 to 11
    am", "14:00-16:00", "in 2 hours"; days like "monday" resolve to next
    occurrence.time only.
    """
    now = now or datetime.now(timezone.utc).astimezone()
    text = str(text or "").lower()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = start_of_day
    if "tomorrow" in text:
        day = day + timedelta(days=1)
    elif "tonight" in text or "this evening" in text:
        day = day
    days_of_week = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for index, name in enumerate(days_of_week):
        if name in text:
            delta = (index - now.weekday()) % 7
            day = start_of_day + timedelta(days=delta if delta else 7)
    ranges = [
        __import__("re").findall(rf"(\d{{1,2}})(?::(\d{{2}}))?\s*(?:am|pm)?\s*(?:to|-|until|till)\s*(\d{{1,2}})(?::(\d{{2}}))?", text)
    ]
    if ranges and ranges[-1]:
        raw = ranges[-1][0]
        h1, m1, h2, m2 = raw
        h1, h2 = int(h1), int(h2)
        m1 = int(m1) if m1 else 0
        m2 = int(m2) if m2 else 0
        if "pm" in text and h1 < 12:
            h1 += 12
        if "pm" in text and h2 < 12:
            h2 += 12
        start = day.replace(hour=int(h1) % 24, minute=m1)
        if h2 <= h1 and h2 != 12:
            h2 += 12  # "10 to 9pm" style wraps past noon
        end = day.replace(hour=h2 % 24, minute=m2)
        duration = max(15, int((end - start).total_seconds() / 60))
        return start, duration
    single = __import__("re").findall(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    for match in single:
        hour, minute, suffix = match
        if hour is None:
            continue
        minute = int(minute) if minute else 0
        hour = int(hour)
        if suffix == "pm" and hour < 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
        if not suffix and hour <= 7:
            hour += 12
        listing = day.replace(hour=hour % 24, minute=minute)
        if listing <= now:
            listing = listing + timedelta(days=1)
        return listing, 60
    if "tomorrow" in text:
        listing = day.replace(hour=14, minute=0)  # 2pm default when no time given
        return listing, 60
    delta_hours = __import__("re").search(r"in\s+(\d+)\s+(hour|hours|minute|minutes)", text)
    if delta_hours:
        amount = int(delta_hours.group(1))
        unit = timedelta(hours=1) if delta_hours.group(2).startswith("hour") else timedelta(minutes=1)
        return now + (unit * amount), 60
    return None


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(path, events):
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(events, handle, ensure_ascii=False, indent=1)


class ScheduleStore:
    """Thread-safe mission schedule store for FRIDAY + the HUD."""

    def __init__(self, path=None):
        self.path = os.environ.get("FRIDAY_SCHEDULE_FILE", path or DEFAULT_PATH)
        self._lock = threading.Lock()
        with self._lock:
            self.events = _load(self.path)

    def add(self, title, start, duration_minutes=60, source="voice"):
        start_iso = start.isoformat()
        event = {
            "title": str(title)[:140],
            "start": start_iso,
            "duration_minutes": int(duration_minutes),
            "source": source,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.events.append(event)
            self.events.sort(key=lambda one: one.get("start", ""))
            _save(self.path, self.events)
        return dict(event)

    def upcoming(self, limit=20, include_past_done=True):
        with self._lock:
            items = list(self.events)
        return items[:200] if include_past_done else items

    def find_conflict(self, start, duration_minutes):
        with self._lock:
            for event in self.events:
                try:
                    start_existing = datetime.fromisoformat(event.get("start"))
                except (ValueError, TypeError):
                    continue
                end_existing = start_existing + timedelta(
                    minutes=event.get("duration_minutes", 60)
                )
                end_new = start + timedelta(minutes=duration_minutes)
                if start < end_existing and start_existing < end_new:
                    return event
        return None


def _singleton(path=None):
    global _store
    with _store_guard:
        if _store is None:
            _store = ScheduleStore(path)
        return _store
