"""Local first-run/configuration CLI. No model calls; keys accepted only by getpass."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import warnings

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
os.environ['PYTHON_DOTENV_DISABLED']='1'
from source.server import user_routes as routes

class SafeParser(argparse.ArgumentParser):
 def error(self,message):
  self.print_usage(sys.stderr)
  self.exit(2,'Invalid command/options. Keys are accepted only through the hidden interactive prompt.\n')

def hidden(prompt):
 if not sys.stdin.isatty():raise routes.RouteConfigError('Secret entry requires an interactive terminal; never pipe a key or put it in arguments.')
 try:
  with warnings.catch_warnings():
   warnings.simplefilter('error',getpass.GetPassWarning)
   return getpass.getpass(prompt)
 except getpass.GetPassWarning:raise routes.RouteConfigError('This terminal cannot hide input; no key was read or saved.') from None

def credentials_path():return Path(os.environ.get('FRIDAY_CREDENTIALS_FILE',routes.private_home()/'api_credentials.json'))
def select():
 os.environ.setdefault('FRIDAY_USER_ROUTES',str(routes.path()))
 os.environ.setdefault('FRIDAY_CREDENTIALS_FILE',str(credentials_path()))

def ensure_private(directory):
 if directory.resolve().is_relative_to(ROOT.parent) or directory.is_symlink():raise routes.RouteConfigError('User configuration belongs outside the checkout, not in a symlink.')
 directory.mkdir(mode=0o700,parents=True,exist_ok=True)
 if os.name=='posix':
  if directory.stat().st_uid!=os.getuid() or directory.stat().st_mode & 0o077:raise routes.RouteConfigError('Configuration directory must be owned by you and mode0700.')

def save_credentials(data,expected_hash):
 file=credentials_path();ensure_private(file.parent);lock=file.parent/'.credentials-write.lock';fd=None;temporary=None
 try:
  fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
  old=file.read_bytes() if file.exists() else b''
  if hashlib.sha256(old).hexdigest()!=expected_hash:raise routes.RouteConfigError('Credentials changed while editing; reload and retry.')
  handle,temporary=tempfile.mkstemp(prefix='.credentials-',dir=file.parent)
  with os.fdopen(handle,'w',encoding='utf-8') as stream:
   json.dump(data,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
  os.replace(temporary,file);temporary=None
 except (OSError,FileExistsError):raise routes.RouteConfigError('Credential save failed or another writer owns it; no success confirmed.') from None
 finally:
  if temporary:Path(temporary).unlink(missing_ok=True)
  if fd is not None:os.close(fd);lock.unlink(missing_ok=True)

def initialize():
 select();ensure_private(routes.path().parent);ensure_private(credentials_path().parent)
 if not credentials_path().exists():
  data=json.loads((ROOT.parent/'config/api_credentials.example.json').read_text(encoding='utf-8'))
  save_credentials(data,hashlib.sha256(b'').hexdigest())
 return routes.Store().initialize()

def key(pool):
 import linux_main
 linux_main.validate_credentials(credentials_path())
 allowed=json.loads((ROOT.parent/'config/api_credentials.example.json').read_text(encoding='utf-8'))
 if pool not in allowed or pool=='modal_endpoint':raise routes.RouteConfigError('Choose a supported credential pool, not an endpoint.')
 raw=credentials_path().read_bytes();data=json.loads(raw)
 value=hidden('New private key (hidden; empty cancels): ').strip()
 if not value:return False
 if len(value)>8192 or any(c.isspace() for c in value):raise routes.RouteConfigError('Expected one nonempty key without whitespace.')
 data[pool]=[value];save_credentials(data,hashlib.sha256(raw).hexdigest());return True

def connection_test(binding_id,capability,transport=None):
 """One explicit metadata GET, never a completion/generation or retry."""
 data=routes.Store().read();b=next((b for b in data['bindings'] if b['id']==binding_id),None)
 if b is None or capability not in b['capabilities']:raise routes.RouteConfigError('Unknown binding or unsupported capability.')
 from source.server import api_pools
 pool=routes.PROVIDERS[b['provider']][capability];keys=api_pools.CredentialStore().pools().get(pool,[])
 if b['provider']!='ollama' and not keys:return {'binding':binding_id,'reachable':'not tested','reason':'credential absent','successful_answer':'not tested'}
 if transport is None:
  import requests
  transport=requests.get
 base=b['base'].removesuffix('/api/chat')
 url=base+('/api/tags' if b['provider']=='ollama' else '/models')
 try:
  response=transport(url,headers={'Authorization':'Bearer '+keys[0]} if keys else {},timeout=10,stream=True,allow_redirects=False)
  try:
   if response.status_code!=200:return {'binding':binding_id,'reachable':'HTTP receipt','http_status':response.status_code,'successful_answer':'not tested','model':'unknown'}
   body=b''.join(_bounded(response.iter_content(8192)))
   payload=json.loads(body);items=payload.get('models' if b['provider']=='ollama' else 'data',[])
   names={x.get('name') or x.get('id') for x in items if isinstance(x,dict)}
   return {'binding':binding_id,'reachable':'metadata received','model':'listed' if b['model'] in names else 'not listed; capability/access still unverified','successful_answer':'not tested','health':'not established'}
  finally:response.close()
 except Exception:return {'binding':binding_id,'reachable':'unconfirmed','reason':'metadata request failed or unsupported','successful_answer':'not tested'}

def _bounded(chunks):
 total=0
 for chunk in chunks:
  total+=len(chunk)
  if total>262144:raise ValueError('Metadata exceeds limit')
  yield chunk

def wizard():
 data=initialize()
 print('FRIDAY local setup. Cloud requests may cost money. Saving a key never assigns a model or starts a call.')
 print('This setup selects explicit local routing. Town catalog enrollment is separate; no residents or identities are created.')
 if data['bindings']:
  print('Existing setup preserved. Use show, add, chain, edit, disable, remove or key.');return
 provider=input('Provider [ollama/modal/groq/openrouter/gemini], empty cancels: ').strip()
 if not provider:return
 if provider not in routes.PROVIDERS:raise routes.RouteConfigError('Unknown provider.')
 model=input('Exact model ID (no guessed default): ').strip()
 caps=[c.strip().lower() for c in input('Capabilities [brain or brain,vision]: ').split(',')]
 deployment=input('Public deployment label (for example ollama-local): ').strip()
 base=hidden('Private HTTPS Modal base (hidden): ').strip().rstrip('/') if provider=='modal' else None
 id=routes.add(data,provider,model,caps,deployment,base)
 for cap in caps:
  if input('Make this the chosen '+cap+' primary? [y/N]: ').strip().lower()=='y':data['chains'][cap]=[id]
 routes.Store().save(data,data['revision']);print('Binding saved: '+id+' (configuration ID, not a catalog character ID).')
 if provider!='ollama' and input('Save a private API key now? [y/N]: ').strip().lower()=='y':
  for cap in caps:
   pool=routes.PROVIDERS[provider][cap]
   from source.server.api_pools import POOL_SCHEMA
   key(POOL_SCHEMA[pool]['file'])
 print('Setup saved. check is passive; test --yes-connect is optional and only checks model-list metadata.')

def main(argv=None):
 select();parser=SafeParser(description='Private local routing setup; no paid model calls.')
 sub=parser.add_subparsers(dest='command',required=True)
 for command in ('init','wizard','show','check'):sub.add_parser(command)
 k=sub.add_parser('key');k.add_argument('pool')
 a=sub.add_parser('add');a.add_argument('provider');a.add_argument('model');a.add_argument('--roles',required=True);a.add_argument('--deployment',required=True)
 c=sub.add_parser('chain');c.add_argument('capability',choices=['brain','vision']);c.add_argument('ids',nargs='*')
 for command in ('enable','disable','remove','edit'):
  e=sub.add_parser(command);e.add_argument('id')
  if command in ('enable','disable'):e.add_argument('--capability',choices=['brain','vision'])
  if command=='edit':e.add_argument('--model');e.add_argument('--roles');e.add_argument('--deployment');e.add_argument('--prompt-base',action='store_true')
 t=sub.add_parser('test');t.add_argument('id');t.add_argument('--capability',choices=['brain','vision'],required=True);t.add_argument('--yes-connect',action='store_true')
 args=parser.parse_args(argv)
 try:
  if args.command=='wizard':wizard();return 0
  if args.command=='init':initialize();print('Empty explicit setup saved; no model selected.');return 0
  if args.command=='key':print('Key saved privately. Restart your own app/terminal to reload cached credential pools; no model call was made.' if key(args.pool) else 'Cancelled; no changes.');return 0
  if args.command in ('show','check'):
   import linux_main
   linux_main.validate_credentials(credentials_path())
   result=routes.public_snapshot();print(json.dumps(result,indent=2));return 0 if args.command=='show' or any(row['capability']=='brain' and row['position'] and row['state'].startswith('eligible') for row in result['routes']) else 2
  if args.command=='test':
   if not args.yes_connect:raise routes.RouteConfigError('No request sent. Add --yes-connect to explicitly request one metadata GET; no answer is generated.')
   print(json.dumps(connection_test(args.id,args.capability),indent=2));return 0
  data=routes.Store().read();revision=data['revision']
  if args.command=='add':
   base=hidden('Private HTTPS Modal base (hidden): ').strip() if args.provider=='modal' else None
   id=routes.add(data,args.provider,args.model,args.roles.split(','),args.deployment,base);print('Binding draft ID: '+id+'; not selected until assigned to a chain.')
  elif args.command=='chain':data['chains'][args.capability]=args.ids
  else:
   binding=next((b for b in data['bindings'] if b['id']==args.id),None)
   if binding is None:raise routes.RouteConfigError('Unknown exact configuration binding ID.')
   if args.command=='remove':
    data['bindings'].remove(binding)
    for cap in data['chains']:data['chains'][cap]=[id for id in data['chains'][cap] if id!=args.id]
   elif args.command=='edit':
    for field in ('model','deployment'):
     if getattr(args,field) is not None:binding[field]=getattr(args,field)
    if args.roles is not None:binding['capabilities']=args.roles.split(',')
    if args.prompt_base:binding['base']=hidden('New private base (hidden): ').strip().rstrip('/')
   elif args.capability:
    if args.capability not in binding['capabilities']:raise routes.RouteConfigError('Binding does not support that capability.')
    disabled=set(binding['disabled_capabilities']);disabled.add(args.capability) if args.command=='disable' else disabled.discard(args.capability);binding['disabled_capabilities']=sorted(disabled)
   else:binding['enabled']=args.command=='enable'
  saved=routes.Store().save(data,revision);print('Saved revision '+str(saved['revision'])+'. Applies at the next selection; no work was dispatched.');return 0
 except (routes.RouteConfigError,OSError,ValueError,TypeError,KeyError):
  error=sys.exc_info()[1]
  print(str(error) if isinstance(error,routes.RouteConfigError) else 'Configuration operation failed; prior saved values retained. Check format, permissions and revision.',file=sys.stderr);return 2
 except (KeyboardInterrupt,EOFError):print('Cancelled; no pending draft was saved.',file=sys.stderr);return 130

if __name__=='__main__':raise SystemExit(main())
