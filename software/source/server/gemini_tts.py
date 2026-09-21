"""Cloud-first TTS: Gemini 3.1 Flash TTS on both Gemini accounts.

Speak chain: gemini account 1 -> gemini account 2 -> local edge-tts voice.
Gemini quotas are per-account AND per-model, so cloud speech uses its own
bucket on the Brain/Vision accounts and never touches their chat/vision
usage. Every output is converted to the same raw format (16-bit mono PCM,
24 kHz) so RealtimeTTS plays one uniform stream no matter which stage won.

Both cloud stages failing degrades silently to the existing local voice —
FRIDAY NEVER goes mute.
"""

import base64
import os
import queue
import threading
import time
from io import BytesIO

import pyaudio
import requests

from . import api_pools

GEMINI_NATIVE_BASE = os.environ.get(
    "FRIDAY_GEMINI_NATIVE_BASE", "https://generativelanguage.googleapis.com/v1beta/models"
)
# Per-model quota buckets and preview-suffix drift: try the preview id first
# (documented as the 3.1 streaming TTS model), then the plain id.
GEMINI_TTS_MODEL_CHAIN = [
    name.strip()
    for name in os.environ.get(
        "FRIDAY_GEMINI_TTS_MODEL_CHAIN", "gemini-3.1-flash-tts-preview,gemini-3.1-flash-tts"
    ).split(",")
    if name.strip()
]
GEMINI_TTS_MODEL = os.environ.get("FRIDAY_GEMINI_TTS_MODEL", GEMINI_TTS_MODEL_CHAIN[0])
GEMINI_TTS_VOICE = os.environ.get("FRIDAY_GEMINI_TTS_VOICE", "Sulafat")  # Gemini prebuilt: Warm, female
GEMINI_TTS_TIMEOUT = float(os.environ.get("FRIDAY_GEMINI_TTS_TIMEOUT", "45"))
LOCAL_TTS_URL = os.environ.get("FRIDAY_TTS_SPEECH_URL", "http://localhost:5050/v1/audio/speech")
LOCAL_TTS_MODEL = os.environ.get("FRIDAY_LOCAL_TTS_MODEL", "tts-1")
# Karen-flavored local fallback voice (calm, warm American female).
LOCAL_TTS_VOICE = os.environ.get("FRIDAY_LOCAL_TTS_VOICE", "en-US-AriaNeural")
SAMPLE_RATE = 24000

_QUOTA_STATUS = (401, 402, 403, 429)
_FORMAT = pyaudio.paInt16
_local_server_started = False

_audio_segment_cls = None


def _local_pcm(text):
    """Local edge-tts voice as final fallback, converted to 24k PCM."""
    global _audio_segment_cls
    global _local_server_started
    try:
        # Lazy local voice: the edge-tts server is only probed/started when
        # the cloud chain has actually failed and local speech is needed.
        from .tts_bootstrap import ensure_edge_tts_server

        if not _local_server_started:
            # Probe/spawn the local edge-tts server once; it stays running
            # for the rest of the session afterwards.
            ensure_edge_tts_server(
                os.environ.get("OPENAI_BASE_URL", "http://localhost:5050/v1")
            )
            _local_server_started = True
        if _audio_segment_cls is None:
            from pydub import AudioSegment

            _audio_segment_cls = AudioSegment
        response = requests.post(
            LOCAL_TTS_URL,
            json={"model": LOCAL_TTS_MODEL, "voice": LOCAL_TTS_VOICE, "input": text},
            timeout=30,
        )
        if response.status_code != 200 or not response.content:
            return False, None, "", f"local tts http {response.status_code}"
        segment = _audio_segment_cls.from_file(BytesIO(response.content), format="mp3")
        pcm = (
            segment.set_frame_rate(SAMPLE_RATE)
            .set_channels(1)
            .set_sample_width(2)
            .raw_data
        )
        return True, pcm, "", ""
    except Exception as error:
        return False, None, "", f"local tts failed: {api_pools.sanitize(error)}"


def _model_hot(model):
    return api_pools.model_banned("gemini_tts", model)


