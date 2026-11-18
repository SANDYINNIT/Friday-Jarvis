"""Second pre-user-test pass: real isolated stores, no external services."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from source.server.status_ui import WebviewBridge
from source.server.task_store import TaskStore


@pytest.mark.parametrize('delete_path',['ui','capability'])
def test_deleted_task_receipts_cannot_return_private_notes_or_claim_saved(tmp_path,delete_path):
    store=TaskStore(str(tmp_path/'tasks.db'));nonce=uuid4().hex
    values={'title':'Delete this fixture','notes':'Selected private fixture notes'}
    created=store.mutate(None,'create',values,operation_id=nonce)
    other=store.mutate(None,'create',{'title':'Keep this fixture','notes':'Unrelated notes'},operation_id=uuid4().hex)
    if delete_path=='ui':
        assert store.mutate(created['reference'],'delete',{},created['task']['revision'])['ok']
    else:assert store.delete(created['task']['id'])['ok']
    repeated=store.mutate(None,'create',values,operation_id=nonce)
    assert not repeated['ok'], 'A replay claims the deleted task still exists.'
    assert repeated['duplicate'] and repeated['deleted']==created['reference']
    assert store.list_tasks('all')['tasks']==[other['task']]
    with store._connect() as conn:
        receipts=[row[0] for row in conn.execute('SELECT result FROM personal_task_receipts')]
    assert 'Selected private fixture notes' not in '\n'.join(receipts)
    assert 'Unrelated notes' in '\n'.join(receipts)


def test_task_deletion_and_receipt_redaction_are_one_transaction(tmp_path):
    store=TaskStore(str(tmp_path/'tasks.db'))
    saved=store.mutate(None,'create',{'title':'Retain if cleanup fails'},operation_id=uuid4().hex)
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER deny_receipt_update BEFORE UPDATE ON personal_task_receipts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    result=store.mutate(saved['reference'],'delete',{},saved['task']['revision'])
    assert not result['ok']
    assert store.list_tasks('all')['tasks']==[saved['task']]


@pytest.mark.parametrize('field',['title','filename'])
def test_invalid_knowledge_metadata_does_not_initialize_storage(tmp_path,monkeypatch,field):
    path=tmp_path/'not-created'/'knowledge.db';monkeypatch.setenv('FRIDAY_KNOWLEDGE_DB',str(path))
    bridge=WebviewBridge(None,SimpleNamespace(server_state={}))
    result=bridge.import_knowledge('broken\ud800' if field=='title' else 'Valid title',
        'Valid source text','broken\ud800.md' if field=='filename' else None)
    assert not result['ok']
    assert not path.exists(), 'Invalid metadata created an empty failed-import database.'
    assert not path.parent.exists()


@pytest.mark.parametrize('action',['update','withdraw'])
def test_stale_knowledge_actions_cannot_create_a_missing_store(tmp_path,monkeypatch,action):
    from source.server.local_knowledge import KnowledgeStore
    path=tmp_path/'missing'/'knowledge.db';monkeypatch.setenv('FRIDAY_KNOWLEDGE_DB',str(path))
    if action=='update':
        bridge=WebviewBridge(None,SimpleNamespace(server_state={}))
        assert not bridge.import_knowledge('Stale source','Old update',None,'a'*32,'b'*32)['ok']
    else:assert KnowledgeStore(path).withdraw('a'*32,'b'*32) is False
    assert not path.exists(), 'A refused stale source action initialized a new database.'
    assert not path.parent.exists()


def test_task_operations_close_owned_connections_on_success_and_failure(tmp_path,monkeypatch):
    from source.server import task_store
    original=task_store.sqlite3.connect;opened=[]
    class Tracked(task_store.sqlite3.Connection):
        was_closed=False
        def close(self):self.was_closed=True;super().close()
    def connect(*args,**kwargs):
        kwargs['factory']=Tracked
        conn=original(*args,**kwargs);opened.append(conn);return conn
    monkeypatch.setattr(task_store.sqlite3,'connect',connect)
    def closed():assert all(conn.was_closed for conn in opened), 'Task operations leave owned SQLite handles open until garbage collection.'
    try:
        store=TaskStore(str(tmp_path/'tasks.db'))
        row=store.add('Owned connection')['task'];closed()
        assert store.list_tasks('all')['tasks'];closed()
        assert store.summary()['ok'];closed()
        assert store.complete(row['id'])['ok'];closed()
        assert store.reopen(row['id'])['ok'];closed()
        with store._connect() as conn:
            conn.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        closed()
        row=store.list_tasks('all')['tasks'][0]
        assert not store.mutate('task-'+str(row['id']),'update',{'title':'Rejected'},row['revision'])['ok'];closed()
        assert store.delete(row['id'])['ok'];closed()
    finally:
        for conn in opened:conn.close()


def test_legacy_deleted_task_receipt_is_refused_without_backfill(tmp_path):
    store=TaskStore(str(tmp_path/'tasks.db'));nonce=uuid4().hex
    values={'title':'Old deleted task','notes':'Legacy deleted text'}
    saved=store.mutate(None,'create',values,operation_id=nonce)
    # Reproduce a deletion written by the previous version, which did not
    # retire receipts. No schema migration or automatic history scan is used.
    with store._connect() as conn:
        conn.execute('DELETE FROM tasks WHERE id=?',(saved['task']['id'],))
        receipt_before=conn.execute('SELECT result FROM personal_task_receipts WHERE operation=?',(nonce,)).fetchone()[0]
    replay=store.mutate(None,'create',values,operation_id=nonce)
    assert not replay['ok'] and replay['duplicate'] and replay['deleted']==saved['reference']
    assert 'Legacy deleted text' not in json.dumps(replay)
    with store._connect() as conn:
        assert conn.execute('SELECT result FROM personal_task_receipts WHERE operation=?',(nonce,)).fetchone()[0]==receipt_before
