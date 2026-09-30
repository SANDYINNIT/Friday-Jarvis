"""Cloud-first TTS: Gemini cloud voice, local neural voice as the fallback.

Speak chain: gemini account 1 -> gemini account 2 -> local edge-tts voice
(en-IE-EmilyNeural @ 0.9x). Every output is converted to the same raw format
(16-bit mono PCM, 24 kHz) so RealtimeTTS plays one uniform stream no matter
which stage won.

ORDER CHANGED 2026-09-30 (Sir's decision). This was local-first, and the local
stage called ensure_edge_tts_server() which BLOCKS for up to 45s waiting for a
local voice server that is usually not running — while Gemini answers in ~0.27s.
That produced a measured 62-second silence between tts_start and first audio.
Cloud now goes first; the local voice is only used when cloud cannot speak, and
never blocks on a speculative server start. If every stage fails, FRIDAY
degrades to the remaining local path — she never goes mute.
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
# The 3.1 streaming TTS model is only published as the -preview id; the bare
# `gemini-3.1-flash-tts` id is undocumented (caused a 404 storm historically),
# so the default chain keeps just the documented preview id. Overridable.
GEMINI_TTS_MODEL_CHAIN = [
    name.strip()
    for name in os.environ.get(
        "FRIDAY_GEMINI_TTS_MODEL_CHAIN", "gemini-3.1-flash-tts-preview"
    ).split(",")
    if name.strip()
]
GEMINI_TTS_MODEL = os.environ.get("FRIDAY_GEMINI_TTS_MODEL", GEMINI_TTS_MODEL_CHAIN[0])
GEMINI_TTS_VOICE = os.environ.get("FRIDAY_GEMINI_TTS_VOICE", "Sulafat")  # Gemini prebuilt: Warm, female
GEMINI_TTS_TIMEOUT = float(os.environ.get("FRIDAY_GEMINI_TTS_TIMEOUT", "45"))
LOCAL_TTS_URL = os.environ.get("FRIDAY_TTS_SPEECH_URL", "http://localhost:5050/v1/audio/speech")
LOCAL_TTS_MODEL = os.environ.get("FRIDAY_LOCAL_TTS_MODEL", "tts-1")
# en-IE-EmilyNeural: Sir's chosen FRIDAY voice — warm Irish female (a nod to
# the character's Irish voice actress, Kerry Condon; JennyNeural is the
# fallback experiment). Override: FRIDAY_LOCAL_TTS_VOICE.
LOCAL_TTS_VOICE = os.environ.get("FRIDAY_LOCAL_TTS_VOICE", "en-IE-EmilyNeural")
# Neural voices default to a calm 0.9x so FRIDAY reads relaxed and natural
# (the edge-tts server's own default is 1.2x = rushed).
LOCAL_TTS_SPEED = float(os.environ.get("FRIDAY_LOCAL_TTS_SPEED", "0.9"))
SAMPLE_RATE = 24000

_QUOTA_STATUS = (401, 402, 403, 429)
_FORMAT = pyaudio.paInt16
_local_server_started = False

_audio_segment_cls = None


def _local_pcm(text, allow_start: bool = True):
    """Local edge-tts voice, converted to 24k PCM.

    `allow_start=False` forbids the blocking server spawn. That spawn waits up
    to 45s for a server that is usually not running, which is what produced a
    62-second silence before the cloud voice ever got a turn. With it disabled
    we only USE the local voice when it is already up, and fail fast otherwise.
    """
    global _audio_segment_cls
    global _local_server_started
    try:
        # Lazy local voice: the edge-tts server is only probed/started when
        # the cloud chain has actually failed and local speech is needed.
        from .tts_bootstrap import ensure_edge_tts_server, probe

        if not _local_server_started:
            if allow_start:
                # Real attempt: probe/spawn once, then remember that we tried.
                # It stays running for the rest of the session afterwards.
                ensure_edge_tts_server(
                    os.environ.get("OPENAI_BASE_URL", "http://localhost:5050/v1")
                )
                _local_server_started = True
            elif not probe(os.environ.get("OPENAI_BASE_URL", "http://localhost:5050/v1")):
                # Never started and not already up: do not pay the startup wait,
                # and do NOT latch _local_server_started - the server may come up
                # later, and a latched flag would disable the local voice for the
                # rest of the session even after it is available.
                return False, None, "", "local tts server is not running (skipped, not starting it)"
        if _audio_segment_cls is None:
            from pydub import AudioSegment

            _audio_segment_cls = AudioSegment
        response = requests.post(
            LOCAL_TTS_URL,
            json={
                "model": LOCAL_TTS_MODEL,
                "voice": LOCAL_TTS_VOICE,
                "input": text,
                "speed": LOCAL_TTS_SPEED,
            },
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


# Which stage spoke last. The HUD reads this MODULE attribute
# (`status_ui.get_ai_agents` does `getattr(_gt, "LAST_STAGE", None)`), so it
# must be written at module scope. FIX (2026-09-29): stages were previously only
# recorded on the engine/voice INSTANCE, so the HUD's "in-use" TTS badge could
# never light even when Gemini was actually speaking.
LAST_STAGE = None


def _record_stage(stage, instance=None):
    """Publish the speaking stage to both the module and the instance."""
    global LAST_STAGE
    LAST_STAGE = stage
    if instance is not None:
        instance.LAST_STAGE = stage
    return stage


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
        return False, None, f"gemini tts request error: {api_pools.sanitize(error)}", False, None
    if response.status_code != 200:
        # FIX (2026-09-30): keep the provider's own guidance. Google answers a
        # TTS quota hit with "Please retry in 29.68s" and names the violated
        # metric (generate_content_free_tier_requests, limit 10 - the free tier
        # TTS cap is tiny). Discarding the body threw that away, so we cooled a
        # fixed 60s regardless of what was asked for.
        detail = ""
        try:
            detail = (response.text or "")[:400]
        except Exception:
            pass
        hint = api_pools._retry_after_seconds(detail)
        message = f"gemini tts http {response.status_code}"
        if detail:
            message = f"{message} ({' '.join(detail.split())[:160]})"
        return (False, None, message, response.status_code in _QUOTA_STATUS,
                hint)
    try:
        body = response.json()
        parts = ((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        inline = next((part.get("inlineData") for part in parts if part.get("inlineData")), None)
        if not inline or not inline.get("data"):
            return False, None, "gemini tts returned no audio", False, None
        return True, base64.b64decode(inline["data"]), "", False, None
    except (ValueError, IndexError, KeyError, TypeError) as error:
        return False, None, f"gemini tts bad payload: {error}", False, None


class CloudFirstTTSVoice:
    """Speaks text cloud-first: gemini 1 -> gemini 2 -> local edge voice.

    The cloud stage walks a per-model id chain (preview suffix variants):
    quotas are per-account AND per-model, so a 404 on one id must not silence
    the whole account. The local voice is the last resort and is never allowed
    to block waiting for a server that is not running.
    """

    def _record_stage(self, stage):
        """Record which TTS stage actually spoke (module + instance)."""
        _record_stage(stage, self)
    def speak(self, text):
        """Speak cloud-first. Returns PCM bytes or None; records LAST_STAGE."""
        text = str(text or "").strip()
        if not text:
            return None
        # CLOUD FIRST (Sir's decision, 2026-09-30). This chain used to try the
        # LOCAL voice first, and _local_pcm() calls ensure_edge_tts_server(),
        # which BLOCKS for up to 45s waiting for a server that often is not
        # running. Measured on this machine: cloud TTS answers in 0.27s, a
        # failed local start burns the full 45s -> a measured 62-second silence
        # between tts_start and first audio. FRIDAY must never make the user
        # wait a minute to hear a reply she could have spoken instantly.
        quota_pool = api_pools.pool("gemini_tts")
        if quota_pool is not None and not quota_pool.empty:
            for model in GEMINI_TTS_MODEL_CHAIN:
                if _model_hot(model):
                    continue
                for _ in range(quota_pool.total()):
                    key = quota_pool.next_key()
                    if key is None:
                        break
                    ok, pcm, error, quota_failure, hint = _synthesize(key, text, model)
                    if ok:
                        quota_pool.succeed(key)
                        api_pools.model_succeed("gemini_tts", model)
                        self._record_stage("gemini")
                        return pcm
                    print(f"[gemini tts] {api_pools.sanitize(error)}", flush=True)
                    if quota_failure:
                        # 429/401-style: this ACCOUNT is hot. FIX (2026-09-30):
                        # this called fail(key) with NO cooldown, which means the
                        # full 6-hour exhaustion window - so one 429 could mute
                        # FRIDAY's voice for six hours (observed: both Gemini TTS
                        # accounts banned for 360 min after a single burst). Use
                        # Google's own "retry in Ns" hint when it supplies one
                        # (it does: the free TTS tier is only 10 requests), else
                        # the shared short cooldown, and let three consecutive
                        # failures escalate on their own.
                        quota_pool.fail(
                            key,
                            cooldown=hint or api_pools.RATE_LIMIT_COOLDOWN_SECONDS,
                        )
                    else:
                        # 404/400-style: the id is missing everywhere - stop
                        # proving it and let the next MODEL id take over.
                        api_pools.fail_model("gemini_tts", model)
                        break

        # Cloud could not speak. Now - and only now - try the local neural
        # voice, WITHOUT the blocking server start: if the local server is not
        # already up we go straight to the offline pytesseract-style path
        # instead of waiting on a speculative spawn.
        ok, pcm, _q, error = _local_pcm(text, allow_start=False)
        if ok:
            self._record_stage("local")
            return pcm
        print(f"[local tts] {error}", flush=True)
        self._record_stage("none")
        return None


class CloudFirstTTSEngine:
    """RealtimeTTS-compatible engine: Gemini cloud voice -> local edge fallback.

    RealtimeTTS engines expose get_stream_info() + synthesize(text) pushing
    raw audio bytes into self.queue. All chain stages here output the SAME
    raw format (16-bit mono PCM @ 24 kHz), so the audio stream config never
    changes even when the speaking stage swaps.
    """

    LAST_STAGE = None

    def _record_stage(self, stage):
        _record_stage(stage, self)

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
            # Last resort before going mute: the offline voice. Here a bounded
            # attempt to START the local server is justified (every other
            # option has already failed), but it is capped so a dead spawn
            # cannot hang the audio pipeline indefinitely.
            ok, pcm, _quota, _error = _local_pcm(text, allow_start=True)
            if ok:
                self._record_stage("local")
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
