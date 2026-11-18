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
import tempfile
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
# ROUND ZB-LAT (2026-11-03): 127.0.0.1, never `localhost`. This machine's
# ::1 blackholes SYNs, so a `localhost` URL burns `requests`' whole connect
# timeout before IPv4 fallback - measured `http://localhost:5050/v1/models`
# **2.047s** vs `http://127.0.0.1:5050/v1/models` **0.000s**. This is the
# LOCAL edge fallback, i.e. the path taken when Gemini is degraded, so the
# `localhost` default was adding 2 seconds to every fallback utterance on top
# of the synthesis itself.
LOCAL_TTS_URL = os.environ.get("FRIDAY_TTS_SPEECH_URL", "http://127.0.0.1:5050/v1/audio/speech")
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
# Statuses that mean "this model id is wrong", which is worth a long ban.
# Everything else that goes wrong (a network blip, an empty body, a malformed
# payload) is transient and must NOT burn six hours of cloud TTS.
_PERMANENT_MODEL_STATUS = (400, 404, 405, 410)


def _is_transient_tts_failure(message):
    """True when a non-quota TTS failure is worth retrying soon rather than banning.

    2026-10-02 audit. An HTTP 200 that simply carried no audio was being sent to
    `api_pools.fail_model()` with no cooldown, and that function defaults to
    `_EXHAUSTION_COOLDOWN` = 21600s. Measured live: one empty response produced
    "model gemini_tts/gemini-3.1-flash-tts-preview cooling down 21600s" - SIX
    HOURS of silence for a transient empty body.

    Google's own guidance for this family of errors (429 RESOURCE_EXHAUSTED)
    is "wait and retry after a short period", and a 200 with no audio says
    nothing at all about whether the model id is valid. So the rule is simple
    and conservative: a message naming an explicit HTTP status is a real
    provider verdict and keeps the long ban; anything else is transient and gets
    the same short cooldown a 429 already gets.
    """
    text = str(message or "")
    return ("gemini tts http " not in text)
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
    try:
        # Lazy local voice: the edge-tts server is only probed/started when
        # the cloud chain has actually failed and local speech is needed.
        from .tts_bootstrap import ensure_edge_tts_server, probe

        # 127.0.0.1, not `localhost` - see LOCAL_TTS_URL above: the `localhost`
        # default cost a measured 2s connect timeout on this machine.
        base = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:5050/v1")
        if allow_start:
            # Returns False both when the server is DOWN and when it is known
            # DEGRADED (cooldown after a 500). Either way a POST would be
            # wasted: it is a round trip already known to fail, and on the
            # fallback path that is ~1.3s charged to every sentence. Measured
            # live: 3.08s per utterance with the POST, 0.25s without.
            if not ensure_edge_tts_server(base):
                from .tts_bootstrap import last_failure

                return False, None, "", (
                    last_failure() or "local tts server unavailable"
                )
        elif not probe(base):
            # Fast path only: never spawn, and do NOT latch anything - the
            # server may come up later.
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
            error = f"local tts http {response.status_code}"
            if 500 <= int(response.status_code) < 600:
                # The server is ALIVE (it answered) but cannot synthesize -
                # observed: a healthy /v1/models next to a /v1/audio/speech
                # that 500s on an upstream 403. Stop readiness from claiming
                # it is good, or every sentence would pay for a known failure.
                try:
                    from .tts_bootstrap import mark_degraded

                    mark_degraded(f"{error} from {LOCAL_TTS_URL}")
                except Exception:
                    pass
            return False, None, "", error
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


_sapi_lock = threading.Lock()


