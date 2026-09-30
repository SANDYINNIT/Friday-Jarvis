"""Cloud speech-to-text with per-account quota failover.

Primary: Groq (keys 1-4) with whisper-large-v3-turbo. Emergency: Deepgram.
Every failure returns down to the next account in the pool; when the whole
cloud chain is exhausted the caller falls back to the local recorder, so the
assistant keeps hearing even while offline.
"""

import io
import os
import wave
import base64

import requests

from . import api_pools

GROQ_BASE = os.environ.get("FRIDAY_GROQ_BASE", "https://api.groq.com/openai/v1")
DEEPGRAM_BASE = os.environ.get("FRIDAY_DEEPGRAM_BASE", "https://api.deepgram.com/v1")
GROQ_STT_MODEL = os.environ.get("FRIDAY_GROQ_STT_MODEL", "whisper-large-v3-turbo")
DEEPGRAM_STT_MODEL = os.environ.get("FRIDAY_DEEPGRAM_STT_MODEL", "nova-3")
# Gemini Stage 3: dedicated accounts reused for transcription. Gemini free
# tier quotas are per-account AND per-model, so using the Brain/Vision
# accounts for the transcribe model consumes its own private bucket.
GEMINI_STT_MODEL = os.environ.get("FRIDAY_GEMINI_STT_MODEL", "gemini-3.5-transcribe")
GEMINI_NATIVE_BASE = os.environ.get(
    "FRIDAY_GEMINI_NATIVE_BASE", "https://generativelanguage.googleapis.com/v1beta/models"
)
STT_TIMEOUT = float(os.environ.get("FRIDAY_STT_TIMEOUT", "30"))
STT_CHAIN = ("stt_groq", "stt_deepgram", "gemini_stt")


def pcm_to_wav(pcm, rate=16000, channels=1, sample_width=2):
    """Wrap raw mono-int16 PCM samples into a WAV file byte string."""
    if not pcm:
        return b""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(sample_width)
        target.setframerate(rate)
        target.writeframes(pcm)
    return buffer.getvalue()


def _quota_failure(status):
    return status in (401, 402, 403, 429)


def _groq_transcribe(key, wav_bytes):
    headers = {"Authorization": f"Bearer {key}"}
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
    data = {"model": GROQ_STT_MODEL, "response_format": "json"}
    try:
        response = requests.post(
            f"{GROQ_BASE}/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
            timeout=STT_TIMEOUT,
        )
    except requests.RequestException as error:
        return False, None, f"groq stt request error: {api_pools.sanitize(error)}", False
    if response.status_code != 200:
        return False, None, f"groq stt http {response.status_code}", _quota_failure(response.status_code)
    try:
        text = (response.json() or {}).get("text", "").strip()
    except ValueError:
        return False, None, "groq stt bad json", False
    return True, text, "", False


def _deepgram_transcribe(key, wav_bytes):
    headers = {"Authorization": f"Token {key}"}
    params = {"model": DEEPGRAM_STT_MODEL, "smart_format": "true", "punctuate": "true"}
    try:
        response = requests.post(
            f"{DEEPGRAM_BASE}/listen",
            headers=headers,
            params=params,
            data=wav_bytes,
            timeout=STT_TIMEOUT,
        )
    except requests.RequestException as error:
        return False, None, f"deepgram stt request error: {api_pools.sanitize(error)}", False
    if response.status_code != 200:
        return False, None, f"deepgram stt http {response.status_code}", _quota_failure(response.status_code)
    try:
        payload = response.json()
        alternatives = payload.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])
        text = (alternatives[0] or {}).get("transcript", "").strip() if alternatives else ""
    except (ValueError, IndexError, TypeError, AttributeError):
        return False, None, "deepgram stt bad payload", False
    return True, text, "", False


def _gemini_transcribe(key, wav_bytes):
    """Gemini generateContent with inline WAV audio -> transcript text."""
    url = f"{GEMINI_NATIVE_BASE}/{GEMINI_STT_MODEL}:generateContent"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": "Transcribe this audio exactly as spoken. "
                                "Reply with ONLY the transcript text, nothing else."
                    },
                    {
                        "inline_data": {
                            "mime_type": "audio/wav",
                            "data": base64.b64encode(wav_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ]
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=STT_TIMEOUT)
    except requests.RequestException as error:
        return False, None, f"gemini stt request error: {api_pools.sanitize(error)}", False
    if response.status_code != 200:
        return False, None, f"gemini stt http {response.status_code}", _quota_failure(response.status_code)
    try:
        payload = response.json()
        candidates = payload.get("candidates") or []
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text", "")) for part in parts).strip()
    except (ValueError, IndexError, KeyError, TypeError):
        return False, None, "gemini stt bad payload", False
    return True, text, "", False


def _call_stt(key, pool_name, wav_bytes):
    if pool_name == "stt_groq":
        return _groq_transcribe(key, wav_bytes)
    if pool_name == "stt_deepgram":
        return _deepgram_transcribe(key, wav_bytes)
    if pool_name == "gemini_stt":
        return _gemini_transcribe(key, wav_bytes)
    return False, None, f"unknown stt pool {pool_name}", False


def transcribe_wav(wav_bytes, enabled=True):
    """Return (text, provider_name, error). Empty text on total failure."""
    if not enabled or not wav_bytes:
        return "", "", ""
    ok, payload, provider, error = api_pools.run_chain(
        STT_CHAIN, lambda key, pool_name: _call_stt(key, pool_name, wav_bytes)
    )
    if ok and payload:
        return payload, provider, ""
    return "", "", error