"""Value-free scanner for the checkout, wheel contents and all retained Git blobs."""
from pathlib import Path
import argparse,hashlib,io,json,os,re,subprocess,zipfile
ROOT=Path(__file__).resolve().parents[1]
os.environ['GIT_OPTIONAL_LOCKS']='0'
PATTERNS={
 'provider-key':r'(?<![\w])(?:gsk_[\w-]{30,}|sk-or-v1-[0-9a-f]{50,}|sk-(?:proj-)?[A-Za-z0-9_-]{28,}|AIza[\w-]{30,})',
 'modal-proxy-credential':r'\bwk-[\w-]{12,}\.ws-[\w-]{12,}',
 'github-token':r'\b(?:gh[pousr]_[\w]{30,}|github_pat_[\w]{40,})',
 'telegram-token':r'\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}',
 'url-embedded-auth':r'https?://[^/?#@\s:]+:[^@\s]+@',
 'private-key':r'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----',
 'aws-access-key':r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
 'developer-profile-path':r'(?i)[A-Z]:[/\\]Users[/\\](?!Public\b|Default\b|<|YOUR_|user(?:[/\\]|\b))[^/\\\s"\x27]+',
 'live-project-path':r'(?i)D:[/\\]01(?![\w ])(?:[/\\]|\b)',
 'private-deployment-url':r'https?://[^\s"\x27<>]+\.(?:modal\.run|ngrok(?:-free)?\.(?:app|io))[^\s"\x27<>]*',
}
PRIVATE_FILES={'software/pc_memory.md','software/jarvis_training.md','software/memory/project_notes.md'}

def hits(data,name):
 text=data.decode('utf-8',errors='replace').replace(chr(92)*2,chr(92));result=[]
 for category,regex in PATTERNS.items():
  rows=[]
  for m in re.finditer(regex,text):
   v=m.group()
   if category not in ('developer-profile-path','live-project-path','private-deployment-url','private-key'):
    # Explicit low-entropy examples do not represent live credentials; complex fixtures remain reported.
    if any(s in v.lower() for s in ('synthetic','placeholder','testkey','fakekey')):continue
   rows.append(text.count('\n',0,m.start())+1)
  if rows:result.append({'file':name,'category':category,'lines':sorted(set(rows))[:30],'count':len(rows)})
 return result

def inspect(data,name):
 out=hits(data,name)
 if name.endswith(('.whl', '.zip')) and zipfile.is_zipfile(io.BytesIO(data)):
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   for entry in z.infolist():
    if entry.is_dir():continue
    if Path(entry.filename).is_absolute() or '..' in Path(entry.filename).parts or entry.flag_bits & 1:
     out.append({'file':name+'!'+entry.filename,'category':'unsafe-archive-entry'});continue
    if Path(entry.filename).suffix.lower() in ('.pyc','.pyo','.db','.sqlite','.sqlite3'):
     out.append({'file':name+'!'+entry.filename,'category':'runtime-archive-entry'})
    if entry.file_size>12*1024*1024:out.append({'file':name+'!'+entry.filename,'category':'oversized-archive-entry-not-scanned'});continue
    out.extend(hits(z.read(entry),name+'!'+entry.filename))
 return out

def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args],stderr=subprocess.DEVNULL)

def run():
 work=[];count=0;forbidden=[]
 for p in ROOT.rglob('*'):
  if '.git' in p.relative_to(ROOT).parts:continue
  # Fresh export is separately scanned in full; never mix its copy count with this checkout.
  if p.relative_to(ROOT).parts[0]=='publication-export':continue
  if p.is_symlink():forbidden.append({'file':p.relative_to(ROOT).as_posix(),'category':'symlink-review-required'});continue
  if not p.is_file():continue
  rel=p.relative_to(ROOT).as_posix();count+=1
  if p.suffix.lower() in ('.db','.sqlite','.sqlite3','.log','.wav','.mp3','.etl','.dmp') or p.name=='.env' or p.name.startswith('.env.') or p.name=='api_credentials.json' or any(x in p.parts for x in ('__pycache__','.pytest_cache','node_modules','.venv','.venv-linux')) or rel in PRIVATE_FILES:
   forbidden.append({'file':rel,'category':'excluded-runtime-or-private-file'})
  work.extend(inspect(p.read_bytes(),rel))
 names={r.split(' ',1)[0]:r.split(' ',1)[1] for r in git('rev-list','--objects','--all','--reflog').decode().splitlines() if ' ' in r}
 history=[];blobs=0;metadata_count=0;objects=git('cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype)').decode().splitlines()
 for row in objects:
  oid,kind=row.split()
  if kind in ('commit','tag'):
   metadata_count+=1;history.extend({'object':oid,**h} for h in hits(git('cat-file',kind,oid),'['+kind+' metadata]'));continue
  if kind!='blob':continue
  blobs+=1;name=names.get(oid,'[unreachable-or-unnamed-blob]');data=git('cat-file','blob',oid)
  history.extend({'object':oid,**h} for h in inspect(data,name))
  if name in PRIVATE_FILES:history.append({'object':oid,'file':name,'category':'historical-personal-runtime-document-review-required','bytes':len(data)})
 config=hits((ROOT/'.git/config').read_bytes(),'.git/config')
 return {'values_exported':False,'separately_scanned_exports':['publication-export'] if (ROOT/'publication-export').exists() else [],'worktree_files_scanned':count,'worktree_findings':work,'excluded_files_present':forbidden,'all_retained_git_objects':len(objects),'all_retained_blobs_scanned':blobs,'commit_tag_objects_scanned':metadata_count,'history_findings':history,'git_config_findings':config,'publication_blocked':bool(history or forbidden or config),'limits':'Pattern scan plus explicit runtime-file checks; not proof that arbitrary natural-language private content never existed.'}

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'packaging/public-scan.json');args=parser.parse_args()
 report=run();args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
 print(json.dumps({k:v for k,v in report.items() if k not in ('worktree_findings','history_findings','excluded_files_present','git_config_findings')}))
 for scope in ('worktree_findings','history_findings','excluded_files_present','git_config_findings'):
  print(scope+': '+json.dumps(report[scope]))