def _sapi_pcm(text):
    """Offline Windows SAPI voice - the stage that makes silence impossible.

    Uses pywin32's SAPI dispatch directly rather than `pyttsx3`: measured
    0.25s against 1.55s for the same utterance, and it does not route through
    comtypes, whose `__del__` printed `ValueError: COM method call without
    VTable` into the boot log every time an engine was collected.

    Needs no network, no API key and no quota, so it still speaks when every
    cloud provider is exhausted AND the edge-tts server cannot reach Microsoft
    (observed 2026-11-01: a persistent `WSServerHandshakeError: 403` from
    speech.platform.bing.com, behind a perfectly healthy /v1/models).

    Returns the same (ok, pcm, error, quota) shape as `_local_pcm`.
    """
    global _audio_segment_cls
    if not str(text or "").strip():
        return False, None, "", "sapi tts: empty text"
    # SAPI is COM: synthesising from two threads at once corrupts the engine,
    # so every call takes the lock.
    with _sapi_lock:
        tmp_path = None
        voice = None
        stream = None
        try:
            import pythoncom
            import win32com.client

            # CoInitialize'd but deliberately NOT CoUninitialize'd. Pairing
            # them made pywin32 finalise IUnknown pointers after the apartment
            # was gone, printing "Win32 exception occurred releasing IUnknown"
            # on every use. Leaving the apartment for the process lifetime is
            # cheap and silent; it is per-thread and idempotent.
            pythoncom.CoInitialize()
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            # Prefer a female voice so the fallback does not sound like a
            # different person when the cloud voice is unavailable.
            for candidate in voice.GetVoices():
                desc = str(candidate.GetDescription() or "").lower()
                if "zira" in desc or "female" in desc:
                    try:
                        voice.Voice = candidate
                    except Exception:
                        pass
                    break
            voice.Rate = -1          # SAPI range is -2..2; slightly unhurried
            voice.Volume = 100
            handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_path = handle.name
            handle.close()
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Open(tmp_path, 3)          # 3 = SSFMCreateForWrite
            voice.AudioOutputStream = stream
            voice.Speak(text)
            voice.AudioOutputStream = None
            stream.Close()
            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                return False, None, "", "sapi tts produced no audio"
            if _audio_segment_cls is None:
                from pydub import AudioSegment

                _audio_segment_cls = AudioSegment
            segment = _audio_segment_cls.from_file(tmp_path, format="wav")
            pcm = (
                segment.set_frame_rate(SAMPLE_RATE)
                .set_channels(1)
                .set_sample_width(2)
                .raw_data
            )
            if not pcm:
                return False, None, "", "sapi tts produced empty pcm"
            return True, pcm, "", ""
        except Exception as error:
            return False, None, "", f"sapi tts failed: {api_pools.sanitize(error)}"
        finally:
            # Release the COM objects before the lock is dropped so their
            # finalisers never run while another thread is inside SAPI.
            try:
                if stream is not None:
                    stream.Close()
            except Exception:
                pass
            try:
                if voice is not None:
                    voice.AudioOutputStream = None
            except Exception:
                pass
            del stream, voice
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


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
    """One Gemini TTS call. Returns (ok, pcm_bytes, error, quota_failure).

    ROUND ZB-LAT/NATURAL (2026-11-03): the delivery brief is STYLE-BY-PROMPT,
    which is the mechanism the Gemini docs actually document for this model
    family - `gemini-3.1-flash-tts-preview` exposes "steerable prompts and
    expressive audio tags", not SSML `<prosody>` and not numeric rate/pitch
    (ai.google.dev/gemini-api/docs/speech-generation). So the brief lives in
    the instruction text rather than in a config field.

    It asks for conversational delivery INCLUDING a genuine hesitation where a
    person would hesitate, while putting figures, moves, code, instructions and
    safety wording explicitly out of bounds. The hesitation is a matter of HOW,
    never an inserted word - `Do not add or omit any word` is still absolute,
    so nothing about the spoken content can drift. Perceptual quality of this
    brief is UNVERIFIED: no human has compared before/after audio.
    """
    url = f"{GEMINI_NATIVE_BASE}/{model}:generateContent"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "You are F.R.I.D.A.Y., a calm, capable and respectful assistant. "
                            "Keep spoken delivery neutral, precise and supportive. "
                            "Do not add jokes, insults or profanity. Read this reply aloud "
                            "naturally at an even, friendly pace; mild warmth, no artificial "
                            "cheerfulness. Where these are ordinary words, let it sound like a "
                            "real person talking: pause naturally at the commas, and allow a "
                            "slight, genuine hesitation in the places a person would hesitate. "
                            "That hesitation is HOW you deliver it, never a word you insert - "
                            "do not add \"um\", \"uh\" or any other word, and do not omit any "
                            "word. Numbers, chess moves, code, instructions and safety messages "
                            "stay crisp, unhurried and word-for-word. Synthesize speech only:\n"
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
        """Speak cloud-first. Returns PCM bytes or None; records LAST_STAGE.

        ROUND LAB-2D2: a user-disable on chr_gemini_tts excludes the cloud
        pool at this entry point (the local edge/sapi voices follow after;
        those are NOT user-disabled here - the eligibility lane is the cloud
        pool, and the local fallback is the honest no-eligible-cloud voice
        which still produces real audible speech). A healthy main cloud TTS
        serves with the model that actually answered (LAST_STAGE truth).
        """
        text = str(text or "").strip()
        if not text:
            return None
        try:
            from . import lab_authority as _la
            if _la.pool_user_disabled("gemini_tts"):
                print("[gemini tts] user-disabled by the user; skipping the "
                      "cloud TTS lane", flush=True)
                quota_pool = None
            else:
                quota_pool = api_pools.pool("gemini_tts")
        except Exception:
            quota_pool = api_pools.pool("gemini_tts")
        # CLOUD FIRST (Sir's decision, 2026-09-30). This chain used to try the
        # LOCAL voice first, and _local_pcm() calls ensure_edge_tts_server(),
        # which BLOCKS for up to 45s waiting for a server that often is not
        # running. Measured on this machine: cloud TTS answers in 0.27s, a
        # failed local start burns the full 45s -> a measured 62-second silence
        # between tts_start and first audio. FRIDAY must never make the user
        # wait a minute to hear a reply she could have spoken instantly.
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
                        # 2026-10-02 audit: an empty 200 ("returned no audio"),
                        # a network blip and a bad payload all landed HERE too,
                        # and fail_model() with no cooldown defaults to 21600s -
                        # so one transient empty response muted cloud TTS for
                        # six hours. Distinguish a real verdict from a blip.
                        if _is_transient_tts_failure(error):
                            api_pools.fail_model(
                                "gemini_tts", model,
                                cooldown=api_pools.RATE_LIMIT_COOLDOWN_SECONDS,
                            )
                        else:
                            api_pools.fail_model("gemini_tts", model)
                        break

        # Cloud could not speak. Now - and only now - the local voices, in
        # order of quality. The server start here is BOUNDED and its readiness
        # is cached by tts_bootstrap, so the cost is paid at most once per
        # session rather than per sentence; a failure sets a cooldown instead
        # of muting FRIDAY for the rest of the run.
        ok, pcm, _q, error = _local_pcm(text, allow_start=True)
        if ok:
            self._record_stage("local")
            return pcm
        print(f"[local tts] {error}", flush=True)
        # Offline Windows SAPI: no network, no key, no quota. This is what
        # stops "the cloud is exhausted" from meaning "FRIDAY is silent".
        ok, pcm, _q, error = _sapi_pcm(text)
        if ok:
            self._record_stage("sapi")
            return pcm
        print(f"[sapi tts] {error}", flush=True)
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

    # ROUND ZB-REC (2026-11-02): circuit breaker for TOTAL synthesis failure.
    #
    # Observed live: Gemini returned no audio, the local edge server returned
    # HTTP 500, and RealtimeTTS's `play()` then re-entered itself from
    # text_to_stream.py:451 until RecursionError. Because that raise happens
    # OUTSIDE play()'s try/finally, `is_playing_flag` and `play_lock` were left
    # latched, so every later `play_async()` was refused for the rest of the
    # process - no `tts_first_audio`, no `tts_end`, and the watchdog never
    # cleared it.
    #
    # We cannot patch the wheel, so the FRIDAY-side defence is: stop feeding
    # the stream once every engine is provably dead, and recover the latch when
    # the playback thread is gone. Bounded attempts, never an audible promise
    # we cannot keep - the caller reports the degradation as TEXT/STATUS.
    DEGRADE_AFTER = max(1, int(os.environ.get("FRIDAY_TTS_FAIL_STREAK", "3")))
    DEGRADE_SECONDS = max(1.0, float(os.environ.get("FRIDAY_TTS_DEGRADE_SECONDS", "45")))

    def _record_stage(self, stage):
        _record_stage(stage, self)

    def __init__(self):
        global _LAST_ENGINE
        self.engine_name = "gemini-cloud-first"
        self.can_consume_generators = False
        self.queue = queue.Queue()
        self.voices = [GEMINI_TTS_VOICE, LOCAL_TTS_VOICE]
        self.voice = GEMINI_TTS_VOICE
        self.voice_bridge = CloudFirstTTSVoice()
        self._fail_streak = 0
        self._degraded_until = 0.0
        self._degraded_reason = ""
        _LAST_ENGINE = self

    def post_init(self):
        return

    def get_stream_info(self):
        return _FORMAT, _CHANNELS, _RATE

    # -- degradation state ------------------------------------------------
    def is_degraded(self):
        from .user_routes import selected
        if selected():
            return True  # Unassigned speech, not an observed provider failure.
        """True while every engine is known dead; self-clears when the window ends."""
        if self._degraded_until <= 0.0:
            return False
        if time.monotonic() >= self._degraded_until:
            # Cooldown elapsed: allow one honest re-attempt instead of staying
            # muted forever (the failure may have been a 60s quota window).
            self._degraded_until = 0.0
            self._degraded_reason = ""
            self._fail_streak = 0
            return False
        return True

    def degraded_status(self):
        from .user_routes import selected
        if selected():
            return {"degraded": True, "reason": "Speech output is not configured by local Brain/Vision setup.", "fail_streak": 0, "retry_in_seconds": 0.0, "last_stage": None}
        """Truthful status for logs/trace/HUD. Never claims speech is possible."""
        now = time.monotonic()
        active = self._degraded_until > 0.0 and now < self._degraded_until
        return {
            "degraded": active,
            "reason": self._degraded_reason if active else "",
            "fail_streak": self._fail_streak,
            "retry_in_seconds": round(max(0.0, self._degraded_until - now), 1) if active else 0.0,
            "last_stage": getattr(self, "LAST_STAGE", None),
        }

    def _note_failure(self):
        self._fail_streak += 1
        if self._fail_streak >= self.DEGRADE_AFTER:
            self._degraded_until = time.monotonic() + self.DEGRADE_SECONDS
            self._degraded_reason = (
                "all TTS engines failed %d times in a row (last stage %r); "
                "not feeding the audio stream for %.0fs"
                % (self._fail_streak,
                   getattr(self, "LAST_STAGE", None),
                   self.DEGRADE_SECONDS)
            )
            print(f"[tts degraded] {self._degraded_reason}", flush=True)

    def _note_success(self):
        self._fail_streak = 0
        self._degraded_until = 0.0
        self._degraded_reason = ""

    def synthesize(self, text):
        from .user_routes import selected
        if selected():
            self._degraded_reason = "Voice output is not assigned by local Brain/Vision setup."
            return False
        # BOUNDED: while degraded we do not burn a cloud + local + SAPI attempt
        # per sentence that is certain to fail. RealtimeTTS drops the sentence
        # (its single-engine path sleeps 0.2s and breaks) - which is exactly
        # what keeps `char_iter.iterated_text` non-empty and stops the
        # `play()` re-entry at text_to_stream.py:448 from looping.
        if self.is_degraded():
            return False
        pcm = self.voice_bridge.speak(text)
        # `speak()` records its stage on the VOICE instance, so mirror it onto
        # the engine: server._tts_stage_now() reads engine.LAST_STAGE, and
        # without this a successful gemini/sapi utterance was reported as
        # "unknown" / fallback_failed by voice_trace.
        stage = getattr(self.voice_bridge, "LAST_STAGE", None)
        if stage:
            _record_stage(stage, self)
        if not pcm:
            # Last resort before going mute: the offline voice. Here a bounded
            # attempt to START the local server is justified (every other
            # option has already failed), but it is capped so a dead spawn
            # cannot hang the audio pipeline indefinitely.
            ok, pcm, _quota, _error = _local_pcm(text, allow_start=True)
            if ok:
                self._record_stage("local")
        if not pcm:
            # Publish the failure on the ENGINE too. `_tts_stage_now()` and
            # voice_trace read engine.LAST_STAGE; leaving it stale from an
            # earlier successful turn would claim a stage that did not serve
            # this utterance.
            _record_stage(getattr(self.voice_bridge, "LAST_STAGE", None) or "none",
                          self)
            self._note_failure()
            return False
        self._note_success()
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

# The engine created for this process (set by CloudFirstTTSEngine.__init__).
# Exposed so ANY module - status_ui, the HUD, self_awareness - can ask whether
# voice is currently dead without importing server.py (which would be a
# circular import) and without needing server_state in hand.
_LAST_ENGINE = None


def is_degraded():
    """True when the process's TTS engine is in its bounded failure window.

    Fail-safe: no engine, or an engine without the health API, reports False,
    so this can never suppress normal speech.
    """
    engine = _LAST_ENGINE
    checker = getattr(engine, "is_degraded", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def degraded_status():
    """Truthful degradation status for logs/trace/HUD. Never claims audio."""
    engine = _LAST_ENGINE
    reader = getattr(engine, "degraded_status", None)
    if callable(reader):
        try:
            return dict(reader())
        except Exception:
            pass
    return {"degraded": False, "reason": "", "fail_streak": 0,
            "retry_in_seconds": 0.0, "last_stage": None}
