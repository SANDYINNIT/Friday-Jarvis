"""Two positively owned workers race one synthetic preference revision."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from source.server.companion import Companion

if len(sys.argv)>1:
    area=Path(sys.argv[1]).resolve()
    assert area.is_relative_to(ROOT/'.venv/field-notes-ui-check')
    instance=Companion(area/'preferences.json');revision=instance.preferences()['revision']
    (area/f'ready-{os.getpid()}').write_text('ready')
    deadline=time.monotonic()+10
    while not (area/'go').exists():
        if time.monotonic()>deadline:raise SystemExit('Race start timed out')
        time.sleep(.02)
    print(json.dumps(instance.save({'private_banter':sys.argv[2]},revision)),flush=True)
else:
    area=Path(tempfile.mkdtemp(prefix='preference-race-',dir=ROOT/'.venv/field-notes-ui-check'))
    children=[]
    try:
        for choice in ('light','spicy'):
            children.append(subprocess.Popen([sys.executable,'-I','-B',__file__,str(area),choice],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=ROOT))
        deadline=time.monotonic()+10
        while len(list(area.glob('ready-*')))<2:
            if time.monotonic()>deadline:raise RuntimeError('Workers did not reach the barrier')
            time.sleep(.02)
        (area/'go').write_text('start')
        results=[]
        for child in children:
            out,err=child.communicate(timeout=15)
            assert child.returncode==0,err
            results.append(json.loads(out))
        assert sum(r['ok'] for r in results)==1
        assert sum(bool(r.get('conflict')) for r in results)==1
        record={'passed':True,'results':results,'final':Companion(area/'preferences.json').preferences(),'owned_pids':[c.pid for c in children],'all_exited':all(c.poll() is not None for c in children)}
        (ROOT/'tools/field_notes_acceptance/completion-preference-race.json').write_text(json.dumps(record,indent=2))
        print('Two-process preference race passed; one save, one stale refusal; owned workers exited.')
    finally:
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=5)
