"""Passive syntax, asset and wheel checks; no runtime imports or bytecode writes."""
from pathlib import Path
import ast,base64,csv,hashlib,io,json,re,shutil,subprocess,zipfile
ROOT=Path(__file__).resolve().parents[1]
def check():
 errors=[];py=js=json_count=0;node=shutil.which('node')
 for p in ROOT.rglob('*.py'):
  if '.git' in p.relative_to(ROOT).parts:continue
  try:
   ast.parse(p.read_text(encoding='utf-8-sig'),filename=p.relative_to(ROOT).as_posix(),feature_version=(3,11));py+=1
  except (SyntaxError,UnicodeError) as e:errors.append({'file':p.relative_to(ROOT).as_posix(),'kind':type(e).__name__,'line':getattr(e,'lineno',None)})
 for suffix in ('*.js','*.cjs'):
  for p in ROOT.rglob(suffix):
   if '.git' in p.relative_to(ROOT).parts:continue
   if node:
    result=subprocess.run([node,'--check',str(p)],capture_output=True);js+=1
    if result.returncode:errors.append({'file':p.relative_to(ROOT).as_posix(),'kind':'JavaScript syntax'})
 for p in ROOT.rglob('*.json'):
  if '.git' in p.relative_to(ROOT).parts:continue
  try:json.loads(p.read_text(encoding='utf-8-sig'));json_count+=1
  except Exception:errors.append({'file':p.relative_to(ROOT).as_posix(),'kind':'JSON syntax'})
 wheel=ROOT/'software/tmp-oi/open_interpreter-0.4.3-py3-none-any.whl';bad=[]
 with zipfile.ZipFile(wheel) as z:
  record=next(n for n in z.namelist() if n.endswith('.dist-info/RECORD'))
  for name,encoded,size in csv.reader(io.StringIO(z.read(record).decode())):
   if not encoded:continue
   algorithm,digest=encoded.split('=',1);data=z.read(name)
   actual=base64.urlsafe_b64encode(hashlib.new(algorithm,data).digest()).decode().rstrip('=')
   if actual!=digest or str(len(data))!=size:bad.append(name)
  corrupt=z.testzip();entries=len(z.namelist())
 ui=ROOT/'software/source/server/ui';missing=[];refs=0
 for p in ui.glob('*.html'):
  for reference in re.findall(r'(?:src|href)=[\x22\x27]([^\x22\x27]+)',p.read_text(encoding='utf-8')):
   if re.match(r'(?:https?:|data:|#|javascript:)',reference):continue
   refs+=1
   if not (p.parent/reference.split('?',1)[0]).exists():missing.append({'file':p.name,'reference':reference})
 return {'python311_ast_files':py,'javascript_syntax_files':js,'node_available':bool(node),'json_files':json_count,'syntax_errors':errors,'wheel_entries':entries,'wheel_corrupt_entry':corrupt,'wheel_record_mismatches':bad,'html_local_refs_checked':refs,'missing_html_assets':missing}
if __name__=='__main__':
 report=check();(ROOT/'packaging/source-checks.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(json.dumps(report));raise SystemExit(bool(report['syntax_errors'] or report['wheel_record_mismatches'] or report['wheel_corrupt_entry'] or report['missing_html_assets']))
