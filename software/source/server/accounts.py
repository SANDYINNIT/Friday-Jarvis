"""Default-off, loopback-only account boundary. No host interpreter/tool access.

Provisioning is an explicit host operation. Reads never create a registry. This
is not a safe way to expose the existing unrestricted local assistant listener.
"""
import asyncio
from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from threading import RLock
import time
from uuid import uuid4

APP_ID=0x46414343
TTL=8*3600
LIMIT=65536
PASSWORD_N=32768
PERMISSIONS=frozenset({'tasks:read','tasks:write'})
_SCHEMA='''
CREATE TABLE accounts(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,salt BLOB NOT NULL,
 password BLOB NOT NULL,permissions TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),expires REAL NOT NULL);
CREATE TABLE login_attempts(peer TEXT PRIMARY KEY,window REAL NOT NULL,count INTEGER NOT NULL);
'''


def password_hash(password,salt):
    # OWASP's 32MiB scrypt setting; no new crypto dependency or weak fast hash.
    return hashlib.scrypt(password.encode('utf-8'),salt=salt,n=PASSWORD_N,r=8,p=3,
                          maxmem=64*1024*1024,dklen=32)


class AccountStore:
    def __init__(self,root,clock=time.time):
        self.root=Path(root).absolute();self.path=self.root/'accounts.db'
        self.clock=clock;self.lock=RLock()

    @contextmanager
    def connection(self,write=False):
        target=self.path.as_uri()+('?mode=rw' if write else '?mode=ro')
        with closing(sqlite3.connect(target,uri=True,timeout=.5)) as conn,conn:
            conn.row_factory=sqlite3.Row
            if conn.execute('PRAGMA application_id').fetchone()[0]!=APP_ID:
                raise ValueError('Not an account registry')
            if write:conn.execute('BEGIN IMMEDIATE')
            yield conn

    def provision(self,username,password,permissions=('tasks:read',)):
        """Explicit local host API only. Never reachable through HTTP registration."""
        if not isinstance(username,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{2,39}',username):
            raise ValueError('Use a distinct lower-case account name of3–40 characters.')
        if not isinstance(password,str) or not 15<=len(password)<=256:
            raise ValueError('Use a password of15–256 characters.')
        if not isinstance(permissions,(list,tuple,set,frozenset)) or set(permissions)-PERMISSIONS:
            raise ValueError('Unsupported account permission.')
        salt=secrets.token_bytes(16);hashed=password_hash(password,salt)
        self.root.mkdir(parents=True,exist_ok=True)
        with self.lock:
            # Atomic schema creation, rejecting unrelated or host databases.
            with closing(sqlite3.connect(self.path)) as conn,conn:
                marker=conn.execute('PRAGMA application_id').fetchone()[0]
                if marker==0 and not conn.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone():
                    conn.execute('BEGIN IMMEDIATE')
                    for sql in _SCHEMA.split(';'):
                        if sql.strip():conn.execute(sql)
                    conn.execute(f'PRAGMA application_id={APP_ID}')
                elif marker!=APP_ID:raise ValueError('Refusing an unrelated store.')
            with self.connection(True) as conn:
                if conn.execute('SELECT 1 FROM accounts WHERE username=?',(username,)).fetchone():
                    raise ValueError('That account already exists.')
                ident=uuid4().hex
                from .task_store import TaskStore
                tasks=TaskStore(str(self.root/'users'/ident/'tasks.db'))
                if not tasks._ensure():raise OSError('Account task storage unavailable.')
                conn.execute('INSERT INTO accounts VALUES(?,?,?,?,?,1)',
                    (ident,username,salt,hashed,json.dumps(sorted(set(permissions)))))
                return ident

    def login(self,username,password,peer):
        if not isinstance(username,str) or not isinstance(password,str) or not 1<=len(password)<=256 or len(username)>40:
            return None
        with self.connection(True) as conn:
            now=self.clock();row=conn.execute('SELECT window,count FROM login_attempts WHERE peer=?',(peer,)).fetchone()
            count=row['count'] if row and now-row['window']<60 else 0
            if count>=5:return None
            conn.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',
                (peer,row['window'] if count else now,count+1))
            account=conn.execute('SELECT * FROM accounts WHERE username=?',(username,)).fetchone()
        # Unknown users pay the same expensive hash; no account existence response.
        expected=account['password'] if account else b'\0'*32
        actual=password_hash(password,account['salt'] if account else b'\0'*16)
        if not hmac.compare_digest(expected,actual) or not account or not account['active']:return None
        token=secrets.token_urlsafe(32)
        with self.connection(True) as conn:
            # Recheck active state after hashing before issuing the session.
            if not conn.execute('SELECT 1 FROM accounts WHERE id=? AND active=1',(account['id'],)).fetchone():return None
            conn.execute('DELETE FROM sessions WHERE expires<=?',(self.clock(),))
            conn.execute('INSERT INTO sessions VALUES(?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(),account['id'],self.clock()+TTL))
        return token

    def authenticate(self,token):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):return None
        with self.connection() as conn:
            row=conn.execute('''SELECT a.id,a.username,a.permissions FROM sessions s
                JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=? AND s.expires>? AND a.active=1''',
                (hashlib.sha256(token.encode()).hexdigest(),self.clock())).fetchone()
        return dict(row) if row else None

    def logout(self,token):
        with self.connection(True) as conn:
            conn.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

    def task_store(self,account):
        # Account UUID comes only from the authenticated registry, never the URL.
        ident=account['id']
        if not re.fullmatch(r'[a-f0-9]{32}',ident):raise ValueError('Invalid account identity')
        from .task_store import TaskStore
        store=TaskStore(str(self.root/'users'/ident/'tasks.db'))
        if not Path(store.db_path).is_file():raise OSError('Account storage unavailable')
        store._ready=True  # Schema was created only during explicit provisioning.
        return store


