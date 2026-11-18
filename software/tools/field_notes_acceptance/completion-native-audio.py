"""Single bounded real output-device check through shipped Light/client paths.

Synthetic quiet PCM, fake providers/STT, isolated stores; never opens microphone.
Callback/drain evidence does not establish natural voice or perceptual quality.
"""
import asyncio
import json
import math
import os
from pathlib import Path
import platform
import struct
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2];os.chdir(ROOT);sys.path.insert(0,str(ROOT))
area=Path(tempfile.mkdtemp(prefix='native-output-',dir=ROOT/'.venv/field-notes-ui-check'))
for name in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP'):os.environ[name]=str(area)
for name,file in {'FRIDAY_COMPANION_SETTINGS':'prefs.json','FRIDAY_MEMORY_DB':'memory.db','FRIDAY_WORLD_DB':'world.db',
    'FRIDAY_LOG_DIR':'logs','FRIDAY_AUDIT_DIR':'audit','FRIDAY_EXHAUSTION_LEDGER':'exhaustion.json',
    'FRIDAY_CREDENTIALS_FILE':'absent-credentials.json','FRIDAY_MCP_CONFIG':'absent-mcp.json'}.items():os.environ[name]=str(area/file)
os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP='True',FRIDAY_IDENTITY_INTEGRATION='0',FRIDAY_IDENTITY_READER='0')
platform.uname()
def guard(event,args):
    if event=='subprocess.Popen':
        if args[0] in (None,'lscpu') and args[1]=='lscpu':raise FileNotFoundError('No Linux probe')
        raise RuntimeError('Native output check forbids subprocesses')
    if event in ('socket.connect','socket.bind') and args[1][0] not in ('127.0.0.1','::1'):
        raise RuntimeError('Native output check forbids external network')
    if event=='socket.getaddrinfo' and args[0] not in ('localhost','127.0.0.1','::1'):
        raise RuntimeError('Native output check forbids external DNS')
sys.addaudithook(guard)
import pytest
from test_request_reply_ownership import isolated_server
from test_audio_session import SocketFixture,client
from source.server.server import _AttemptOutput

patch=pytest.MonkeyPatch()
runtime,_=isolated_server.__wrapped__(patch,area,SimpleNamespace(param=None))
result={'scope':'real PortAudio output callback and actual Light/WebSocket handlers; synthetic quiet PCM; no microphone/provider', 'passed':False}

async def check():
    await runtime.server.app.router.startup()
    original=client.pyaudio.PyAudio
    patch.setattr(client.pyaudio,'PyAudio',lambda:None)
    socket=SocketFixture(runtime)
    patch.setattr(client.pyaudio,'PyAudio',original)
    device=original();socket.device.p=device
    try:
        await socket.start()
        token=runtime.server_state['playback_attempt']=object()
        link=runtime.server_state['audio_link'];link.begin(token)
        pcm=b''.join(struct.pack('<h',int(300*math.sin(2*math.pi*440*i/24000))) for i in range(48000))
        runtime.on_tts_chunk(pcm,token)
        await socket.device.handle_audio_message(await socket.message('audio_pcm'))
        playback=socket.device.playback
        result['device_reported_latency_ms']=round(playback.stream.get_output_latency()*1000,3)
        link.finish_synthesis(token,lambda frame:runtime.output_queue.sync_q.put(_AttemptOutput(token,frame,playback=True)))
        await socket.device.handle_audio_message(await socket.message('audio_end'))
        start=time.monotonic()
        while link.playback.outcome=='buffered' and time.monotonic()-start<10:
            await socket.device.observe_output();await asyncio.sleep(.02)
        result.update(outcome=link.playback.outcome,elapsed_seconds=round(time.monotonic()-start,3),
                      generated_audio_seconds=2.0,metrics=dict(link.playback.metrics))
        result['passed']=link.playback.outcome=='drained' and link.playback.bytes_reported==len(pcm)
    finally:
        if socket.device.playback:socket.device.playback.close()
        await socket.close();device.terminate()
        await runtime.server.app.router.shutdown()
try:
    asyncio.run(check())
except Exception as error:
    result['error_type']=type(error).__name__
finally:
    patch.undo()
Path(__file__).with_name('completion-native-audio.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result));raise SystemExit(0 if result['passed'] else 1)
