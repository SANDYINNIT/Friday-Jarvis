"""Bounded passive shell-inspection requests; never generates mouse/keyboard input."""
from pathlib import Path
import json
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
AREA=Path(sys.argv[1]).resolve()
assert AREA.is_relative_to(ROOT/'.venv/tray-native-acceptance')
sys.dont_write_bytecode=True
from PIL import Image

def guard(event,args):
    if event.startswith(('socket.','winreg.'))or event in ('subprocess.Popen','os.system','os.startfile','sqlite3.connect'):
        raise RuntimeError('Passive observer denied external operation')
sys.addaudithook(guard)
start=time.monotonic();seen=set();observations=[];candidate=None
initial=json.loads((AREA/'state.json').read_text(encoding='utf-8'))
for sequence in range(240):
    if (AREA/'closed.json').exists():break
    (AREA/'inspect-shell').write_text(f'manual-reveal-watch-{start}-{sequence}',encoding='ascii')
    time.sleep(.4)
    try:data=json.loads((AREA/'state.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):continue
    assert data['pid']==initial['pid'] and data['manual_mode'] is True
    name=data.get('image')
    if data['event']=='icon-crop' and name and name not in seen:
        path=(AREA/name).resolve();assert path.is_relative_to(AREA)
        seen.add(name)
        with Image.open(path)as image:
            pixels=list(image.convert('RGB').getdata())
        cream=sum(r>210 and g>210 and b>190 for r,g,b in pixels)
        lime=sum(g>r+10 and g>b+45 and r>140 for r,g,b in pixels)
        observation={'image':name,'bounds':data['bounds'],'pid':data['pid'],
            'tray_hwnd':data['tray_hwnd'],'tray_uid':data['tray_uid'],
            'cream_pixels':cream,'lime_pixels':lime,'scale':data.get('monitor_scale_percent')}
        observations.append(observation)
        # Candidate only: an agent must inspect it before identifying the icon.
        if cream>=20 and lime>=1:candidate=observation;break
    if time.monotonic()-start>=90:break
result={'pid':initial['pid'],'candidate':candidate,'observations':observations,
        'duration_seconds':round(time.monotonic()-start,2),
        'closed':(AREA/'closed.json').exists(),'input_automation':False}
(AREA/'manual-reveal-watch.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
