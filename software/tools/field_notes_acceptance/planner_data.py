"""Planner browser fixtures backed by isolated real stores and a fake callback."""
from datetime import date, datetime, timedelta, timezone
import os

from source.server.pending_requests import PendingStore, decide_owner_request
from source.server.reminders import ReminderStore
from source.server.schedule_store import ScheduleStore, _save
from source.server.task_store import TaskStore


class PlannerData:
    def __init__(self, area, runtime, bridge):
        self.runtime, self.bridge = runtime, bridge
        self.area = area
        os.environ.pop('FRIDAY_SCHEDULE_FILE', None)
        self.callbacks = []
        self.pending = PendingStore(path=area / 'pending.json')
        self.first = self.pending.add('May n8n prepare the workshop brief?', source='n8n')
        self.second = self.pending.add('Which outline should the workflow use?', source='n8n')
        self.other = self.pending.add('Different decision domain', source='fixture-other')
        self.pending_save = self.pending._save_locked
        os.environ['FRIDAY_TASK_DB'] = str(area / 'personal-tasks.db')
        self.tasks = TaskStore()
        assert self.tasks.add('Review the workshop plan', due='someday', notes='Saved personal note')['ok']
        self.reminders = ReminderStore(area / 'reminders.db')
        self.reminders.create('Review the workshop plan', datetime(2026, 10, 25, 8, tzinfo=timezone.utc))
        self.schedule_path = area / 'schedule.json'
        self.schedule = ScheduleStore(self.schedule_path)
        self.schedule.add('Local midnight handoff', datetime.fromisoformat('2026-10-24T22:05:00+00:00'), 15, 'fixture text')
        self.schedule.add('First 02:30', datetime.fromisoformat('2026-10-25T00:30:00+00:00'), 90, 'fixture voice')
        self.schedule.add('Second 02:30', datetime.fromisoformat('2026-10-25T01:30:00+00:00'), 30, 'fixture text')
        self.schedule.add('Studio maintenance', date(2026,10,25), source='fixture local')
        self.schedule.add('Legacy time', datetime(2026,10,25,2,30), source='fixture legacy')
        _save(self.schedule_path, [*self.schedule.events, {'title':'Unconfirmed proposal','start':'2026-10-25','status':'proposed'}])
        self.schedule = ScheduleStore(self.schedule_path)
        self.attach()

    def attach(self):
        self.runtime.server_state.update(schedule_store=self.schedule, pending_store=self.pending,
                                         reminder_store=self.reminders)
        self.runtime.submit_pending_decision = self.submit

    def submit(self, correlation_id, answer):
        return decide_owner_request(self.pending, correlation_id, answer, source='text', notify=self.notify)

    def notify(self, request, status, answer):
        self.callbacks.append({'id':request.id,'status':status,'answer':answer})
        return {'http_status':200,'result':{'ok':False,'correlation_id':request.id}}

    def control(self, command):
        action = command['action']
        if action == 'planner:decision-write-failure':
            def fail():
                raise OSError('synthetic pending write failure')
            self.pending._save_locked = fail if command['value'] else self.pending_save
        elif action == 'planner:reopen':
            self.pending = PendingStore(path=self.pending.path)
            self.pending_save = self.pending._save_locked
            self.schedule = ScheduleStore(self.schedule_path)
            self.attach()
        elif action == 'planner:calendar-disconnected':
            self.runtime.server_state.pop('schedule_store',None)
        elif action == 'planner:calendar-error':
            corrupt = self.area / 'corrupt-schedule.json'
            corrupt.write_text('{broken',encoding='utf-8')
            self.runtime.server_state['schedule_store'] = ScheduleStore(corrupt)
        elif action == 'planner:calendar-empty':
            self.runtime.server_state['schedule_store'] = ScheduleStore(self.area / 'empty-schedule.json')
        elif action == 'planner:calendar-connected':
            self.runtime.server_state['schedule_store'] = self.schedule
        elif action == 'planner:decision-disconnected':
            self.runtime.submit_pending_decision = None
        elif action != 'planner:snapshot':
            raise ValueError('Unknown planner fixture action')
        return {'first':self.first.id,'second':self.second.id,'other':self.other.id,
                'callbacks':self.callbacks,'tasks':self.bridge.get_tasks(),'schedule':self.bridge.get_schedule()}
