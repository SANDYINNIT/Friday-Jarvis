"""Explicit local-user configuration projection; existing routers remain dispatch authorities."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

class RouteConfigError(ValueError): pass
PROVIDERS={
 'ollama':{'brain':'local','vision':'vision_local','base':'http://127.0.0.1:11434'},
 'modal':{'brain':'brain_modal','vision':'vision_modal'},
 'groq':{'brain':'brain_groq','base':'https://api.groq.com/openai/v1'},
 'openrouter':{'brain':'brain_openrouter','vision':'vision_openrouter','base':'https://openrouter.ai/api/v1'},
 'gemini':{'brain':'brain_gemini','vision':'vision_gemini','base':'https://generativelanguage.googleapis.com/v1beta/openai'},
}
MODEL=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?(?::[A-Za-z0-9][A-Za-z0-9_.-]*)?')
SLUG=re.compile(r'[a-z][a-z0-9_-]{0,63}')
ROOT=Path(__file__).resolve().parents[3]

def private_home():
 if os.name=='nt':return Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))/'FRIDAY'
 return Path(os.environ.get('XDG_CONFIG_HOME',Path.home()/'.config'))/'friday'

def path():return Path(os.environ.get('FRIDAY_USER_ROUTES',private_home()/'routes.json')).expanduser()
def selected():return bool(os.environ.get('FRIDAY_USER_ROUTES'))
def _unique(pairs):
 out={}
 for k,v in pairs:
  if k in out:raise RouteConfigError('Duplicate configuration field.')
  out[k]=v
 return out

def validate(data):
 if not isinstance(data,dict) or set(data)!={'version','revision','catalog_mode','bindings','chains'} or data['version']!=1 or type(data['revision']) is not int or data['revision']<0 or data['catalog_mode']!='legacy':
  raise RouteConfigError('Unsupported routing format/catalog mode; review local setup, never substitute another catalog.')
 bindings=data['bindings'];chains=data['chains']
 if not isinstance(bindings,list) or len(bindings)>64 or not isinstance(chains,dict) or set(chains)!={'brain','vision'}:raise RouteConfigError('Expected bounded Brain/Vision chains and bindings.')
 ids={};identities=set();providers={}
 for b in bindings:
  if not isinstance(b,dict) or set(b)!={'id','provider','deployment','model','base','capabilities','enabled','disabled_capabilities'}:raise RouteConfigError('Unknown or incomplete binding fields.')
  if not isinstance(b['id'],str) or not re.fullmatch(r'[0-9a-f]{32}',b['id']) or b['id'] in ids:raise RouteConfigError('Binding configuration IDs must be unique.')
  provider=b['provider'];spec=PROVIDERS.get(provider)
  if spec is None or not isinstance(b['model'],str) or len(b['model'])>160 or not MODEL.fullmatch(b['model']):raise RouteConfigError('Unsupported provider or exact model ID format.')
  if re.match(r'(?:gsk_[A-Za-z0-9_-]{24,}|sk-or-v1-[A-Za-z0-9_-]{24,}|sk-proj-[A-Za-z0-9_-]{24,}|AIza[A-Za-z0-9_-]{25,}|github_pat_[A-Za-z0-9_]{24,})',b['model']):raise RouteConfigError('A credential-shaped value is not a model ID; enter secrets only in the hidden key prompt.')
  if not isinstance(b['deployment'],str) or not SLUG.fullmatch(b['deployment']):raise RouteConfigError('Use a public deployment label containing lowercase letters, digits, _ or -.')
  caps=b['capabilities'];disabled=b['disabled_capabilities']
  if not isinstance(caps,list) or not caps or len(set(caps))!=len(caps) or any(c not in ('brain','vision') or c not in spec for c in caps) or not isinstance(disabled,list) or len(set(disabled))!=len(disabled) or any(c not in caps for c in disabled) or type(b['enabled']) is not bool:
   raise RouteConfigError('Unsupported capability or enabled state. Groq supports Brain only in this integration.')
  try:
   base=urlsplit(b['base']);port=base.port
   if base.username or base.password or base.query or base.fragment or not base.hostname or b['base'].endswith('/') or any(c.isspace() for c in b['base']):raise ValueError()
   if provider=='ollama':
    if base.scheme!='http' or base.hostname not in ('127.0.0.1','localhost','::1') or base.path not in ('','/api/chat'):raise ValueError()
   elif base.scheme!='https':raise ValueError()
   if provider not in ('modal','ollama') and b['base']!=spec['base']:raise ValueError()
  except (ValueError,TypeError,AttributeError):raise RouteConfigError('Use the supported HTTPS provider base, or a loopback Ollama base; credentials/query strings are forbidden.') from None
  # Current catalog matcher has one endpoint/reference per provider pool. Refuse,
  # rather than conflate, multiple deployments under that same pool.
  pair=(b['deployment'],b['base'])
  if provider in providers and providers[provider]!=pair:raise RouteConfigError('This runtime supports one deployment/base per provider profile. A second deployment needs separate catalog/router support; it cannot be merged by name.')
  providers[provider]=pair
  identity=(provider,b['deployment'],b['model'])
  if identity in identities:raise RouteConfigError('Exact binding already exists; edit its capabilities instead of duplicating it.')
  identities.add(identity);ids[b['id']]=b
 for cap,chain in chains.items():
  if not isinstance(chain,list) or len(chain)>20 or len(set(chain))!=len(chain) or any(i not in ids or cap not in ids[i]['capabilities'] for i in chain):raise RouteConfigError('Chain references a missing, duplicate or unsupported binding.')
 return data

class Store:
 def __init__(self,file=None):self.path=Path(file) if file else path()
 def read(self):
  try:
   if self.path.is_symlink() or self.path.stat().st_size>65536:raise RouteConfigError('Selected configuration is redirected or too large.')
   return validate(json.loads(self.path.read_text(encoding='utf-8'),object_pairs_hook=_unique))
  except RouteConfigError:raise
  except (OSError,ValueError,UnicodeError,TypeError,KeyError):raise RouteConfigError('Selected route configuration is missing or unreadable. Run setup; no legacy provider chain was substituted.') from None
 def save(self,data,expected_revision):
  validate(data);directory=self.path.parent
  if directory.is_symlink():raise RouteConfigError('Private configuration directory cannot be a symlink.')
  directory.mkdir(parents=True,mode=0o700,exist_ok=True)
  lock=directory/'.routes-write.lock';fd=None;temp=None
  try:
   fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
   old=self.read() if self.path.exists() else None
   if (old['revision'] if old else 0)!=expected_revision:raise RouteConfigError('Stale configuration revision; reload before saving.')
   updated=json.loads(json.dumps(data));updated['revision']=expected_revision+1;validate(updated)
   handle,temp=tempfile.mkstemp(prefix='.routes-',dir=directory)
   with os.fdopen(handle,'w',encoding='utf-8') as stream:
    json.dump(updated,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
   os.replace(temp,self.path);temp=None
   return updated
  except FileExistsError:raise RouteConfigError('Another configuration write is active; retry after it completes. A stale lock requires review.') from None
  except OSError:raise RouteConfigError('Configuration write failed; no success confirmed. Previous valid file retained when replacement did not occur.') from None
  finally:
   if temp:
    try:os.unlink(temp)
    except OSError:pass
   if fd is not None:os.close(fd);lock.unlink(missing_ok=True)
 def initialize(self):
  if self.path.exists():return self.read()
  return self.save({'version':1,'revision':0,'catalog_mode':'legacy','bindings':[],'chains':{'brain':[],'vision':[]}},0)

def add(data,provider,model,capabilities,deployment,base=None):
 b={'id':uuid4().hex,'provider':provider,'deployment':deployment,'model':model,'base':(base or PROVIDERS.get(provider,{}).get('base','')).rstrip('/'),'capabilities':list(capabilities),'enabled':True,'disabled_capabilities':[]}
 data['bindings'].append(b);validate(data);return b['id']

def candidates(capability,*,include_disabled=False):
 if not selected():return None
 data=Store().read();byid={b['id']:b for b in data['bindings']};out=[]
 for id in data['chains'][capability]:
  b=byid[id]
  if not include_disabled and (not b['enabled'] or capability in b['disabled_capabilities']):continue
  spec=PROVIDERS[b['provider']];base=b['base']
  if b['provider']=='ollama':base=base.removesuffix('/api/chat')+('/api/chat' if capability=='vision' else '')
  out.append((spec[capability],b['model'],base))
 return out

def public_snapshot():
 data=Store().read();rows=[]
 from . import api_pools
 pools=api_pools.CredentialStore().pools()
 for b in data['bindings']:
  for cap in b['capabilities']:
   pool=PROVIDERS[b['provider']][cap];position=(data['chains'][cap].index(b['id'])+1) if b['id'] in data['chains'][cap] else None
   reason='eligible by saved settings; runtime availability unknown'
   observation,cooldown=api_pools.cached_route_state(pool,b['model'])
   if not b['enabled'] or cap in b['disabled_capabilities']:reason='disabled'
   elif position is None:reason='not assigned to this chain'
   elif b['provider']!='ollama' and not pools.get(pool):reason='credential absent'
   elif cooldown:reason=cooldown.replace('_',' ')
   elif b['provider']!='ollama' and not getattr(api_pools,'cloud_'+cap+'_enabled')():reason='cloud capability disabled for this process'
   rows.append({k:b[k] for k in ('id','provider','deployment','model')}|{'capability':cap,'position':position,'enabled':b['enabled'] and cap not in b['disabled_capabilities'],'state':reason,'health':'unknown','runtime_observation':observation,'cooldown':cooldown,'answer_success':'not tested','catalog_identity':'not enrolled by this configuration'})
 return {'revision':data['revision'],'mode':'saved local configuration','routes':rows,'vision_execution':'Windows native only; Linux can configure it for later use'}
