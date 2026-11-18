"""Deliberate local edits through real storage/bridge contracts, no live services."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from source.server.companion import Companion
from source.server.light_archive import ConversationArchive
from source.server.schedule_store import ScheduleStore
from source.server.status_ui import WebviewBridge
from source.server.task_store import TaskStore


def test_personal_task_revision_domain_receipt_and_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv('FRIDAY_TASK_DB',str(tmp_path/'tasks.db'))
    bridge=WebviewBridge(None,SimpleNamespace(server_state={}))
    nonce=uuid4().hex
    saved=bridge.mutate_personal_task(None,'create',{'title':'Read source','notes':'Keep'},operation_id=nonce)
    duplicate=bridge.mutate_personal_task(None,'create',{'title':'Read source','notes':'Keep'},operation_id=nonce)
    assert saved['ok'] and duplicate['duplicate']
    store=TaskStore();row=store.list_tasks('all')['tasks'][0];ref='task-'+str(row['id'])
    assert len(store.list_tasks('all')['tasks'])==1
    for wrong in ('mem-1','exp-1','evt-1','n8n:1','lab:1'):
        assert not bridge.mutate_personal_task(wrong,'complete',{},row['revision'])['ok']
    done=bridge.mutate_personal_task(ref,'complete',{},row['revision'])
    assert done['ok'] and done['task']['status']=='done'
    assert not bridge.mutate_personal_task(ref,'update',{'title':'Stale'},row['revision'])['ok']
    current=TaskStore().list_tasks('all')['tasks'][0]
    assert current['notes']=='Keep' and current['status']=='done'
    assert not bridge.mutate_personal_task(ref,'delete',{},current['revision'])['ok']
    assert bridge.mutate_personal_task(ref,'reopen',{},current['revision'])['ok']
    assert not store.mutate(ref,'update',{'alien':'x'},current['revision'])['ok']


def test_calendar_atomic_local_edits_dst_reopen_and_wrong_source(tmp_path,monkeypatch):
    monkeypatch.delenv('FRIDAY_SCHEDULE_FILE',raising=False)
    path=tmp_path/'schedule.json';store=ScheduleStore(path)
    first=store.agenda();assert not path.exists()
    values=dict(title='Review',start='2026-10-25T02:30',all_day=False,duration_minutes=90)
    assert not store.mutate(None,'create',values,first['revision'])['ok']  # fold is ambiguous
    assert not path.exists()
    values['start']='2026-10-25T02:30+02:00'
    assert store.mutate(None,'create',values,first['revision'])['ok']
    current=store.agenda();event=current['events'][0]
    assert event['duration_minutes']==90 and event['start_label']=='02:30 CEST'
    assert not store.mutate('calendar:'+event['id'],'delete',{},first['revision'],True)['ok']
    before=path.read_bytes()
    monkeypatch.setattr('source.server.schedule_store.os.replace',lambda *a:(_ for _ in ()).throw(OSError('PRIVATE')))
    assert not store.mutate('calendar:'+event['id'],'update',values,current['revision'])['ok']
    assert path.read_bytes()==before
    monkeypatch.undo()
    monkeypatch.delenv('FRIDAY_SCHEDULE_FILE',raising=False)
    reopened=ScheduleStore(path);assert reopened.agenda()['events'][0]['id']==event['id']
    records=json.loads(path.read_text());records[0]['source']='remote';path.write_text(json.dumps(records))
    assert not reopened.mutate('calendar:'+event['id'],'delete',{},reopened.agenda()['revision'],True)['ok']
    records[0]['source']='local';records[0]['status']='proposed';path.write_text(json.dumps(records))
    assert not reopened.mutate('calendar:'+event['id'],'delete',{},reopened.agenda()['revision'],True)['ok']
    records[0].pop('status')
    records[0]['source']='local';records.append(dict(records[0]));path.write_text(json.dumps(records))
    assert not reopened.mutate('calendar:'+event['id'],'delete',{},reopened.agenda()['revision'],True)['ok']


def test_calendar_civil_dates_and_cross_instance_refresh(tmp_path,monkeypatch):
    monkeypatch.delenv('FRIDAY_SCHEDULE_FILE',raising=False)
    store=ScheduleStore(tmp_path/'schedule.json');other=ScheduleStore(store.path)
    assert store.mutate(None,'create',dict(title='Days',start='2026-11-14',end='2026-11-17',all_day=True),store.agenda()['revision'])['ok']
    assert other.agenda()['events'][0]['end_date_exclusive']=='2026-11-17'
    assert len(other.upcoming())==1
    assert not other.mutate('mem-1','delete',{},other.agenda()['revision'],True)['ok']


def test_archive_export_delete_active_stale_and_no_replay(tmp_path):
    archive=ConversationArchive(tmp_path/'archive.db');archive.recover()
    owner=SimpleNamespace(channel='local');generation=object()
    bridge=WebviewBridge(None,SimpleNamespace(light_archive=archive,server_state={}))
    async def turn():
        archive.admit(owner,generation,'hud','Saved question')
        archive.append(owner,generation,1,'<script>nothing executes</script>')
        reference='light:'+archive.turn.conversation
        revision=bridge.get_conversation_transcript(reference)['revision']
        assert not bridge.delete_conversation(reference,revision,True)['ok']
        archive.finish(owner)
        return reference
    reference=asyncio.run(turn())
    result=bridge.get_conversation_transcript(reference)
    assert not bridge.delete_conversation(reference,'old',True)['ok']
    exported=bridge.export_conversation(reference)
    assert exported['ok'] and '<script>' in exported['content'] and archive.turn is None
    assert not bridge.delete_conversation(reference,result['revision'])['ok']
    assert bridge.delete_conversation(reference,result['revision'],True)['ok']
    assert not bridge.delete_conversation(reference,result['revision'],True)['ok']
    assert not bridge.delete_conversation('legacy.json','anything',True)['ok']
    assert bridge.get_conversation_library()['rows']==[]
    reopened=ConversationArchive(archive.path);reopened.recover()
    assert WebviewBridge(None,SimpleNamespace(light_archive=reopened)).get_conversation_library()['rows']==[]


def test_preference_write_preserves_other_fields_and_refuses_stale(tmp_path):
    first=Companion(tmp_path/'prefs.json');second=Companion(first.path)
    revision=first.preferences()['revision']
    saved=first.save({'private_banter':'light'},revision)
    assert saved['ok']
    assert not second.save({'proactive_suggestions':True},revision)['ok']
    reopened=Companion(first.path).preferences()
    assert reopened['values']['private_banter']=='light'


def test_personal_failed_transaction_preserves_saved_row(tmp_path):
    store=TaskStore(str(tmp_path/'tasks.db'))
    row=store.add('Original',notes='Preserved')['task']
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    result=store.mutate('task-'+str(row['id']),'update',{'title':'Unsaved'},row['revision'])
    assert not result['ok'] and 'synthetic' not in result['message']
    assert store.list_tasks('all')['tasks'][0]==row