def _synthesize(key, text, model):
    """One Gemini TTS call. Returns (ok, pcm_bytes, error, quota_failure)."""
    url = f"{GEMINI_NATIVE_BASE}/{model}:generateContent"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "You are F.R.I.D.A.Y., a warm, caring, playful female assistant with "
                            "the steady competence of a Stark suit AI — precise when needed, "
                            "gently witty, like a supportive older sister. Read this reply aloud "
                            "naturally at an even, friendly pace; mild warmth, no artificial "
                            "cheerfulness. Do not add or omit words. Synthesize speech only:\n"
                            + text
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_TTS_VOICE}}
            },
        },
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=GEMINI_TTS_TIMEOUT)
    except requests.RequestException as error:
        return False, None, f"gemini tts request error: {api_pools.sanitize(error)}", False
    if response.status_code != 200:
        return False, None, f"gemini tts http {response.status_code}", response.status_code in _QUOTA_STATUS
    try:
        body = response.json()
        parts = ((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        inline = next((part.get("inlineData") for part in parts if part.get("inlineData")), None)
        if not inline or not inline.get("data"):
            return False, None, "gemini tts returned no audio", False
        return True, base64.b64decode(inline["data"]), "", False
    except (ValueError, IndexError, KeyError, TypeError) as error:
        return False, None, f"gemini tts bad payload: {error}", False


class CloudFirstTTSVoice:
    """Speaks text cloud-first: gemini account 1 -> 2 -> local voice.

    The Gemini stage itself walks a per-model id chain (preview suffix
    variants): quotas are per-account AND per-model, a 404 on one id must
    not silence the whole account.
    """

    def speak(self, text):
        """Speak cloud-first. Returns PCM bytes or None; records LAST_STAGE."""
        text = str(text or "").strip()
        if not text:
            return None
        quota_pool = api_pools.pool("gemini_tts")
        if quota_pool is not None and not quota_pool.empty:
            for model in GEMINI_TTS_MODEL_CHAIN:
                if _model_hot(model):
                    continue
                for _ in range(quota_pool.total()):
                    key = quota_pool.next_key()
                    if key is None:
                        break
                    ok, pcm, error, quota_failure = _synthesize(key, text, model)
                    if ok:
                        quota_pool.succeed(key)
                        api_pools.model_succeed("gemini_tts", model)
                        self.LAST_STAGE = "gemini"
                        return pcm
                    print(f"[gemini tts] {api_pools.sanitize(error)}", flush=True)
                    if quota_failure:
                        # 429/401-style: this ACCOUNT is hot — next account.
                        quota_pool.fail(key)
                    else:
                        # 404/400-style: the id is missing everywhere — stop
                        # proving it and let the next MODEL id take over.
                        api_pools.fail_model("gemini_tts", model)
                        break
        ok, pcm, _quota, _error = _local_pcm(text)
        self.LAST_STAGE = "local" if ok else "none"
        return pcm if ok else None


class CloudFirstTTSEngine:
    """RealtimeTTS-compatible engine: Gemini 1 -> 2 -> local voice.

    RealtimeTTS engines expose get_stream_info() + synthesize(text) pushing
    raw audio bytes into self.queue. All chain stages here output the SAME
    raw format (16-bit mono PCM @ 24 kHz), so the audio stream config never
    changes even when the speaking stage swaps.
    """

    LAST_STAGE = None

    def __init__(self):
        self.engine_name = "gemini-cloud-first"
        self.can_consume_generators = False
        self.queue = queue.Queue()
        self.voices = [GEMINI_TTS_VOICE, LOCAL_TTS_VOICE]
        self.voice = GEMINI_TTS_VOICE
        self.voice_bridge = CloudFirstTTSVoice()

    def post_init(self):
        return

    def get_stream_info(self):
        return _FORMAT, _CHANNELS, _RATE

    def synthesize(self, text):
        pcm = self.voice_bridge.speak(text)
        if not pcm:
            # Never let FRIDAY go mute if something unexpected happened.
            ok, pcm, _quota, _error = _local_pcm(text)
            if ok:
                self.LAST_STAGE = "local"
        if not pcm:
            return False
        data = bytes(pcm)
        for index in range(0, len(data), 2048):
            self.queue.put(data[index : index + 2048])
        return True

    def get_voices(self):
        return list(self.voices)

    def set_voice(self, voice=None):
        return

    def set_voice_parameters(self, **parameters):
        return

    def shutdown(self):
        return


_FORMAT = pyaudio.paInt16
_CHANNELS = 1
_RATE = SAMPLE_RATE
