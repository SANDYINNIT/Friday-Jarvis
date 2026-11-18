"""Distribution launch adapter. Uses shipped native main or limited Linux entry."""
import importlib.metadata
import os
from pathlib import Path
import runpy
import socket
import sys
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT.parent/'packaging'))
from source.server import user_routes as routes
import configure

def prepare(platform, *, require_brain=True):
 configure.select()
 if not routes.path().exists():
  if not sys.stdin.isatty():raise routes.RouteConfigError('First-run setup required. Run the launcher with setup in an interactive terminal.')
  configure.wizard()
 data=routes.Store().read()
 # Do not redirect/reconcile an already enrolled installation behind its back.
 inherited=os.environ.get('FRIDAY_CATALOG_MODE')
 if inherited not in (None,'legacy') or any(os.environ.get(k) for k in ('FRIDAY_IDENTITY_DEPLOYMENTS','FRIDAY_IDENTITY_CATALOG_DB','FRIDAY_IDENTITY_HISTORY_DB')) or any(os.environ.get(k,'0') not in ('','0') for k in ('FRIDAY_IDENTITY_INTEGRATION','FRIDAY_IDENTITY_READER','FRIDAY_TOWN_INSTALL_OPERATOR')):
  raise routes.RouteConfigError('Existing catalog/gate selectors conflict with this local setup. Use the separately reviewed catalog enrollment path; no settings were changed.')
 existing=Path.home()/'.friday/catalog-activation'
 if any((existing/name).exists() for name in ('deployments.json','identity-catalog.db','identity-duty-history.db')):
  raise routes.RouteConfigError('An existing default catalog requires reconciliation review; this launcher will not silently use legacy routing.')
 os.environ['FRIDAY_CATALOG_MODE']='legacy'
 os.environ['PYTHON_DOTENV_DISABLED']='1';os.environ['LITELLM_MODE']='PRODUCTION'
 os.environ['LITELLM_LOCAL_MODEL_COST_MAP']='True';os.environ['SCARF_NO_ANALYTICS']='true'
 if require_brain:os.environ['CUSTOM_TIKTOKEN_CACHE_DIR']=str(__import__('tokenizer_cache').verify())
 # Exact private file: never search another installation for missing keys.
 import linux_main
 linux_main.validate_credentials(configure.credentials_path())
 snapshot=routes.public_snapshot()
 if require_brain and not any(row['capability']=='brain' and row['position'] and row['state'].startswith('eligible') for row in snapshot['routes']):
  raise routes.RouteConfigError('No eligible chosen Brain route. Run configure check; set a chain, enable its role and supply its own credential if required.')
 if platform=='windows':
  data_home=routes.private_home()/'data';configure.ensure_private(data_home)
  locations={'FRIDAY_WORLD_DB':'world.db','FRIDAY_MEMORY_DB':'memory.db','FRIDAY_KNOWLEDGE_DB':'knowledge.db','FRIDAY_LIGHT_ARCHIVE_DB':'conversations.db','FRIDAY_COMPANION_SETTINGS':'companion.json','FRIDAY_PERSONALITY_SETTINGS':'personality.json','FRIDAY_EXHAUSTION_LEDGER':'route-cooldowns.json','FRIDAY_LOG_DIR':'logs','FRIDAY_AUDIT_DIR':'audit','FRIDAY_CONFIRM_STATE_DIR':'consent','FRIDAY_SCRATCH_DIR':'scratch','FRIDAY_STOP_FILE':'intentional-stop.flag','FRIDAY_MCP_CONFIG':'mcp-unconfigured.json'}
  if any(os.environ.get(k) and Path(os.environ[k])!=(data_home/value) for k,value in locations.items()):raise routes.RouteConfigError('A data selector belongs to another profile. Clear it or arrange a reviewed launch; no personal stores were opened.')
  for key,value in locations.items():os.environ[key]=str(data_home/value)
  os.environ['FRIDAY_CLOUD_STT']='0'
  os.environ['FRIDAY_TELEGRAM_BOT_TOKEN']=''
 return snapshot

def main(argv=None):
 argv=list(sys.argv[1:] if argv is None else argv)
 try:
  if not argv:raise routes.RouteConfigError('Expected windows, linux, setup, configure or check.')
  mode=argv.pop(0);configure.select()
  if mode=='n8n':
   import n8n_setup
   return n8n_setup.main(argv)
  if mode=='setup':return configure.main(['wizard',*argv])
  if mode=='configure':return configure.main(argv)
  if mode=='check':return configure.main(['check',*argv])
  if mode not in ('windows','linux'):raise routes.RouteConfigError('Unsupported launch mode.')
  if mode=='windows' and (sys.platform!='win32' or sys.version_info[:2]!=(3,11)):
   raise routes.RouteConfigError('Native Windows entry requires Windows x64 and CPython3.11.')
  if mode=='linux' and sys.platform!='linux':raise routes.RouteConfigError('Terminal entry requires supported Linux.')
  if mode=='windows' and argv!=['--enable-devices']:
   if not routes.path().exists():
    if not sys.stdin.isatty():raise routes.RouteConfigError('First-run setup required. Run -Setup in an interactive terminal; no native app was started.')
    configure.wizard()
    print('No native app was started. Review -Check, then use -EnableDevices only after reading the device/model notice.')
    return 0 if routes.Store().read()['chains']['brain'] else 2
   raise routes.RouteConfigError('Native app initialization can load speech models and use devices. Read README, then explicitly launch with -EnableDevices. No app was started.')
  local_data_only = mode=='linux' and bool(argv) and argv[0] in ('import-text','search')
  prepare(mode,require_brain=not local_data_only)
  if mode=='linux':
   import linux_main
   return linux_main.main(argv or ['chat'])
  # Refuse to attach to or signal some other FRIDAY process on the normal port.
  with socket.socket() as probe:
   if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
   try:probe.bind(('127.0.0.1',10101))
   except OSError:raise routes.RouteConfigError('Local port10101 is owned already; no existing application was raised, stopped or altered.') from None
  sys.argv=[str(ROOT/'main.py'),'--status-ui']
  runpy.run_path(str(ROOT/'main.py'),run_name='__main__');return 0
 except (routes.RouteConfigError,ValueError,OSError):
  error=sys.exc_info()[1]
  print(str(error) if isinstance(error,routes.RouteConfigError) else 'Launch configuration/cache validation failed. Run setup/check or reinstall; no fallback was activated.',file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
