"""Local provider setup: actual routing/store paths, fake providers, no personal state."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'software'))
from source.server import user_routes as routes
import configure

class SetupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='friday-route-fixture-');self.home=Path(self.tmp.name)
  clean={k:v for k,v in os.environ.items() if not (k.startswith(('FRIDAY_','GROQ_','GEMINI_','OPENROUTER_','MODAL_','DEEPGRAM_','OLLAMA_')) or 'API_KEY' in k)}
  clean.update(HOME=str(self.home),USERPROFILE=str(self.home),LOCALAPPDATA=str(self.home/'Local'),XDG_CONFIG_HOME=str(self.home/'config'),XDG_DATA_HOME=str(self.home/'data'),PYTHON_DOTENV_DISABLED='1',FRIDAY_EXHAUSTION_LEDGER=str(self.home/'ledger.json'))
  self.env=patch.dict(os.environ,clean,clear=True);self.env.start();self.data=configure.initialize()
  from source.server import api_pools,brain_router
  self.api=api_pools;self.brain=brain_router
  self.saved=(api_pools._store,api_pools._pools,dict(api_pools._model_bans),brain_router._ELIGIBILITY_CONSULT)
  api_pools._store=api_pools.CredentialStore();api_pools._pools=None;api_pools._model_bans.clear();brain_router._ELIGIBILITY_CONSULT=None
  original_connect=socket.socket.connect
  def guarded(sock,address):
   frame=sys._getframe(1)
   while frame:
    if frame.f_code.co_name in ('socketpair','_fallback_socketpair') and Path(frame.f_code.co_filename).resolve()==Path(socket.__file__).resolve():return original_connect(sock,address)
    frame=frame.f_back
   raise AssertionError('External network forbidden')
  self.network=patch.object(socket.socket,'connect',guarded);self.network.start()
 def tearDown(self):
  self.network.stop();self.api._store,self.api._pools,bans,self.brain._ELIGIBILITY_CONSULT=self.saved;self.api._model_bans.clear();self.api._model_bans.update(bans)
  self.env.stop();self.tmp.cleanup()
 def add(self,provider='ollama',model='qwen3:8b',caps=('brain',),deployment=None,base=None):
  return routes.add(self.data,provider,model,caps,deployment or provider+'-main',base)
 def save(self):self.data=routes.Store().save(self.data,self.data['revision'])
 def keys(self):
  p=configure.credentials_path();d=json.loads(p.read_text());d.update(groq_brain=['TESTKEY_GROQ'],modal_brain=['TESTKEY_MODAL'],modal_vision=['TESTKEY_MODAL_VISION'],gemini_brain=['TESTKEY_GEMINI'],gemini_vision=['TESTKEY_GEMINI_VISION']);configure.save_credentials(d,hashlib.sha256(p.read_bytes()).hexdigest())
  self.api._store=self.api.CredentialStore();self.api._pools=None
 def runtime(self):return SimpleNamespace(llm=SimpleNamespace(),messages=[])
 def vision(self):
  source=(ROOT/'software/source/server/screen_understanding.py').read_text(encoding='utf-8');tree=ast.parse(source)
  names={'_configured_vision','_cloud_vision','_local_vision','_usable_vision_text','_openai_vision_payload'}
  selected=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[])
  self.vision_calls=[]
  @contextlib.contextmanager
  def attempt(*args):
   self.vision_calls.append(args);yield lambda:None
  g={'__package__':'source.server','__name__':'source.server.vision_fixture','time':__import__('time'),'_request_current':__import__('source.server.identity_integration',fromlist=['request_current']).request_current,'api_pools':self.api,'_serving_disabled':lambda *a:False,'_identity_vision_attempt':attempt,'base64':__import__('base64'),'VISION_TIMEOUT_SECONDS':2,'requests':SimpleNamespace(post=None)}
  exec(compile(selected,'actual-screen-vision-functions','exec'),g);return g
 def test_empty_first_run_and_key_alone_assign_nothing(self):
  self.assertEqual(routes.candidates('brain'),[]);self.keys();self.assertEqual(routes.candidates('brain'),[])
  self.assertIsNone(self.brain.BrainRouter().apply_for('hello',self.runtime()))
 def test_exact_config_ids_persist_after_reopen(self):
  id=self.add();self.data['chains']['brain']=[id];self.save();again=routes.Store().read();self.assertEqual(again['bindings'][0]['id'],id);self.assertEqual(again['chains']['brain'],[id])
 def test_add_does_not_assign_any_role(self):
  self.add(caps=('brain','vision'));self.save();self.assertEqual(routes.candidates('brain'),[]);self.assertEqual(routes.candidates('vision'),[])
 def test_brain_and_vision_order_independent(self):
  a=self.add(model='model-a',caps=('brain','vision'));b=self.add(model='model-b',caps=('brain','vision'))
  self.data['chains']={'brain':[b,a],'vision':[a,b]};self.save();self.assertEqual([x[1] for x in self.brain.resolve('')],['model-b','model-a']);self.assertEqual([x[1] for x in routes.candidates('vision')],['model-a','model-b'])
 def test_manual_local_model_and_base_used_exactly(self):
  id=self.add(model='chosen:7b',base='http://127.0.0.1:11435');self.data['chains']['brain']=[id];self.save();runtime=self.runtime();ticket=self.brain.BrainRouter().apply_for('hello',runtime)
  self.assertEqual(ticket[2],'chosen:7b');self.assertEqual(runtime.llm.model,'ollama_chat/chosen:7b');self.assertEqual(runtime.llm.api_base,'http://127.0.0.1:11435')
 def test_fallback_uses_next_chosen_local_then_exhausts(self):
  a=self.add(model='first');b=self.add(model='second');self.data['chains']['brain']=[a,b];self.save();router=self.brain.BrainRouter();runtime=self.runtime();router.new_turn();router.apply_for('hello',runtime)
  self.assertTrue(router.failover_to_next(runtime,'hello','synthetic failure'));self.assertEqual(runtime.llm.model,'ollama_chat/second');self.assertFalse(router.failover_to_next(runtime,'hello','synthetic failure'));self.assertIsNone(router.current)
 def test_no_implicit_local_after_cloud_failure(self):
  id=self.add('groq','chosen-cloud');self.data['chains']['brain']=[id];self.save();self.keys();router=self.brain.BrainRouter();runtime=self.runtime();router.apply_for('hello',runtime);self.assertEqual(runtime.llm.model,'openai/chosen-cloud');self.assertFalse(router.failover_to_next(runtime,'hello','HTTP503'));self.assertIsNone(router.current)
 def test_disabled_brain_preserves_vision(self):
  id=self.add(caps=('brain','vision'));self.data['chains']={'brain':[id],'vision':[id]};self.data['bindings'][0]['disabled_capabilities']=['brain'];self.save();self.assertEqual(routes.candidates('brain'),[]);self.assertEqual(len(routes.candidates('vision')),1)
 def test_global_cloud_off_does_not_force_unchosen_local(self):
  id=self.add('groq','chosen');self.data['chains']['brain']=[id];self.save();self.keys();self.assertIsNone(self.brain.BrainRouter(enabled=False).apply_for('hello',self.runtime()))
 def test_missing_key_reports_credential_absent_not_health(self):
  id=self.add('groq','chosen');self.data['chains']['brain']=[id];self.save();row=routes.public_snapshot()['routes'][0];self.assertEqual(row['state'],'credential absent');self.assertEqual(row['health'],'unknown')
 def test_saved_key_not_a_health_check_or_speech_role(self):
  id=self.add('gemini','chosen');self.data['chains']['brain']=[id];self.save();self.keys();row=routes.public_snapshot()['routes'][0];self.assertEqual(row['health'],'unknown');self.assertEqual(self.api.CredentialStore().pools()['gemini_tts'],[]);self.assertEqual(self.api.CredentialStore().pools()['gemini_stt'],[])
 def test_show_check_passive_and_redacted(self):
  id=self.add('modal','chosen',base='https://private-fixture.invalid/v1');self.data['chains']['brain']=[id];self.save();self.keys();before={p:p.read_bytes() for p in self.home.rglob('*') if p.is_file()};output=json.dumps(routes.public_snapshot());self.assertNotIn('TESTKEY',output);self.assertNotIn('private-fixture.invalid',output);self.assertEqual(before,{p:p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
 def test_stale_revision_preserves_original(self):
  old=copy.deepcopy(self.data);self.add();self.save();before=routes.path().read_bytes()
  with self.assertRaises(routes.RouteConfigError):routes.Store().save(old,old['revision'])
  self.assertEqual(before,routes.path().read_bytes())
 def test_failed_write_preserves_file(self):
  self.add();before=routes.path().read_bytes()
  with patch.object(routes.os,'replace',side_effect=OSError('secret endpoint')):
   with self.assertRaises(routes.RouteConfigError) as error:self.save()
  self.assertNotIn('endpoint',str(error.exception));self.assertEqual(before,routes.path().read_bytes())
 def test_invalid_format_or_unknown_fields_refused(self):
  for mutate in (lambda d:d.update(unknown=True),lambda d:d['chains']['brain'].append('missing')):
   d=copy.deepcopy(self.data);mutate(d)
   with self.assertRaises(routes.RouteConfigError):routes.Store().save(d,d['revision'])
 def test_unsupported_groq_vision_refused(self):
  with self.assertRaises(routes.RouteConfigError):self.add('groq','some-model',('vision',))
 def test_duplicate_binding_refused(self):
  self.add()
  with self.assertRaises(routes.RouteConfigError):self.add()
 def test_different_provider_same_name_keeps_distinct_ids(self):
  a=self.add('groq','same');b=self.add('gemini','same');self.assertNotEqual(a,b);self.save();self.assertEqual(len(routes.Store().read()['bindings']),2)
 def test_multiple_deployments_same_pool_refused_not_merged(self):
  self.add('modal','same',base='https://one.invalid/v1')
  with self.assertRaises(routes.RouteConfigError):self.add('modal','different',deployment='other',base='https://two.invalid/v1')
 def test_unsafe_model_endpoint_markup_refused(self):
  for model in ('<script>','x\nignore','model?secret=key'):
   d=copy.deepcopy(self.data)
   with self.assertRaises(routes.RouteConfigError):routes.add(d,'ollama',model,['brain'],'local')
  with self.assertRaises(routes.RouteConfigError):self.add('modal','model',base='https://user:password@host.invalid/v1')
 def test_missing_or_malformed_selected_file_never_defaults(self):
  routes.path().write_text('{')
  with self.assertRaises(routes.RouteConfigError):self.brain.resolve('hello')
  routes.path().unlink()
  with self.assertRaises(routes.RouteConfigError):self.brain.resolve('hello')
 def test_edit_remove_preserve_other_chain_and_credentials(self):
  a=self.add(model='a');b=self.add(model='b',caps=('brain','vision'));self.data['chains']={'brain':[a,b],'vision':[b]};self.save();before=configure.credentials_path().read_bytes()
  with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(configure.main(['remove',a]),0)
  after=routes.Store().read();self.assertEqual(after['chains'],{'brain':[b],'vision':[b]});self.assertEqual(before,configure.credentials_path().read_bytes())
 def test_key_accepted_only_hidden_and_persisted(self):
  with patch.object(configure.sys.stdin,'isatty',return_value=True),patch.object(configure.getpass,'getpass',return_value='TESTKEY_PRIVATE_SENTINEL'),contextlib.redirect_stdout(io.StringIO()) as out:self.assertEqual(configure.main(['key','groq_brain']),0)
  self.assertNotIn('TESTKEY_PRIVATE_SENTINEL',out.getvalue());self.assertEqual(json.loads(configure.credentials_path().read_text())['groq_brain'],['TESTKEY_PRIVATE_SENTINEL']);self.assertEqual(routes.candidates('brain'),[])
 def test_duplicate_stale_credential_save_refused(self):
  p=configure.credentials_path();data=json.loads(p.read_text());digest=hashlib.sha256(p.read_bytes()).hexdigest();data['groq_brain']=['TESTKEY_ONE'];configure.save_credentials(data,digest)
  with self.assertRaises(routes.RouteConfigError):configure.save_credentials(data,digest)
 def test_optional_connection_test_never_generates_answer(self):
  id=self.add();self.save();calls=[]
  class Reply:
   status_code=200
   def iter_content(self,size):return iter([b'{"models":[{"name":"qwen3:8b"}]}'])
   def close(self):pass
  def transport(url,**kwargs):calls.append((url,kwargs));return Reply()
  result=configure.connection_test(id,'brain',transport);self.assertEqual(len(calls),1);self.assertEqual(result['model'],'listed');self.assertEqual(result['successful_answer'],'not tested');self.assertFalse(calls[0][1]['allow_redirects'])
 def test_connection_test_requires_explicit_opt_in(self):
  id=self.add();self.save()
  with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(configure.main(['test',id,'--capability','brain']),2)
 def test_vision_uses_exact_selected_order_and_keeps_brain_unchanged(self):
  a=self.add(model='vision-a',caps=('brain','vision'));b=self.add(model='vision-b',caps=('brain','vision'));self.data['chains']={'brain':[a],'vision':[b,a]};self.save();g=self.vision();calls=[]
  def post(url,**kw):
   calls.append(kw['json']['model']);return SimpleNamespace(status_code=503 if len(calls)==1 else 200,json=lambda:{'message':{'content':'Visible description'}})
  g['requests'].post=post;result=g['_cloud_vision'](b'synthetic','describe');self.assertTrue(result[0]);self.assertEqual(calls,['vision-b','vision-a']);self.assertEqual([x[1] for x in self.brain.resolve('')],['vision-a']);self.assertEqual(g['_local_vision']('describe',b'synthetic')[0],False);self.assertEqual(len(calls),2)
 def test_vision_empty_does_not_use_legacy_models(self):
  g=self.vision();g['requests'].post=lambda *a,**k:self.fail('Unchosen Vision provider called');self.assertFalse(g['_cloud_vision'](b'x','question')[0])
 def test_installed_catalog_cannot_be_reinterpreted_by_user_routes(self):
  from source.server.catalog_startup import prepare,CatalogStartupError
  with self.assertRaises(CatalogStartupError):prepare(env={'FRIDAY_USER_ROUTES':str(routes.path()),'FRIDAY_CATALOG_MODE':'installed'},home=self.home)
 def test_distinct_catalog_characters_never_merge_fallback(self):
  from source.server.world.identity_catalog import IdentityCatalog,Route
  cat=IdentityCatalog(self.home/'catalog.db')
  rows=[dict(Route('modal','modal-primary','same-model').public(),kind='reasoning',capabilities=['brain','vision'],enabled=True),dict(Route('groq','groq-main','different-model').public(),kind='reasoning',capabilities=['brain'],enabled=True)]
  cat.reconcile(rows,complete=True)
  from source.server.world.identity_catalog import read_shadow_projection
  view=read_shadow_projection(cat.path);bindings=list(view['bindings'].values());modal={b['character_id'] for b in bindings if b['provider']=='modal'};groq={b['character_id'] for b in bindings if b['provider']=='groq'}
  self.assertEqual(len(modal),1);self.assertTrue(modal.isdisjoint(groq))

 def test_modal_failure_can_use_groq_while_modal_vision_remains(self):
  m=self.add('modal','modal-model',('brain','vision'),base='https://modal-fixture.invalid/v1');g=self.add('groq','groq-model');self.data['chains']={'brain':[m,g],'vision':[m]};self.save();self.keys()
  router=self.brain.BrainRouter();runtime=self.runtime();router.apply_for('hello',runtime);self.assertEqual(router.current_pool,'brain_modal')
  self.assertTrue(router.failover_to_next(runtime,'hello','HTTP503'));self.assertEqual(router.current_pool,'brain_groq')
  vision=self.vision();vision['requests'].post=lambda *a,**k:SimpleNamespace(status_code=200,json=lambda:{'choices':[{'message':{'content':'Visible'}}]})
  self.assertTrue(vision['_cloud_vision'](b'jpeg','question')[0]);self.assertEqual(vision['api_pools'].model_banned('vision_modal','modal-model'),False);self.assertEqual(router.current_pool,'brain_groq')
 def test_actual_linux_turn_fallback_before_text_only(self):
  import asyncio,linux_main
  a=self.add(model='one');b=self.add(model='two');self.data['chains']['brain']=[a,b];self.save();calls=[]
  def completion(**kw):
   calls.append(kw['model'])
   if len(calls)==1:raise RuntimeError('synthetic failure')
   return iter([{'choices':[{'delta':{'content':'Visible'}}]}])
  session=linux_main.Session(self.home/'session',completion);result=asyncio.run(session.ask('Question',lambda _:None));self.assertEqual(result['outcome'],'completed');self.assertEqual(calls,['ollama_chat/one','ollama_chat/two'])
 def test_visible_partial_cannot_replay_on_fallback(self):
  import asyncio,linux_main
  a=self.add(model='one');b=self.add(model='two');self.data['chains']['brain']=[a,b];self.save();calls=[]
  def completion(**kw):
   calls.append(kw['model'])
   def stream():
    yield {'choices':[{'delta':{'content':'Partial'}}]}
    raise RuntimeError('synthetic failure')
   return stream()
  session=linux_main.Session(self.home/'session',completion)
  with self.assertRaises(RuntimeError):asyncio.run(session.ask('Question',lambda _:None))
  self.assertEqual(calls,['ollama_chat/one'])
 def test_legacy_caption_helpers_cannot_call_other_models(self):
  tree=ast.parse((ROOT/'software/source/server/screen_understanding.py').read_text(encoding='utf-8'))
  selected=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'_brain_answer','_brain_answer_modal','_photo_caption_brain'}],type_ignores=[])
  g={'__package__':'source.server','__name__':'fixture'};exec(compile(selected,'actual-caption-functions','exec'),g)
  self.assertEqual(g['_brain_answer']('question','report'),'');self.assertEqual(g['_brain_answer_modal']('prompt'),'');self.assertEqual(g['_photo_caption_brain']('question','report'),'')
 def test_bundled_legacy_vision_tools_refuse_before_capture_or_api(self):
  tree=ast.parse((ROOT/'software/vendor/open-interpreter/interpreter/core/computer/display/friday_locate.py').read_text(encoding='utf-8'))
  selected=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'locate','describe_image'}],type_ignores=[]);g={'os':os};exec(compile(selected,'actual-bundled-functions','exec'),g)
  with self.assertRaises(RuntimeError):g['locate']('button')
  with self.assertRaises(RuntimeError):g['describe_image']('image')
 def test_kernel_pointer_guard_refuses_unchosen_provider(self):
  tree=ast.parse((ROOT/'software/source/server/kernel_display_patch.py').read_text(encoding='utf-8'))
  g={'_os':os};selected=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='locate_with_vision'],type_ignores=[]);exec(compile(selected,'actual-kernel-function','exec'),g)
  with self.assertRaises(RuntimeError):g['locate_with_vision']('button')
 def test_cached_cooldown_is_not_healthy(self):
  id=self.add('groq','chosen');self.data['chains']['brain']=[id];self.save();self.keys();self.api.fail_model('brain_groq','chosen',cooldown=45)
  self.assertEqual(routes.public_snapshot()['routes'][0]['state'],'model cooldown')
 def test_connection_failure_does_not_expose_error_details(self):
  id=self.add();self.save()
  def transport(*a,**k):raise RuntimeError('TESTKEY_PRIVATE https://private.invalid')
  output=json.dumps(configure.connection_test(id,'brain',transport));self.assertNotIn('TESTKEY_PRIVATE',output);self.assertNotIn('private.invalid',output);self.assertIn('unconfirmed',output)
 def test_linux_entry_refuses_native_without_starting(self):
  import public_entry
  with patch.object(public_entry.sys,'platform','linux'),contextlib.redirect_stderr(io.StringIO()):self.assertEqual(public_entry.main(['windows']),2)

 def test_first_run_wizard_requires_explicit_primary_and_keeps_vision_unassigned(self):
  replies=iter(['ollama','qwen3:8b','brain,vision','ollama-local','y','n'])
  with patch('builtins.input',side_effect=lambda _:next(replies)),contextlib.redirect_stdout(io.StringIO()):configure.wizard()
  data=routes.Store().read();self.assertEqual(len(data['chains']['brain']),1);self.assertEqual(data['chains']['vision'],[]);self.assertEqual(len(data['bindings']),1)
 def test_edit_preserves_ids_and_other_values(self):
  id=self.add(model='old');self.data['chains']['brain']=[id];self.save()
  with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(configure.main(['edit',id,'--model','new']),0)
  after=routes.Store().read();self.assertEqual(after['bindings'][0]['id'],id);self.assertEqual(after['bindings'][0]['model'],'new');self.assertEqual(after['chains']['brain'],[id])
 def test_reopen_never_autocreates_catalog_or_world(self):
  id=self.add();self.data['chains']['brain']=[id];self.save();routes.Store().read();routes.public_snapshot()
  self.assertEqual(list(self.home.rglob('*.db')),[])

 def test_noninteractive_key_never_echoes_or_saves(self):
  before=configure.credentials_path().read_bytes()
  with patch.object(configure.sys.stdin,'isatty',return_value=False),patch.object(configure.getpass,'getpass') as prompt,contextlib.redirect_stderr(io.StringIO()):self.assertEqual(configure.main(['key','groq_brain']),2)
  prompt.assert_not_called();self.assertEqual(before,configure.credentials_path().read_bytes())
 def test_invalid_key_arguments_never_echo_literal(self):
  with contextlib.redirect_stderr(io.StringIO()) as out:
   with self.assertRaises(SystemExit):configure.main(['key','groq_brain','TESTKEY_DO_NOT_ECHO'])
  self.assertNotIn('TESTKEY_DO_NOT_ECHO',out.getvalue())
 def test_native_entry_skips_dotenv_for_selected_routes(self):
  tree=ast.parse((ROOT/'software/main.py').read_text(encoding='utf-8'))
  block=next(n for n in tree.body if isinstance(n,ast.If) and 'audio_startup_enabled' in ast.unparse(n.test))
  calls=[];module=SimpleNamespace(load_dotenv=lambda:calls.append(1))
  with patch.dict(sys.modules,{'dotenv':module}):exec(compile(ast.Module(body=[block],type_ignores=[]),'actual-startup-dotenv-guard','exec'),{'audio_startup_enabled':lambda:False,'os':os})
  self.assertEqual(calls,[])

 def test_public_startup_settings_never_touch_legacy_registration(self):
  from source.server import startup_settings
  class Forbidden:
   def __getattr__(self,name):raise AssertionError('Registry access forbidden')
  with patch.dict(sys.modules,{'winreg':Forbidden()}):
   self.assertFalse(startup_settings.startup_snapshot()['available']);self.assertFalse(startup_settings.set_startup_checked(True,'0'*64)['accepted']);self.assertFalse(startup_settings.startup_enabled())
 def test_credential_rotation_keeps_configuration_binding_ids(self):
  id=self.add('groq','model');self.data['chains']['brain']=[id];self.save();before=routes.Store().read()
  with patch.object(configure.sys.stdin,'isatty',return_value=True),patch.object(configure.getpass,'getpass',return_value='TESTKEY_ROTATED'),contextlib.redirect_stdout(io.StringIO()):configure.main(['key','groq_brain'])
  self.assertEqual(before,routes.Store().read())

 def test_intentional_exit_uses_profile_stop_flag_not_other_installation(self):
  file=self.home/'own-stop.flag';os.environ['FRIDAY_STOP_FILE']=str(file)
  tree=ast.parse((ROOT/'software/source/server/status_ui.py').read_text(encoding='utf-8'));fn=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='tray_on_exit')
  statements=[]
  for node in fn.body:
   if isinstance(node,ast.Import):statements.append(node)
   if isinstance(node,ast.Try):statements.append(node);break
  exec(compile(ast.Module(body=statements,type_ignores=[]),'actual-profile-stop-flag','exec'),{})
  self.assertTrue(file.exists());self.assertFalse((self.home/'.friday/stop_friday.flag').exists())

 def test_key_shaped_model_id_refused_without_echo(self):
  secret='gsk_'+'ABCD'*10
  with self.assertRaises(routes.RouteConfigError) as error:self.add('groq',secret)
  self.assertNotIn(secret,str(error.exception))

 def test_stopped_vision_scope_cannot_dispatch_or_accept_late_result(self):
  from source.server.identity_integration import scoped
  a=self.add(model='one',caps=('vision',));b=self.add(model='two',caps=('vision',));self.data['chains']['vision']=[a,b];self.save();g=self.vision();owner=object();generation=object();state={'turn_ownership':owner,'turn_attempt':generation};calls=[]
  def post(*args,**kw):
   calls.append(1);state['cancelled_owner']=owner
   return SimpleNamespace(status_code=503,json=lambda:{})
  g['requests'].post=post
  with scoped(state,generation):self.assertFalse(g['_cloud_vision'](b'jpeg','question')[0])
  self.assertEqual(calls,[1])
  with scoped(state,generation):self.assertFalse(g['_cloud_vision'](b'jpeg','question')[0])
  self.assertEqual(calls,[1])
 def test_old_generation_vision_does_not_dispatch(self):
  from source.server.identity_integration import scoped
  id=self.add(caps=('vision',));self.data['chains']['vision']=[id];self.save();g=self.vision();owner=object();old=object();state={'turn_ownership':owner,'turn_attempt':object()}
  g['requests'].post=lambda *a,**k:self.fail('Stale Vision generation dispatched')
  with scoped(state,old):self.assertFalse(g['_cloud_vision'](b'jpeg','question')[0])

 def test_unassigned_speech_reports_unavailable_without_failure_or_calls(self):
  tree=ast.parse((ROOT/'software/source/server/gemini_tts.py').read_text(encoding='utf-8'));cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CloudFirstTTSEngine')
  g={'__package__':'source.server'};exec(compile(ast.Module(body=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in {'is_degraded','degraded_status','synthesize'}],type_ignores=[]),'actual-speech-policy','exec'),g)
  fake=SimpleNamespace();self.assertTrue(g['is_degraded'](fake));self.assertFalse(g['synthesize'](fake,'Never spoken'));status=g['degraded_status'](fake);self.assertTrue(status['degraded']);self.assertEqual(status['fail_streak'],0);self.assertIn('not configured',status['reason'])
 def test_late_vision_json_cannot_become_current_success(self):
  from source.server.identity_integration import scoped
  id=self.add(caps=('vision',));self.data['chains']['vision']=[id];self.save();g=self.vision();owner=object();generation=object();state={'turn_ownership':owner,'turn_attempt':generation}
  def payload():state['turn_attempt']=object();return {'message':{'content':'Stale text'}}
  g['requests'].post=lambda *a,**k:SimpleNamespace(status_code=200,json=payload)
  with scoped(state,generation):self.assertFalse(g['_cloud_vision'](b'jpeg','question')[0])
 def test_first_native_invocation_guides_setup_before_devices(self):
  import public_entry
  routes.path().unlink()
  def wizard():
   d=configure.initialize();id=routes.add(d,'ollama','fixture-model',['brain'],'local');d['chains']['brain']=[id];routes.Store().save(d,d['revision'])
  with patch.object(public_entry.sys,'platform','win32'),patch.object(public_entry.sys,'version_info',(3,11,9)),patch.object(public_entry.sys.stdin,'isatty',return_value=True),patch.object(configure,'wizard',side_effect=wizard) as setup,contextlib.redirect_stdout(io.StringIO()) as out:
   self.assertEqual(public_entry.main(['windows']),0)
  setup.assert_called_once();self.assertIn('No native app was started',out.getvalue());self.assertEqual(list(self.home.rglob('*.db')),[])

 def test_linux_local_knowledge_commands_need_no_chosen_model(self):
  import public_entry,linux_main
  self.assertEqual(routes.Store().read()['chains']['brain'],[])
  with patch.object(public_entry.sys,'platform','linux'),patch.object(linux_main,'main',return_value=0) as entry:
   self.assertEqual(public_entry.main(['linux','search','synthetic']),0)
  entry.assert_called_once_with(['search','synthetic']);self.assertEqual(list(self.home.rglob('*.db')),[])

if __name__=='__main__':unittest.main()
