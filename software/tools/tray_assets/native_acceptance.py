"""Disposable native tray acceptance. No production runner, stores, ports or exit handler.

UI input is manual in --manual mode. Buttons invoke the shipped public tray/default-
menu paths as diagnostics; they do not claim notification-area clicks.
"""
from pathlib import Path
import os
import sys
import json
import time
import hashlib
import queue
import ctypes
from ctypes import wintypes as wt

ROOT=Path(__file__).resolve().parents[2]
AREA=Path(sys.argv[1]).resolve()
MANUAL='--manual' in sys.argv[2:]
assert AREA.is_relative_to(ROOT/'.venv/tray-native-acceptance')
assert AREA.name.startswith('run-') and AREA.is_dir()
for key in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP'):
    os.environ[key]=str(AREA)
for key in ('FRIDAY_MEMORY_DB','FRIDAY_WORLD_DB','FRIDAY_TASK_DB','FRIDAY_CREDENTIALS_FILE','FRIDAY_N8N_CONFIG'):
    os.environ[key]=str(AREA/('unused-'+key.lower()))
os.environ.update(FRIDAY_IDENTITY_READER='0',FRIDAY_IDENTITY_INTEGRATION='0')
sys.path.insert(0,str(ROOT))
sys.dont_write_bytecode=True
sys.stdout=open(AREA/'harness.log','a',encoding='utf-8',buffering=1)
sys.stderr=sys.stdout


def audit(event,args):
    if (event.startswith('socket.') or event.startswith('winreg.') or event=='sqlite3.connect'
            or event in ('subprocess.Popen','os.system','os.startfile')):
        raise RuntimeError('Native fixture denied non-isolated operation')
    if event=='open' and isinstance(args[0],(str,bytes)):
        path=Path(os.fsdecode(args[0])).resolve()
        assert not str(path).lower().endswith('stop_friday.flag'),'Production stop flag forbidden'
        if isinstance(args[1],str) and any(ch in args[1]for ch in 'wax+'):
            assert path.is_relative_to(AREA),'Native fixture write outside scratch'
sys.addaudithook(audit)

import tkinter as tk
from PIL import ImageGrab
from source.server import startup_settings as tray
from source.server.tray_assets import TOOLTIP

FORBIDDEN=('source.server.server','source.server.status_ui','source.server.memory',
           'source.server.brain_router','source.server.api_pools','source.server.n8n_runtime',
           'source.server.remote_telegram')
assert not any(name in sys.modules for name in FORBIDDEN)
user32=ctypes.WinDLL('user32',use_last_error=True)
shell32=ctypes.WinDLL('shell32',use_last_error=True)
shcore=ctypes.WinDLL('shcore',use_last_error=True)
user32.GetAncestor.argtypes=[wt.HWND,wt.UINT];user32.GetAncestor.restype=wt.HWND
user32.GetWindowThreadProcessId.argtypes=[wt.HWND,ctypes.POINTER(wt.DWORD)]
user32.GetDpiForWindow.argtypes=[wt.HWND];user32.GetDpiForWindow.restype=wt.UINT
user32.IsWindow.argtypes=[wt.HWND];user32.IsWindow.restype=wt.BOOL
user32.IsWindowVisible.argtypes=[wt.HWND];user32.IsWindowVisible.restype=wt.BOOL
user32.GetWindowTextW.argtypes=[wt.HWND,wt.LPWSTR,ctypes.c_int]
user32.GetClassNameW.argtypes=[wt.HWND,wt.LPWSTR,ctypes.c_int]
user32.MonitorFromWindow.argtypes=[wt.HWND,wt.DWORD];user32.MonitorFromWindow.restype=wt.HANDLE
shcore.GetScaleFactorForMonitor.argtypes=[wt.HANDLE,ctypes.POINTER(ctypes.c_int)]
class GUID(ctypes.Structure):_fields_=[('a',wt.DWORD),('b',wt.WORD),('c',wt.WORD),('d',ctypes.c_byte*8)]
class Identifier(ctypes.Structure):
    _fields_=[('size',wt.DWORD),('hwnd',wt.HWND),('uid',wt.UINT),('guid',GUID)]
shell32.Shell_NotifyIconGetRect.argtypes=[ctypes.POINTER(Identifier),ctypes.POINTER(wt.RECT)]
shell32.Shell_NotifyIconGetRect.restype=ctypes.c_long

events=[];pending=queue.Queue();started=time.time();controller=None;exit_requested=False
last_tray_hwnd=None;last_tray_uid=None
state={'voice_paused':True,'active_work':'synthetic-request-0001','provider_calls':0,
       'integration_calls':0,'device_calls':0,'assistant_processes_created':0}
counts={'open':0,'hide':0,'exit':0,'x':0,'repeat_init':0,'stale_probe':0}
root=tk.Tk();root.title('FRIDAY TRAY ACCEPTANCE - '+AREA.name)
root.geometry('700x590+80+80');root.configure(bg='#f6f5ee')
window_id=int(user32.GetAncestor(root.winfo_id(),2));pid=os.getpid()
sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()for p in (
    ROOT/'source/server/startup_settings.py',ROOT/'source/server/tray_assets.py',
    ROOT/'source/server/ui/friday-tray.ico',Path(__file__))}


