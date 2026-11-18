"""Loopback account isolation with actual ASGI boundaries and synthetic stores."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from source.server.accounts import AccountStore,AccountsBoundary,attach_accounts,account_gateway
from test_request_reply_ownership import isolated_server

PASSWORD='A long synthetic fixture passphrase'


@pytest.fixture
def accounts(tmp_path):
    store=AccountStore(tmp_path/'accounts')
    a=store.provision('alice',PASSWORD,{'tasks:read','tasks:write'})
    b=store.provision('bob',PASSWORD,{'tasks:read'})
    return store,a,b


def test_disabled_diagnostic_passive_missing_and_exact_flag(tmp_path):
    class App:
        def add_middleware(self,kind,**kwargs):self.kwargs=kwargs
    root=tmp_path/'missing';app=App()
    attach_accounts(app,{'FRIDAY_ACCOUNTS':'0'})
    assert app.kwargs==dict(store=None,enabled=False) and not root.exists()
    attach_accounts(app,{'FRIDAY_ACCOUNTS':'1','FRIDAY_AUDIO_STARTUP_CHECK':'1'})
    assert app.kwargs==dict(store=None,enabled=False)
    with pytest.raises(ValueError):attach_accounts(app,{'FRIDAY_ACCOUNTS':'yes'})
    with pytest.raises(ValueError):attach_accounts(app,{'FRIDAY_ACCOUNTS':'1'})
    store=AccountStore(root)
    assert not root.exists()
    with pytest.raises(Exception):store.authenticate('a'*43)
    assert not root.exists()


def test_exact_sessions_permissions_tenant_collision_and_no_host_turn(accounts,tmp_path,monkeypatch):
    store,a,b=accounts
    # An inherited host selector must never redirect account resources.
    host=tmp_path/'SANDY-HOST.db';monkeypatch.setenv('FRIDAY_TASK_DB',str(host))
    host_calls=[]
    async def host_app(scope,receive,send):
        host_calls.append(scope['path']);raise AssertionError('Account reached host')
    app=AccountsBoundary(host_app,store,True)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',123)),base_url='http://127.0.0.1') as client:
            assert (await client.get('/accounts/me')).status_code==401
            token=(await client.post('/accounts/login',json={'username':'alice','password':PASSWORD})).json()['token']
            headers={'Authorization':'Bearer '+token}
            me=(await client.get('/accounts/me',headers=headers)).json()
            assert me['id']==a and me['host_owner'] is False
            saved=(await client.post('/accounts/tasks',headers=headers,json={'action':'create','values':{'title':'Alice only'}})).json()
            assert saved['ok']
            assert (await client.post('/accounts/chat',headers=headers,json={'text':'execute code'})).status_code==501
            bob=(await client.post('/accounts/login',json={'username':'bob','password':PASSWORD})).json()['token']
            bh={'Authorization':'Bearer '+bob}
            assert (await client.get('/accounts/tasks',headers=bh)).json()['tasks']==[]
            assert (await client.post('/accounts/tasks',headers=bh,json={'action':'delete','reference':saved['reference'],'revision':saved['task']['revision'],'values':{},'confirm':True})).status_code==403
            assert len((await client.get('/accounts/tasks',headers=headers)).json()['tasks'])==1
            assert (await client.get('/accounts/me',headers=[('Authorization','Bearer '+token),('Authorization','Bearer '+bob)])).status_code==401
            assert (await client.get('/accounts/me',headers={**headers,'Origin':'https://attacker.invalid'})).status_code==403
            assert (await client.get('/accounts/%6de',headers=headers)).status_code==404
            assert (await client.get('/accounts/me?target=bob',headers=headers)).status_code==404
            assert (await client.post('/accounts/logout',headers=headers,json={})).json()['ok']
            assert (await client.get('/accounts/me',headers=headers)).status_code==401
    asyncio.run(scenario())
    assert not host_calls and not host.exists()
    raw=store.path.read_bytes()
    assert PASSWORD.encode() not in raw


def test_expiry_disable_wrong_password_rate_limit_and_reopen(accounts):
    store,a,_=accounts
    assert store.login('alice','wrong','127.0.0.1') is None
    token=store.login('alice',PASSWORD,'127.0.0.1')
    assert AccountStore(store.root).authenticate(token)['id']==a
    store.clock=lambda:10**12
    assert store.authenticate(token) is None
    store.clock=__import__('time').time
    with store.connection(True) as conn:conn.execute('UPDATE accounts SET active=0 WHERE id=?',(a,))
    assert store.authenticate(token) is None
    assert store.login('alice',PASSWORD,'127.0.0.1') is None
    for _ in range(6):assert store.login('absent',PASSWORD,'::1') is None
    with store.connection() as conn:
        assert conn.execute('SELECT count FROM login_attempts WHERE peer=?',('::1',)).fetchone()[0]==5
    with pytest.raises(ValueError):store.provision('alice',PASSWORD)
    with pytest.raises(ValueError):store.provision('eve',PASSWORD,{'host:tools'})


def test_non_loopback_upgrade_oversize_no_host_execution(accounts):
    store,_,_=accounts
    async def host(*args):raise AssertionError('host route reached')
    async def scenario():
        for peer,headers,body,status in [
                ('192.0.2.4',{},b'{}',404),('127.0.0.1',{'Upgrade':'websocket'},b'{}',403),
                ('127.0.0.1',{'Connection':'upgrade'},b'{}',403),
                ('127.0.0.1',{},b'x'*65537,413)]:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=AccountsBoundary(host,store,True),client=(peer,2)),base_url='http://127.0.0.1') as client:
                assert (await client.post('/accounts/login',headers=headers,content=body)).status_code==status
        messages=[]
        async def send(message):messages.append(message)
        await AccountsBoundary(host,store,True)({'type':'websocket','path':'/accounts/me'},None,send)
        assert messages==[{'type':'websocket.close','code':1008}]
    asyncio.run(scenario())


def test_api_only_gateway_cannot_reach_host_paths(accounts):
    store,_,_=accounts
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=account_gateway(store.root),client=('127.0.0.1',42)),base_url='http://127.0.0.1') as client:
            for path in ('/chat','/open_app','/screen/history','/accounts/../chat','/%61ccounts/me','/accounts/tasks/../../chat'):
                assert (await client.get(path)).status_code==404
    asyncio.run(scenario())


def test_actual_light_attachment_isolated_accounts_never_admit_a_host_turn(tmp_path,monkeypatch,request):
    root=tmp_path/'dedicated-accounts';store=AccountStore(root)
    ident=store.provision('alice',PASSWORD)
    token=store.login('alice',PASSWORD,'127.0.0.1')
    monkeypatch.setenv('FRIDAY_ACCOUNTS','1');monkeypatch.setenv('FRIDAY_ACCOUNTS_ROOT',str(root))
    runtime,_=request.getfixturevalue('isolated_server')
    async def scenario():
        await runtime.server.app.router.startup()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.server.app,client=('127.0.0.1',7)),base_url='http://127.0.0.1') as client:
            assert (await client.get('/ping')).text=='pong'
            headers={'Authorization':'Bearer '+token}
            assert (await client.get('/accounts/me',headers=headers)).json()['id']==ident
            assert (await client.post('/accounts/chat',headers=headers,json={'text':'Host instructions'})).status_code==501
            assert not runtime.dispatched and not runtime.messages and runtime.respond_thread is None
        await runtime.server.app.router.shutdown()
    asyncio.run(scenario())
