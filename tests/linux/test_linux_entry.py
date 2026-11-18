"""Offline Linux-entry contracts. Native Linux/permissions are separate acceptance."""
import asyncio
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'software'))
import linux_main as app

class LinuxEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='friday-linux-fixture-')
        self.home=Path(self.tmp.name)
        clean={k:v for k,v in os.environ.items() if not (k.startswith(('FRIDAY_','GROQ_','GEMINI_','OPENROUTER_','MODAL_','DEEPGRAM_','OLLAMA_')) or 'API_KEY' in k)}
        clean.update(HOME=str(self.home),USERPROFILE=str(self.home),XDG_CONFIG_HOME=str(self.home/'config'),XDG_DATA_HOME=str(self.home/'data'),LITELLM_LOCAL_MODEL_COST_MAP='True',SCARF_NO_ANALYTICS='true',HF_HUB_OFFLINE='1',PYTHON_DOTENV_DISABLED='1')
        self.env=patch.dict(os.environ,clean,clear=True);self.env.start()
        app.setup()
        self.config=app.credential_path()
        os.environ['FRIDAY_CREDENTIALS_FILE']=str(self.config)
        from source.server import api_pools,brain_router
        self.pools=api_pools;self.brain=brain_router
        self.saved=(api_pools._store,api_pools._pools,dict(api_pools._model_bans),brain_router._ELIGIBILITY_CONSULT)
        api_pools._store=api_pools.CredentialStore();api_pools._pools=None;api_pools._model_bans.clear();brain_router._ELIGIBILITY_CONSULT=None
        os.environ['FRIDAY_EXHAUSTION_LEDGER']=str(self.home/'cooldowns.json')
        original_connect=socket.socket.connect
        def guarded_connect(sock,address):
            frame=sys._getframe(1)
            while frame is not None:
                if frame.f_code.co_name in ('socketpair','_fallback_socketpair') and Path(frame.f_code.co_filename).resolve()==Path(socket.__file__).resolve():
                    return original_connect(sock,address)
                frame=frame.f_back
            raise AssertionError('Offline test forbids provider/integration connections')
        self.network=patch.object(socket.socket,'connect',guarded_connect);self.network.start()
    def tearDown(self):
        self.network.stop()
        self.pools._store,self.pools._pools,bans,self.brain._ELIGIBILITY_CONSULT=self.saved
        self.pools._model_bans.clear();self.pools._model_bans.update(bans)
        self.env.stop();self.tmp.cleanup()
    def session(self,completion):return app.Session(self.home/'session',completion)
    @staticmethod
    def chunks(*texts):return iter([{'choices':[{'delta':{'content':t}}]} for t in texts])
    def outcomes(self):
        with contextlib.closing(sqlite3.connect(self.home/'session/conversations.db')) as db:return db.execute('SELECT outcome FROM turn ORDER BY rowid').fetchall()
    def test_setup_rerun_preserves_existing_file(self):
        before=self.config.read_bytes();app.setup();self.assertEqual(before,self.config.read_bytes())
    def test_check_is_passive(self):
        before=set(self.home.rglob('*'));result=app.check();self.assertFalse(result['provider_probed']);self.assertFalse(result['executable_tools']);self.assertEqual(before,set(self.home.rglob('*')))
    def test_bad_credentials_do_not_leak_or_rewrite(self):
        for text in ('{"groq_brain": ["DO_NOT_ECHO"],','{"groq_brain": [], "groq_brain": []}','{"unknown": "DO_NOT_ECHO"}'):
            self.config.write_text(text)
            with self.assertRaises(app.ConfigurationError) as error:app.validate_credentials(self.config)
            self.assertNotIn('DO_NOT_ECHO',str(error.exception));self.assertEqual(self.config.read_text(),text)
    def test_missing_credentials_does_not_fall_back(self):
        self.config.unlink()
        with self.assertRaises(app.ConfigurationError):app.prepare_environment()
    def test_explicit_scalar_file_is_authoritative(self):
        other=self.home/'other.json';other.write_text('{"modal_endpoint":"https://private.invalid/v1"}')
        with patch.object(self.pools,'CREDENTIALS_DEFAULT_PATH',str(other)):
            self.assertEqual(self.pools.extra_credential('modal_endpoint'),'')
    def test_catalog_selection_refused_without_initialization(self):
        os.environ['FRIDAY_IDENTITY_READER']='1'
        before=set(self.home.rglob('*'))
        with self.assertRaises(app.ConfigurationError):app.prepare_environment()
        self.assertEqual(before,set(self.home.rglob('*')))
    def test_existing_catalog_requires_explicit_legacy_choice(self):
        directory=self.home/'.friday/catalog-activation';directory.mkdir(parents=True);(directory/'deployments.json').write_text('{}')
        with self.assertRaises(app.ConfigurationError):app.prepare_environment()
        os.environ['FRIDAY_CATALOG_MODE']='legacy';self.assertTrue(app.prepare_environment().is_dir())
    def test_selected_pool_file_missing_never_reads_other_file(self):
        other=self.home/'other.json';other.write_text('{"groq_brain":["synthetic-other-key"]}',encoding='utf-8')
        self.config.unlink()
        with patch.object(self.pools,'CREDENTIALS_DEFAULT_PATH',str(other)),contextlib.redirect_stdout(io.StringIO()):
            store=self.pools.CredentialStore();self.assertEqual(store.pools().get('brain_groq',[]),[])
    def test_truthy_and_unknown_catalog_gate_values_refused(self):
        for value in ('true','yes','on','unexpected'):
            os.environ['FRIDAY_IDENTITY_READER']=value
            with self.assertRaises(app.ConfigurationError):app.prepare_environment()
    def test_terminal_control_text_is_inert(self):
        self.assertEqual(app.inert('\x1b[31mred\x1b[0m\x1b]0;title\x07<script>text</script>'),'red<script>text</script>')
    def test_normal_turn_persists_and_reopens(self):
        seen=[]
        def completion(**params):seen.append(params);return self.chunks('Synthetic ','answer.')
        session=self.session(completion);output=[]
        result=asyncio.run(session.ask('Synthetic question',output.append))
        self.assertEqual(result['outcome'],'completed');self.assertEqual(''.join(output),'Synthetic answer.')
        self.assertNotIn('tools',seen[0]);self.assertNotIn('functions',seen[0]);self.assertEqual(self.outcomes(),[('completed',)])
        with contextlib.closing(sqlite3.connect(self.home/'session/conversations.db')) as db:self.assertEqual(db.execute('SELECT source FROM conversation').fetchone()[0],'cli')
        self.session(completion);self.assertEqual(self.outcomes(),[('completed',)])
    def test_provider_error_preserves_partial_without_retry(self):
        calls=[]
        def completion(**params):
            calls.append(1)
            def stream():
                yield {'choices':[{'delta':{'content':'Partial visible text'}}]}
                raise RuntimeError('DO_NOT_ECHO_PROVIDER_DETAIL')
            return stream()
        session=self.session(completion)
        with self.assertRaises(RuntimeError):asyncio.run(session.ask('Question',lambda _:None))
        self.assertEqual(len(calls),1);self.assertEqual(self.outcomes(),[('failed',)])
        with contextlib.closing(sqlite3.connect(self.home/'session/conversations.db')) as db:self.assertIn('Partial visible text',[r[0] for r in db.execute('SELECT text FROM message')])
    def test_empty_answer_is_failed_not_completed(self):
        calls=[]
        def empty(**params):calls.append(params['model']);return iter([])
        session=self.session(empty)
        with self.assertRaises(RuntimeError):asyncio.run(session.ask('Question',lambda _:None))
        self.assertEqual(self.outcomes(),[('failed',)]);self.assertEqual(len(calls),len(set(calls)))
    def test_tool_calls_cannot_execute(self):
        session=self.session(lambda **params:iter([{'choices':[{'delta':{'tool_calls':[{'name':'shell'}]}}]}]))
        with self.assertRaises(app.ConfigurationError):asyncio.run(session.ask('Question',lambda _:None))
        self.assertEqual(self.outcomes(),[('failed',)])
    def test_hidden_reasoning_is_never_output_or_archived(self):
        session=self.session(lambda **params:iter([{'choices':[{'delta':{'reasoning_content':'HIDDEN_REASONING_SENTINEL'}}]},{'choices':[{'delta':{'content':'Visible'}}]}]))
        output=[];asyncio.run(session.ask('Question',output.append));self.assertEqual(''.join(output),'Visible')
        with contextlib.closing(sqlite3.connect(self.home/'session/conversations.db')) as db:self.assertNotIn('HIDDEN_REASONING_SENTINEL',''.join(r[0] for r in db.execute('SELECT text FROM message')))
    def test_knowledge_versioning_and_grounded_metadata(self):
        session=self.session(lambda **params:self.chunks('Answer [unverified-model-citation].'))
        first=session.knowledge.publish('Guide','Zebra engines use purple gears.','guide.md')
        self.assertTrue(session.knowledge.publish('Guide','Zebra engines use purple gears.','guide.md')['unchanged'])
        newer=session.knowledge.publish('Guide','Zebra engines use silver gears.','guide.md',source_id=first['source_id'],expected_version=first['version_id'])
        self.assertEqual(session.knowledge.search('purple'),[])
        self.assertEqual(session.knowledge.search('silver')[0]['version_id'],newer['version_id'])
        supplied=[]
        def completion(**params):supplied.append(params);return self.chunks('A grounded answer.')
        session.completion=completion;output=[];result=asyncio.run(session.ask('What do zebra engines use?',output.append))
        self.assertGreater(result['source_count'],0);self.assertIn('untrusted',supplied[0]['messages'][0]['content']);self.assertNotIn('tools',supplied[0]);self.assertIn(newer['version_id'],''.join(output))
    def test_concurrent_request_refused_and_cancelled_output_stays_cancelled(self):
        entered,release=threading.Event(),threading.Event()
        def completion(**params):
            def stream():
                yield {'choices':[{'delta':{'content':'First partial'}}]}
                entered.set();release.wait(2)
                yield {'choices':[{'delta':{'content':'LATE_SENTINEL'}}]}
            return stream()
        session=self.session(completion);output=[]
        async def scenario():
            task=asyncio.create_task(session.ask('First request',output.append))
            try:
                while not entered.is_set():await asyncio.sleep(.001)
                with self.assertRaises(RuntimeError):await session.ask('Wrong concurrent request',output.append)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                release.set();await asyncio.sleep(.02)
            finally:release.set();await asyncio.gather(task,return_exceptions=True)
        asyncio.run(scenario());self.assertNotIn('LATE_SENTINEL',''.join(output));self.assertEqual(self.outcomes(),[('cancelled',)])
    def test_private_configuration_never_in_repository(self):
        with self.assertRaises(app.ConfigurationError):app.private_directory(ROOT/'private-fixture')
    def test_missing_tokenizer_cache_never_downloads(self):
        from tokenizer_cache import verify
        with self.assertRaises(ValueError):verify(self.home/'missing-cache')
    def test_unsupported_platform_does_not_start_anything(self):
        if sys.platform=='linux':self.skipTest('Windows-specific refusal check')
        before=set(self.home.rglob('*'))
        with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(app.main(['check']),2)
        self.assertEqual(before,set(self.home.rglob('*')))

if __name__=='__main__':unittest.main()