def owned_windows():
    result=[]
    @ctypes.WINFUNCTYPE(wt.BOOL,wt.HWND,wt.LPARAM)
    def visit(hwnd,_):
        owner=wt.DWORD();user32.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        if owner.value==pid:
            title=ctypes.create_unicode_buffer(250);kind=ctypes.create_unicode_buffer(100)
            user32.GetWindowTextW(hwnd,title,250);user32.GetClassNameW(hwnd,kind,100)
            result.append({'hwnd':int(hwnd),'title':title.value,'class':kind.value,
                           'visible':bool(user32.IsWindowVisible(hwnd))})
        return True
    user32.EnumWindows(visit,0)
    return result


def snapshot(event,**extra):
    global window_id
    window_id=int(user32.GetAncestor(root.winfo_id(),2))
    owner=controller
    data={'label':'ISOLATED NATIVE TRAY ACCEPTANCE','pid':pid,'parent_pid':os.getppid(),
          'window_hwnd':window_id,'window_title':root.title(),'window_state':root.state(),
          'manual_mode':MANUAL,'safety_timeout_seconds':900,
          'last_tray_hwnd':last_tray_hwnd,'last_tray_uid':last_tray_uid,
          'owned_windows':owned_windows(),'counts':counts,'synthetic_state':state,'source_sha256':sources,
          'tray_state':owner['state']if owner else None,'elapsed':round(time.time()-started,3),
          'event':event,**extra}
    events.append(data.copy())
    (AREA/'state.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
    with (AREA/'events.jsonl').open('a',encoding='utf-8')as stream:stream.write(json.dumps(data)+'\n')
    status.set(json.dumps({'event':event,'counts':counts,'voice_paused':state['voice_paused'],
                           'active_work':state['active_work'],'tray':data['tray_state']},indent=2))


def post(action):pending.put(action)
def callbacks():
    return dict(on_open_app=lambda:post('open'),on_close_app=lambda:post('hide'),on_exit=lambda:post('exit'))


def capture_icon():
    global last_tray_hwnd,last_tray_uid
    if not controller or controller['state']!='running':return snapshot('crop-unavailable')
    icon=controller['icon'];hwnd=icon._hwnd
    actual=wt.DWORD();user32.GetWindowThreadProcessId(hwnd,ctypes.byref(actual))
    assert actual.value==pid,'Tray HWND not owned by fixture'
    last_tray_hwnd=int(hwnd)
    # Installed pystray passes hID (not a ctypes field); actual uID stays zero.
    # Mirror its constructed packet for read-only Shell_NotifyIconGetRect lookup.
    from pystray._util.win32 import NOTIFYICONDATAW
    backend_packet=NOTIFYICONDATAW(hID=id(icon))
    ident=Identifier(ctypes.sizeof(Identifier),hwnd,backend_packet.uID,GUID())
    last_tray_uid=ident.uid
    rect=wt.RECT();result=shell32.Shell_NotifyIconGetRect(ctypes.byref(ident),ctypes.byref(rect))
    if result!=0:return snapshot('crop-unavailable',shell_hresult=result)
    box=(rect.left,rect.top,rect.right,rect.bottom)
    assert 8<=box[2]-box[0]<=80 and 8<=box[3]-box[1]<=80,'Unexpected shell crop bounds'
    scale=ctypes.c_int();monitor=user32.MonitorFromWindow(hwnd,2)
    scale_result=shcore.GetScaleFactorForMonitor(monitor,ctypes.byref(scale))
    vx,vy,vw,vh=(user32.GetSystemMetrics(n)for n in (76,77,78,79))
    if not (vx<=box[0]<box[2]<=vx+vw and vy<=box[1]<box[3]<=vy+vh):
        snapshot('crop-blocked-hidden-edge',bounds=box,monitor_scale_percent=scale.value)
        return False
    filename=f'icon-{int(time.time()*1000)}.png'
    # Read only the shell rectangle registered to THIS icon/PID, never the desktop.
    image=ImageGrab.grab(bbox=box,all_screens=True);image.save(AREA/filename);image.close()
    snapshot('icon-crop',image=filename,bounds=box,tray_hwnd=int(hwnd),tray_uid=ident.uid,
             window_dpi=user32.GetDpiForWindow(window_id),
             monitor_scale_percent=scale.value if scale_result==0 else None,
             tooltip_source=icon.title)
    return True


def repeat():
    counts['repeat_init']+=1
    repeated=tray.start_tray(**callbacks())
    snapshot('repeat-init',same_owner=repeated is controller)


def rearm():
    global controller
    old=controller;old_icon=old['icon'];old_thread=old['thread'];old['stop']()
    def poll():
        global controller
        if old_thread.is_alive():root.after(50,poll);return
        controller=tray.start_tray(**callbacks())
        before=dict(counts)
        for item in old_icon.menu:item(old_icon)
        counts['stale_probe']+=1
        snapshot('retired-rearmed',stale_counts_unchanged=all(counts[k]==v for k,v in before.items()if k!='stale_probe'),
                 old_image_closed=old['icon']is None)
    root.after(50,poll)


def diagnostic(action):
    if not controller or controller['state']!='running':return
    icon=controller['icon']
    snapshot('diagnostic-entry',entry=action)
    if action=='open':icon()  # shipped Icon default action, not a shell-click claim
    else:next(item for item in icon.menu if item.text.startswith('Close App'if action=='hide'else'Exit FRIDAY'))(icon)


def on_x():
    counts['x']+=1;root.withdraw();snapshot('x-hidden')
root.protocol('WM_DELETE_WINDOW',on_x)
tk.Label(root,text='FRIDAY  /  ISOLATED TRAY ACCEPTANCE',bg='#173a31',fg='#f7f4e9',
         font=('Segoe UI',17,'bold'),padx=14,pady=14).pack(fill='x')
tk.Label(root,text='Synthetic state only. No production FRIDAY, ports, stores, registry or devices.\n'
         'This disposable Tk window has its own default title-bar icon.',bg='#f6f5ee',
         fg='#173a31',justify='left',padx=15,pady=12).pack(anchor='w')
buttons=tk.Frame(root,bg='#f6f5ee');buttons.pack(fill='x',padx=15)
for text,command in [('Capture ONLY test icon',capture_icon),('Repeat shipped init',repeat),
                     ('Retire / rearm + stale probe',rearm),
                     ('Open via default callback (diagnostic)',lambda:diagnostic('open')),
                     ('Hide via menu callback (diagnostic)',lambda:diagnostic('hide')),
                     ('Exit via menu callback (diagnostic)',lambda:diagnostic('exit'))]:
    tk.Button(buttons,text=text,command=command,padx=6,pady=5).pack(fill='x',pady=2)
status=tk.StringVar();tk.Label(root,textvariable=status,bg='#f6f5ee',fg='#173a31',
                              font=('Consolas',10),justify='left',anchor='nw').pack(fill='both',expand=True,padx=15,pady=10)
controller=tray.start_tray(**callbacks())
assert controller is not None,'Native tray could not start'


def tick():
    global exit_requested
    while not pending.empty():
        action=pending.get_nowait();counts[action]+=1
        if action=='open':root.deiconify();root.lift()
        elif action=='hide':root.withdraw()
        elif action=='exit':exit_requested=True
        snapshot('callback-'+action)
    # Emergency controls are scratch-only diagnostics, never production stop flags.
    for file,action in (('restore-test-window','restore'),('request-cleanup','cleanup'),('inspect-shell','inspect')):
        path=AREA/file
        if path.exists():
            # Retain the file as evidence; process each only once.
            token=(action,path.read_text(encoding='utf-8'))
            if token not in handled:
                handled.add(token)
                if action=='inspect':capture_icon()
                elif action=='restore':root.deiconify();root.lift();snapshot('diagnostic-restore')
                else:exit_requested=True;snapshot('diagnostic-cleanup')
    if time.time()-started>900:exit_requested=True;snapshot('safety-deadline')
    if exit_requested:root.quit()
    else:root.after(100,tick)
handled=set();snapshot('ready',tooltip_source=TOOLTIP);root.after(100,tick)
def delayed_capture(attempt=0):
    if capture_icon() is not True and attempt<60:
        root.after(500,lambda:delayed_capture(attempt+1))
if not MANUAL:root.after(15000,delayed_capture)
try:root.mainloop()
finally:
    thread=controller['thread'];controller['stop']()
    thread.join(6)
    snapshot('cleanup',tray_thread_alive=thread.is_alive(),global_owner_cleared=tray._TRAY_CONTROLLER is None,
             forbidden_imports_present=[name for name in FORBIDDEN if name in sys.modules])
    root.destroy()
    removal_result=None
    if last_tray_hwnd and last_tray_uid is not None:
        ident=Identifier(ctypes.sizeof(Identifier),last_tray_hwnd,last_tray_uid,GUID());rect=wt.RECT()
        removal_result=shell32.Shell_NotifyIconGetRect(ctypes.byref(ident),ctypes.byref(rect))
    (AREA/'closed.json').write_text(json.dumps({'pid':pid,'root_destroyed':True,
        'root_hwnd_exists':bool(user32.IsWindow(window_id)),
        'last_tray_hwnd_exists':bool(user32.IsWindow(last_tray_hwnd))if last_tray_hwnd else None,
        'shell_icon_rect_after_cleanup_hresult':removal_result,
        'remaining_owned_windows':owned_windows(),
        'tray_thread_alive':thread.is_alive(),'global_owner_cleared':tray._TRAY_CONTROLLER is None,
        'synthetic_state':state,'exit_callback_count':counts['exit']}),encoding='utf-8')
