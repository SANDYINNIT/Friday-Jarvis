"""FRIDAY's mission schedule (HUD calendar) — tiny JSON-backed store.

Voice journeys like "put something in my calendar tomorrow 10 to 11" land
here deterministically, so the Calendar tab in the HUD shows the SAME
events FRIDAY announced — instead of the model printing garbage code.
"""

import json
import hashlib
import os
import threading
import time
from uuid import uuid4
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

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
    return _schedule_snapshot(path)[0]


def _save(path, events):
    # Validate the exact bytes BEFORE atomic replacement. A write that exceeds
    # the reader's limits must preserve the previous usable calendar.
    if not isinstance(events,list) or len(events)>10000 or any(not isinstance(row,dict) for row in events):
        raise CalendarInputError('Local calendar exceeds its supported record limit.')
    encoded=json.dumps(events,ensure_ascii=False,indent=1).encode('utf-8')
    if len(encoded)>2_000_000:
        raise CalendarInputError('Local calendar exceeds its supported size.')
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=parent, mode="wb", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ScheduleStore:
    """Thread-safe mission schedule store for FRIDAY + the HUD."""

    def __init__(self, path=None):
        self.path = os.environ.get("FRIDAY_SCHEDULE_FILE", path or DEFAULT_PATH)
        self._lock = threading.Lock()
        try:
            self.events = _load(self.path)
            self._read_failed = False
        except (OSError, ValueError):
            self.events = []
            self._read_failed = True

    def add(self, title, start, duration_minutes=60, source="voice"):
        start_iso = start.isoformat()
        event = {
            "title": str(title)[:140],
            "start": start_iso,
            "duration_minutes": int(duration_minutes),
            "source": source,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        from .file_transaction import file_transaction
        with self._lock,file_transaction(self.path):
            try: records,_=_schedule_snapshot(self.path)
            except (ValueError,TypeError):raise OSError('Existing calendar could not be read; nothing was changed.') from None
            updated = sorted([*records, event], key=lambda one: one.get("start", ""))
            _save(self.path, updated)
            self.events = updated
        return dict(event)

    def agenda(self):
        with self._lock:
            records,revision=_schedule_snapshot(self.path)
            self.events=records;self._read_failed=False
        return {**calendar_projection(records),'revision':revision,'editable':True}

    def mutate(self, reference, action, values, expected, confirm=False):
        if action not in ('create','update','delete') or not isinstance(values,dict):
            return {'ok':False,'message':'Unsupported local calendar action.'}
        if action=='delete' and values:
            return {'ok':False,'message':'Delete accepts no event fields.'}
        if action=='delete' and confirm is not True:
            return {'ok':False,'message':'Confirm deletion of the exact local calendar event.'}
        if action!='create' and (not isinstance(reference,str) or not reference.startswith('calendar:')):
            return {'ok':False,'message':'A local calendar reference is required.'}
        try:
            from .file_transaction import file_transaction
            with self._lock,file_transaction(self.path):
                records,revision=_schedule_snapshot(self.path)
                if revision!=expected:return {'ok':False,'conflict':True,'message':'The calendar changed. Refresh before saving.'}
                if action=='create':
                    if reference is not None:raise ValueError('New calendar events have no prior reference.')
                    event={**_calendar_values(values),'event_id':'cal-'+uuid4().hex,'source':'local'}
                    records.append(event)
                else:
                    ids=_calendar_ids(records)
                    if ids.count(reference[9:])!=1:raise ValueError('That exact local event is unavailable or ambiguous.')
                    index=ids.index(reference[9:]);event=records[index]
                    if event.get('status') in ('proposed','proposal','draft'):
                        raise CalendarInputError('Proposals are not saved calendar events. Use their owning review process.')
                    if event.get('source') not in (None,'local','friday','FRIDAY','voice','text'):
                        raise ValueError('This source is read-only; no remote calendar was changed.')
                    if action=='delete':records.pop(index)
                    else:
                        event={**event,**_calendar_values(values),'event_id':event.get('event_id') or 'cal-'+uuid4().hex}
                        records[index]=event
                _save(self.path,records)
                self.events=records;self._read_failed=False
                return {'ok':True,'event':event if action!='delete' else None,'message':'Local calendar saved. No external calendar was contacted.',
                        'revision':_schedule_snapshot(self.path)[1]}
        except CalendarInputError as error:return {'ok':False,'message':str(error)}
        except ValueError:return {'ok':False,'message':'Calendar date, time or stored format is invalid. Nothing was changed.'}
        except Exception:return {'ok':False,'message':'Local calendar change was not saved. Your edit is kept.'}

    def upcoming(self, limit=20, include_past_done=True):
        with self._lock:
            items,_ = _schedule_snapshot(self.path)
        return items[:200] if include_past_done else items

    def find_conflict(self, start, duration_minutes):
        if start.utcoffset() is None:
            return None  # Do not invent an instant for a legacy wall time.
        end_new = start.astimezone(timezone.utc) + timedelta(minutes=_duration(duration_minutes))
        start_new = start.astimezone(timezone.utc)
        with self._lock:
            events,_ = _schedule_snapshot(self.path)
            for event in events:
                if event.get('status') in ('proposed','proposal','draft') or event.get('all_day') is True:
                    continue
                try:
                    start_existing = datetime.fromisoformat(event.get("start"))
                    if start_existing.utcoffset() is None:continue
                    start_existing = start_existing.astimezone(timezone.utc)
                    end_existing = start_existing + timedelta(minutes=_duration(event.get('duration_minutes',60)))
                except (ValueError, TypeError, OverflowError):
                    continue
                if start_new < end_existing and start_existing < end_new:
                    return event
        return None


def _schedule_snapshot(path):
    try:
        with open(path,'rb') as stream:raw=stream.read(2_000_001)
    except FileNotFoundError:raw=b''
    if len(raw)>2_000_000:raise ValueError('Local calendar exceeds its supported size.')
    records=json.loads(raw) if raw else []
    if not isinstance(records,list) or len(records)>10000 or any(not isinstance(r,dict) for r in records):
        raise ValueError('Local calendar format is unavailable.')
    return records,hashlib.sha256(raw).hexdigest()


def _calendar_ids(records):
    occurrences={};ids=[]
    for row in records:
        signature=hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()[:24]
        occurrences[signature]=occurrences.get(signature,0)+1
        stable=row.get('event_id')
        ids.append(stable if isinstance(stable,str) and re.fullmatch(r'cal-[a-f0-9]{32}',stable) else f'schedule-{signature}-{occurrences[signature]}')
    return ids


class CalendarInputError(ValueError):
    pass


def _calendar_values(values):
    if set(values)-{'title','start','all_day','duration_minutes','timezone','end'}:
        raise CalendarInputError('Unknown calendar field.')
    title=values.get('title','')
    if not isinstance(title,str) or not title.strip() or len(title)>300:raise CalendarInputError('An event needs a title of at most300 characters.')
    if type(values.get('all_day',False)) is not bool:raise CalendarInputError('Invalid all-day choice.')
    start=values.get('start','')
    if not isinstance(start,str) or len(start)>80:raise CalendarInputError('Invalid event date/time.')
    if values.get('all_day'):
        day=date.fromisoformat(start);end=date.fromisoformat(values.get('end') or (day+timedelta(days=1)).isoformat())
        if end<=day:raise CalendarInputError('All-day end must follow its start.')
        return {'title':title.strip(),'start':day.isoformat(),'end':end.isoformat(),'all_day':True}
    moment=datetime.fromisoformat(start)
    if moment.utcoffset() is None:
        zone=ZoneInfo(values.get('timezone','Europe/Madrid'))
        candidates=[moment.replace(tzinfo=zone,fold=f) for f in (0,1)]
        valid=[m for m in candidates if m.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)==moment]
        if len({m.utcoffset() for m in valid})!=1:raise CalendarInputError('This local time is ambiguous or does not exist. Supply an explicit UTC offset.')
        moment=valid[0]
    duration=values.get('duration_minutes',60)
    if type(duration) is not int or not 1<=duration<=10080:raise CalendarInputError('Duration must be1–10080 minutes.')
    return {'title':title.strip(),'start':moment.isoformat(),'all_day':False,'duration_minutes':duration}


