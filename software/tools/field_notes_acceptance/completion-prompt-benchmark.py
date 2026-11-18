"""One owned isolated Interpreter kernel; no provider or device request."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));os.chdir(ROOT)
scratch=Path(tempfile.mkdtemp(prefix='prompt-benchmark-',dir=ROOT/'.venv/field-notes-ui-check'))
for key in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP'):
    os.environ[key]=str(scratch)
os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP='True',SCARF_NO_ANALYTICS='true',HF_HUB_OFFLINE='1')
os.environ['FRIDAY_CREDENTIALS_FILE']=str(scratch/'absent-credentials.json')
from interpreter import AsyncInterpreter
from interpreter.core.render_message import render_message
from source.server.prompt_batch import render

runtime=AsyncInterpreter();runtime.verbose=False;runtime.debug=False
template='\n'.join('section%d {{print(%r)}}' % (i,'value-%d'%i) for i in range(11))
result={'scope':'actual isolated installed Interpreter kernel; eleven harmless constant print blocks', 'runs':[]}
try:
    runtime.computer.run('python','print("warm")',display=False)
    for label,renderer in [('sequential',render_message),('batch',render),('batch',render),('sequential',render_message)]:
        start=time.monotonic();value=renderer(runtime,template);elapsed=time.monotonic()-start
        result['runs'].append({'kind':label,'seconds':round(elapsed,4),'text':value})
    result['same_output']=len({r['text'] for r in result['runs']})==1
    for row in result['runs']:row['output_preview']=repr(row.pop('text')[:180])
finally:
    runtime.computer.terminate()
Path(__file__).with_name('completion-prompt-benchmark.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
raise SystemExit(0 if result['same_output'] else 1)
