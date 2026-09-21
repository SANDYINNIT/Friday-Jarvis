import asyncio
import websockets
import pyaudio
import json
import array
import math
import time

CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RECORDING_RATE = 16000
PLAYBACK_RATE = 24000

SILENCE_THRESHOLD = 800
SILENCE_LIMIT_SEC = 1.5

def can_listen_now(voice_paused, server_busy, response_speaking, current_time, cooldown_start):
    return (
        not voice_paused
        and (not server_busy or response_speaking)
        and current_time - cooldown_start > 1.2
    )


class Device:
    def __init__(self):
        self.server_url = "0.0.0.0:10001"
        self.p = pyaudio.PyAudio()
        self.websocket = None
        self.recording = False
        self.input_stream = None
        self.output_stream = None
        self.play_audio = True
        
        self.server_busy = False
        self.was_busy_on_record = False
        self.voice_paused_by_user = False
        self.cooldown_start_time = 0.0
        self.last_action_time = 0.0
        self.response_speaking = False
        self.hot_window_active = False
        self.debug = False
        self.trace_start = time.monotonic()
        self.trace_recording_start = None
        self.trace_first_audio = False

    def trace(self, event):
        if self.debug:
            elapsed = time.monotonic() - self.trace_start
            print(f"[trace +{elapsed:.3f}s] client {event}", flush=True)

    async def connect_with_retry(self, max_retries=50, retry_delay=2):
        for attempt in range(max_retries):
            try:
                ws_url = "ws://" + str(self.server_url)
                self.websocket = await websockets.connect(
                    ws_url, 
                    open_timeout=120, 
                    ping_interval=None
                )
                return
            except ConnectionRefusedError:
                if attempt % 8 == 0 and attempt != 0:
                    print("Loading...")
                await asyncio.sleep(retry_delay)
        raise Exception("Failed to connect to the server after multiple attempts")

    async def send_audio(self):
        self.input_stream = self.p.open(
            format=FORMAT, 
            channels=CHANNELS, 
            rate=RECORDING_RATE, 
            input=True, 
            frames_per_buffer=CHUNK
        )
        
        silence_chunks = 0
        chunks_per_sec = RECORDING_RATE / CHUNK
        silence_limit_chunks = int(chunks_per_sec * SILENCE_LIMIT_SEC)
        
        print("\n=============================================")
        print(" HANDS-FREE JARVIS VOICE MODE ACTIVE!")
        print("=============================================")
        print("Just start speaking, the system will record and respond automatically...\n")
        
        while True:
            try:
                data = self.input_stream.read(CHUNK, exception_on_overflow=False)
                
                shorts = array.array('h', data)
                rms = 0.0
                if shorts:
                    rms = math.sqrt(sum(s*s for s in shorts) / len(shorts))
                
                current_time = time.time()
                can_listen = can_listen_now(
                    self.voice_paused_by_user,
                    self.server_busy,
                    self.response_speaking,
                    current_time,
                    self.cooldown_start_time,
                )
                
                if rms > SILENCE_THRESHOLD and can_listen:
                    if not self.recording:
                        self.recording = True
                        self.was_busy_on_record = self.server_busy
                        self.trace_first_audio = False
                        self.trace_recording_start = time.monotonic()
                        self.trace("recording_start")
                        print("[Listening...]")
                        await self.websocket.send(json.dumps({
                            "role": "user", 
                            "type": "audio", 
                            "format": "bytes.wav", 
                            "start": True
                        }))
                    silence_chunks = 0
                else:
                    if self.recording:
                        silence_chunks += 1

                if self.recording:
                    await self.websocket.send(data)
                    
                    if silence_chunks > silence_limit_chunks:
                        self.recording = False
                        if self.trace_recording_start is not None:
                            duration = time.monotonic() - self.trace_recording_start
                            self.trace(f"recording_end duration={duration:.3f}s silence_limit={SILENCE_LIMIT_SEC:.1f}s")
                        self.trace_recording_start = None
                        print("[Thinking...]")
                        await self.websocket.send(json.dumps({
                            "role": "user", 
                            "type": "audio", 
                            "format": "bytes.wav", 
                            "end": True
                        }))
                        
                        self.server_busy = True
                        self.last_action_time = time.time()
                
                if self.server_busy and (not self.voice_paused_by_user) and (time.time() - self.last_action_time > 45.0):
                    self.server_busy = False
                    self.cooldown_start_time = time.time()
                        
            except Exception as e:
                print("Error in send_audio: " + str(e))
            
            await asyncio.sleep(0.001)

    async def receive_audio(self):
        self.output_stream = self.p.open(
            format=FORMAT, 
            channels=CHANNELS, 
            rate=PLAYBACK_RATE, 
            output=True, 
            frames_per_buffer=CHUNK
        )
        while True:
            try:
                data = await self.websocket.recv()
                
                if isinstance(data, bytes):
                    self.response_speaking = True
                    if self.play_audio and not self.recording:
                        if not self.trace_first_audio:
                            self.trace_first_audio = True
                            self.trace("first_audio")
                        self.output_stream.write(data)
                        self.last_action_time = time.time()
                        
                elif isinstance(data, str):
                    try:
                        msg = json.loads(data)
                        if isinstance(msg, dict):
                            if msg.get("hot_window") is True:
                                self.hot_window_active = bool(msg.get("active"))
                                self.trace("hot_window_open")
                            elif msg.get("voice_paused") is True:
                                self.voice_paused_by_user = True
                                self.server_busy = True
                                self.hot_window_active = False
                                print("\n[Voice listening suspended. Press ENTER in the server terminal to resume.]")
                            elif msg.get("voice_paused") is False:
                                self.voice_paused_by_user = False
                                self.server_busy = False
                                self.response_speaking = False
                                self.hot_window_active = False
                                self.cooldown_start_time = time.time()
                                print("[Ready!]")
                            elif msg.get("ignored") is True:
                                self.trace("ready ignored")
                                self.server_busy = self.was_busy_on_record
                                self.cooldown_start_time = time.time()
                                if self.server_busy:
                                    print("[Ignored background noise. Task continues...]")
                                else:
                                    print("[Ignored. Standing by...]")
                            elif msg.get("end") is True:
                                self.trace("ready response_end")
                                self.server_busy = False
                                self.response_speaking = False
                                self.hot_window_active = False
                                self.cooldown_start_time = time.time()
                                print("[Ready!]")
                    except json.JSONDecodeError:
                        pass
                        
            except Exception as e:
                await self.connect_with_retry()

    async def main(self):
        await self.connect_with_retry()
        await asyncio.gather(self.send_audio(), self.receive_audio())

    def start(self):
        asyncio.run(self.main())

def run(server_url, debug):
    device = Device()
    device.server_url = server_url
    device.debug = debug
    device.start()