class AccountsBoundary:
    """Middleware intercepts its exact namespace; it cannot dispatch host work."""
    def __init__(self,app,store=None,enabled=False):
        self.app,self.store,self.enabled=app,store,enabled
        self.serial=asyncio.Semaphore(1)

    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        if not path.startswith('/accounts'):
            return await self.app(scope,receive,send)
        if scope['type']=='websocket':
            return await send({'type':'websocket.close','code':1008})
        async def reply(code,value):
            raw=json.dumps(value,ensure_ascii=False).encode()
            await send({'type':'http.response.start','status':code,'headers':[
                (b'content-type',b'application/json; charset=utf-8'),(b'cache-control',b'no-store'),
                (b'x-content-type-options',b'nosniff')]})
            await send({'type':'http.response.body','body':raw})
        if not self.enabled:return await reply(404,{'error':'Account access is disabled.'})
        if ((scope.get('client') or ('',))[0] not in ('127.0.0.1','::1')
                or scope.get('raw_path',path.encode())!=path.encode()
                or scope.get('query_string') or path not in ('/accounts/login','/accounts/me','/accounts/logout','/accounts/tasks','/accounts/chat')):
            return await reply(404,{'error':'Account route unavailable.'})
        headers=scope.get('headers',[])
        if any(k.lower() in (b'origin',b'upgrade',b'forwarded',b'x-forwarded-for',b'x-forwarded-host')
               or k.lower()==b'connection' and b'upgrade' in v.lower() for k,v in headers):
            return await reply(403,{'error':'Direct loopback API access required.'})
        method=scope.get('method')
        allowed={'/accounts/login':('POST',),'/accounts/logout':('POST',),'/accounts/me':('GET',),
                 '/accounts/tasks':('GET','POST'),'/accounts/chat':('POST',)}
        if method not in allowed[path]:return await reply(405,{'error':'Method not allowed.'})
        if self.serial.locked():return await reply(429,{'error':'Account request busy; retry deliberately.'})
        async with self.serial:
            try:
                auth=[v for k,v in headers if k.lower()==b'authorization']
                token=None;account=None
                if path!='/accounts/login':
                    if len(auth)!=1 or not auth[0].startswith(b'Bearer '):return await reply(401,{'error':'Authentication required.'})
                    token=auth[0][7:].decode('ascii')
                    account=await asyncio.to_thread(self.store.authenticate,token)
                    if account is None:return await reply(401,{'error':'Authentication required.'})
                payload={}
                if method=='POST':
                    body=bytearray()
                    async with asyncio.timeout(5):
                        while True:
                            frame=await receive()
                            if frame['type']!='http.request':raise ValueError('Disconnected')
                            body.extend(frame.get('body',b''))
                            if len(body)>LIMIT:return await reply(413,{'error':'Request too large.'})
                            if not frame.get('more_body'):break
                    payload=json.loads(body or b'{}')
                    if not isinstance(payload,dict):raise ValueError('Expected object')
                if path=='/accounts/login':
                    if set(payload)!={'username','password'}:raise ValueError('Invalid login fields')
                    token=await asyncio.to_thread(self.store.login,payload['username'],payload['password'],scope['client'][0])
                    return await reply(200,{'token':token,'expires_in':TTL}) if token else await reply(401,{'error':'Authentication failed or temporarily limited.'})
                if path=='/accounts/me':
                    return await reply(200,{'id':account['id'],'username':account['username'],
                        'permissions':json.loads(account['permissions']),'host_owner':False,
                        'assistant_execution':'unavailable','reason':'Host conversation, tools, memories and devices are not delegated to accounts.'})
                if path=='/accounts/logout':
                    if payload:raise ValueError('Unexpected logout fields')
                    await asyncio.to_thread(self.store.logout,token);return await reply(200,{'ok':True})
                if path=='/accounts/chat':
                    return await reply(501,{'error':'Account-isolated assistant execution is not implemented. No host turn was submitted.'})
                permission='tasks:read' if method=='GET' else 'tasks:write'
                if permission not in json.loads(account['permissions']):return await reply(403,{'error':'Account permission denied.'})
                store=self.store.task_store(account)
                if method=='GET':
                    result=await asyncio.to_thread(store.list_tasks,'all',50)
                    if result.get('ok'):
                        return await reply(200,{**result,'limit':50,'may_be_truncated':len(result['tasks'])==50})
                    return await reply(503,{'error':'Account task storage unavailable.'})
                if set(payload)-{'reference','action','values','revision','confirm','operation_id'}:raise ValueError('Unknown mutation field')
                if payload.get('action')=='delete' and payload.get('confirm') is not True:return await reply(400,{'error':'Exact deletion confirmation required.'})
                result=await asyncio.to_thread(store.mutate,payload.get('reference'),payload.get('action'),
                    payload.get('values'),payload.get('revision'),payload.get('operation_id'))
                return await reply(200 if result.get('ok') else 409,result)
            except (ValueError,TypeError,UnicodeError):return await reply(400,{'error':'Invalid account request.'})
            except TimeoutError:return await reply(408,{'error':'Account request timed out.'})
            except Exception:return await reply(503,{'error':'Account storage unavailable. No host action was submitted.'})


