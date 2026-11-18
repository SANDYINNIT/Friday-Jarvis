"""Tasks and Calendar projections against isolated real stores."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from source.server import schedule_store
from source.server.pending_requests import PendingStore
from source.server.reminders import ReminderStore
from source.server.schedule_store import ScheduleStore, calendar_projection
from source.server.status_ui import WebviewBridge
from source.server.task_store import TaskStore


def bridge(**state):
    return WebviewBridge(None, SimpleNamespace(server_state=state))


def test_task_sources_and_colliding_ids_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv('FRIDAY_TASK_DB', str(tmp_path / 'tasks.db'))
    tasks = TaskStore()
    created = tasks.add('Same title')
    assert tasks.complete(created['task']['id'])['ok']
    reminders = ReminderStore(tmp_path / 'reminders.db')
    reminders.create('Same title', datetime(2026, 11, 8, tzinfo=timezone.utc))
    pending = PendingStore(tmp_path / 'pending.json')
    first = pending.add('An exact request')
    other = pending.add('Other domain', source='different')
    assert pending.resolve(first.id, 'saved')
    out = bridge(reminder_store=reminders, pending_store=pending).get_tasks()
    assert out['sources'] == dict(tasks='ready', reminders='ready', pending='ready')
    assert out['tasks'][0]['id'] == out['reminders'][0]['id']
    assert out['tasks'][0]['status'] == 'done' and out['reminders'][0]['status'] == 'pending'
    assert out['pending'][0]['correlation_id'] == first.id and not out['pending'][0]['actionable']
    assert other.id not in [row['correlation_id'] for row in out['pending']]
    assert not out['decision_available']


def test_task_reader_marks_partial_sources_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv('FRIDAY_TASK_DB', str(tmp_path))  # directory, so SQLite refuses
    out = bridge().get_tasks()
    assert out['sources'] == dict(tasks='error', reminders='disconnected', pending='disconnected')
    assert all(not out[key] for key in ('tasks', 'reminders', 'pending'))


def test_calendar_midnight_dst_order_and_civil_dates():
    records = [
        {'title':'Second fold','start':'2026-10-25T01:30:00Z'},
        {'title':'First fold','start':'2026-10-25T00:30:00Z','duration_minutes':90},
        {'title':'Madrid midnight','start':'2026-10-24T22:05:00Z'},
        {'title':'Civil days','start':'2026-10-25','end':'2026-10-27'},
        {'title':'Unknown zone','start':'2026-10-25T02:30:00'},
        {'title':'Not a saved event','start':'2026-10-25','status':'proposed'},
    ]
    moment = datetime.fromisoformat('2026-10-24T22:20:00+00:00')
    result = calendar_projection(records, now=moment)
    assert result['today'] == '2026-10-25' and result['timezone'] == 'Europe/Madrid'
    civil, midnight, first, second, unknown = result['events']
    assert [r['title'] for r in result['events']] == ['Civil days','Madrid midnight','First fold','Second fold','Unknown zone']
    assert civil['kind'] == 'all_day' and civil['end_date_exclusive'] == '2026-10-27'
    assert midnight['date'] == '2026-10-25' and midnight['start_label'] == '00:05 CEST'
    assert first['start_label'] == '02:30 CEST' and first['end_label'] == '03:00 CET'
    assert second['start_label'] == '02:30 CET'
    assert unknown['kind'] == 'unplaced' and 'date' not in unknown


def test_calendar_reader_distinguishes_empty_corrupt_and_disconnected(tmp_path, monkeypatch):
    monkeypatch.delenv('FRIDAY_SCHEDULE_FILE', raising=False)
    assert bridge().get_schedule()['status'] == 'disconnected'
    path = tmp_path / 'schedule.json'
    store = ScheduleStore(path)
    assert bridge(schedule_store=store).get_schedule()['events'] == []
    assert not path.exists()
    path.write_text('{broken', encoding='utf-8')
    store = ScheduleStore(path)
    assert bridge(schedule_store=store).get_schedule()['status'] == 'error'
    with pytest.raises(OSError):
        store.add('Do not overwrite unread data', date(2026,11,8))
    assert path.read_text(encoding='utf-8') == '{broken'


def test_failed_schedule_write_does_not_publish_event(tmp_path, monkeypatch):
    monkeypatch.delenv('FRIDAY_SCHEDULE_FILE', raising=False)
    path = tmp_path / 'schedule.json'
    store = ScheduleStore(path)
    store.add('Persisted event', date(2026,11,8), source='text')
    prior = bridge(schedule_store=store).get_schedule()['events']
    def refused(*_args):
        raise OSError('synthetic atomic replace failure')
    monkeypatch.setattr(schedule_store.os, 'replace', refused)
    with pytest.raises(OSError):
        store.add('Unsaved event', date(2026,11,8))
    assert bridge(schedule_store=store).get_schedule()['events'] == prior
    assert bridge(schedule_store=ScheduleStore(path)).get_schedule()['events'] == prior