def _date_only(value):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        day = date.fromisoformat(value)
        return day if day.isoformat() == value else None
    except ValueError:
        return None


def _duration(value):
    # Retain unambiguous legacy integer strings; never round floats or bools.
    if isinstance(value,str) and re.fullmatch(r'[0-9]{1,5}',value):value=int(value)
    if type(value) is not int or not 1<=value<=10080:
        raise ValueError('Saved event duration is invalid.')
    return value


def _timed_event(record, zone):
    start = datetime.fromisoformat(record.get("start", ""))
    if start.utcoffset() is None:
        return {"kind": "unplaced", "reason": "The saved time has no timezone."}
    end = start.astimezone(timezone.utc) + timedelta(minutes=_duration(record.get("duration_minutes",60)))
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    return {"kind": "timed", "date": local_start.date().isoformat(), "start": local_start.isoformat(),
            "end": local_end.isoformat(), "start_epoch": start.timestamp(),
            "start_label": local_start.strftime("%H:%M %Z"), "end_label": local_end.strftime("%H:%M %Z"),
            "end_date": local_end.date().isoformat()}


def calendar_projection(records, now=None):
    """Display persisted events without interpreting local wall times as instants."""
    zone = ZoneInfo("Europe/Madrid")
    moment = (now or datetime.now(timezone.utc)).astimezone(zone)
    events, occurrences = [], {}
    identities=_calendar_ids([r for r in records if isinstance(r,dict)])
    from collections import Counter
    identity_counts=Counter(identities)
    for record_index,record in enumerate(r for r in records if isinstance(r,dict)):
        if not isinstance(record, dict) or record.get("status") in ("proposed", "proposal", "draft"):
            continue
        raw = record.get("start")
        signature = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:24]
        occurrences[signature] = occurrences.get(signature, 0) + 1
        item = {"id": identities[record_index], "title": record.get("title") or "Untitled event",
                "source": record.get("source") or "Source not recorded", "stored_start": raw or "",
                "duration_minutes": record.get("duration_minutes",60),
                "editable": record.get('source') in (None,'local','friday','FRIDAY','voice','text')
                            and identity_counts[identities[record_index]]==1}
        day = _date_only(raw)
        if day is None and record.get("all_day") is True and isinstance(raw, str):
            day = _date_only(raw[:10])
        if day and record.get("all_day") is not False:
            end = _date_only(record.get("end"))
            item.update(kind="all_day", date=day.isoformat(), end_date_exclusive=(end if end and end > day else day + timedelta(days=1)).isoformat())
        elif record.get("all_day") is True:
            item.update(kind="unplaced", reason="The saved all-day date could not be read.")
        else:
            try:
                item.update(_timed_event(record, zone))
            except (TypeError, ValueError, OverflowError):
                item.update(kind="unplaced", reason="The saved event time could not be read.")
        events.append(item)
    events.sort(key=lambda event: (event.get("date", "9999-99-99"), event["kind"] != "all_day",
                                  event.get("start_epoch", 0)))
    return {"available": True, "timezone": zone.key, "today": moment.date().isoformat(),
            "read_at": moment.isoformat(), "events": events, "count": len(events)}


def _singleton(path=None):
    global _store
    with _store_guard:
        if _store is None:
            _store = ScheduleStore(path)
        return _store