def attach_accounts(app,environment=None):
    """No store creation, provisioning, listener changes or outgoing connections."""
    env=os.environ if environment is None else environment
    if env.get('FRIDAY_ACCOUNTS','') not in ('','0','1'):
        raise ValueError('FRIDAY_ACCOUNTS must be unset, 0 or 1.')
    diagnostic=env.get('FRIDAY_AUDIO_STARTUP_CHECK','')
    if diagnostic not in ('','0','1'):
        raise ValueError('FRIDAY_AUDIO_STARTUP_CHECK must be unset, 0 or 1.')
    enabled=env.get('FRIDAY_ACCOUNTS')=='1' and diagnostic!='1'
    root=env.get('FRIDAY_ACCOUNTS_ROOT')
    if enabled and (not root or not Path(root).is_absolute()):
        raise ValueError('Enabled accounts require an explicit absolute dedicated storage root.')
    app.add_middleware(AccountsBoundary,store=AccountStore(root) if enabled else None,enabled=enabled)


def account_gateway(root):
    """Separate API-only ASGI surface: path escapes never reach host handlers."""
    async def missing(scope,receive,send):
        if scope['type']=='lifespan':
            while True:
                event=await receive()
                if event['type']=='lifespan.startup':await send({'type':'lifespan.startup.complete'})
                elif event['type']=='lifespan.shutdown':
                    await send({'type':'lifespan.shutdown.complete'});return
        elif scope['type']=='websocket':await send({'type':'websocket.close','code':1008})
        else:
            await send({'type':'http.response.start','status':404,'headers':[]})
            await send({'type':'http.response.body','body':b''})
    return AccountsBoundary(missing,AccountStore(root),True)
