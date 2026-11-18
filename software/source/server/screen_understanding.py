"""Small, on-demand screen description bridge for the voice server."""

import base64
import io
import os
import re
import time

import requests
from PIL import ImageGrab
import pytesseract

from .file_logger import friday_logger

# PHASE 5 (2026-10-03): this module used `_LOGGER` in three except-blocks
# (live regions, window context, uia context) but NEVER defined it, so every one
# of those handlers raised NameError instead of logging its context - the exact
# "diagnostic that silently cannot fail" pattern this round is fixing elsewhere.
# Defined here, deliberately NOT via friday_logger's private state, so it is
# available at import time and in tests.
_LOGGER = friday_logger()

from .windows_context import get_active_window_context
from .model_registry import VISION, select_model
from .identity_integration import vision_attempt as _identity_vision_attempt
from .identity_integration import serving_disabled as _serving_disabled
from .identity_integration import request_current as _request_current
from . import api_pools
from .personality import neutral_messages


SCREEN_REQUESTS = (
    # Visual/descriptive asks — answered by the VISION CHAIN (a real
    # description of what's rendered). Note: NOT matched by the window-title
    # router: "looking at / what do you see" questions were deliberately
    # removed from command_router._ACTIVITY_RE so they land HERE, because a
    # window title ("Right now ... (opera.exe) has focus") sounds robotic.
    "what am i looking at",
    "what am i looking at right now",
    "what are you looking at",
    "what are you looking at right now",
    "what do you see",
    "what can you see",
    "what are you seeing",
    "what is on my screen",
    "what's on my screen",
    # Contraction-free and wording variants that were MISSING and therefore
    # silently skipped the vision path (FRIDAY then answered about the screen
    # without looking at it). All are screen-specific, so they cannot collide
    # with ordinary chat.
    "whats on my screen",
    "what is on the screen",
    "whats on the screen",
    "what is on my monitor",
    "what is on my display",
    "what does my screen show",
    "what does my screen say",
    "what shows on my screen",
    "what am i looking at on my screen",
    "take a look at my screen",
    "look at my screen",
    "catch a look at my screen",
    "glance at my screen",
    "see my screen",
    "describe my screen",
    "read my screen",
    "what do you think i'm doing",
    "what do you think im doing",
    "what do you think i am doing",
    "take a screenshot",
    "take a screenshot of my screen",
    "screenshot my screen",
    # "show me" family — the bare "show me"/"show me your screen" forms are
    # captured on Telegram by server.py's pre-flight (photo-to-phone); these
    # ensure the desk/voice path also answers deterministically instead of
    # letting the brain author looping screenshot code.
    "show me your screen",
    "show me the screen",
    "show me my screen",
    "show me what you see",
    "show me what u see",
    "show me what's on my screen",
    "show me your view",
    "show me your camera",
    "show me your desktop",
    "show me a screenshot",
    "show me the screenshot",
    "show yourself",
    "show your screen",
    "can you see my screen",
    "can you see me",
    "are you looking at my screen",
)

OCR_REQUESTS = (
    "read my screen",
    "read the text on my screen",
    "what text is on my screen",
    "what does my screen say",
)

# "What is that thing DOING / what is it ABOUT" asks are not literal
# transcription, but they are impossible to answer without reading the pixels.
# They used to fall through to pure captioning, which produced the observed
# failure: "a simple or minimalistic web page... likely a background display"
# for a screen that was actually a full app. These phrases now also trigger OCR
# so the answer is grounded in the real text.
OCR_INTENT_REQUESTS = (
    "what does it do",
    "what does the app do",
    "what does the application do",
    "what does that app do",
    "what does this app do",
    "what does the program do",
    "what is it about",
    "what's it about",
    "what is that about",
    "what is this about",
    "what is on the left",
    "what is on the right",
    "what is that screen",
    "what's that screen",
    "what is the left one",
    "what is the right one",
    "what does the left one",
    "what does the right one",
    "tell me about",
    "explain what i am looking at",
    "explain what i'm looking at",
    "what am i working on",
    "what am i working on right now",
    "what code is this",
    "what does this code do",
    "what does the error say",
    "read the text",
    "read the code",
    "what text",
    "what code",
    "what does the terminal",
    "what is the terminal",
)

VISION_TIMEOUT_SECONDS = float(os.getenv("FRIDAY_VISION_TIMEOUT", "25"))
# Modal (GLM-5.3 Flash NVFP4) REASONS BEFORE IT ANSWERS, and those reasoning
# tokens are billed against max_tokens. Measured on a real desktop capture with
# the real screen prompt: 400 -> 402 reasoning + 0 chars, 1000 -> 1000 + 0 chars,
# 1600+ -> a real answer. A tight budget therefore yields an EMPTY reply, not a
# short one, so this must sit well above the reasoning cost. 2000 leaves headroom
# for a busy screen (measured 1184-1459 completion tokens) without paying the
# 2600-token latency. The old value of 400 silently broke every complex screen
# read - it was reported to Sir as "vision unavailable".
MODAL_VISION_MAX_TOKENS = int(os.getenv("FRIDAY_MODAL_VISION_MAX_TOKENS", "2000"))
# GLM-5.3 Flash has thinking ALWAYS ON and, per NVIDIA's NIM docs, "the default
# reasoning effort is max" - reasoning_effort is the documented control for it.
# For describing a screenshot we want perception, not deliberation: measured on a
# real capture, effort=low answered in 2.8-3.1s with 1378 chars of specific
# detail, versus 7.0s at the default. Set FRIDAY_MODAL_VISION_REASONING_EFFORT
# to an empty string to omit the parameter entirely for an endpoint that
# rejects it (the code retries without it if the request comes back 400).
MODAL_VISION_REASONING_EFFORT = os.getenv(
    "FRIDAY_MODAL_VISION_REASONING_EFFORT", "low"
).strip()
# Local vision gets its OWN (small) budget so a slow CPU-only model (this
# machine's Ryzen 5 2600 took ~135s for qwen2.5vl:3b) can never stall a
# request: it yields and the title/app fallback answers instead.
LOCAL_VISION_TIMEOUT_SECONDS = float(os.getenv("FRIDAY_LOCAL_VISION_TIMEOUT", "20"))
# Gemini vision gets a SHORT trial budget (its full 25s chain of 5 models
# stalled voice turns by a minute whenever the free endpoint hung); on trial
# failure the fast OpenRouter stage answers.
GEMINI_VISION_TRIAL_TIMEOUT = float(os.getenv("FRIDAY_GEMINI_VISION_TRIAL_TIMEOUT", "8"))
SCREEN_RESPONSE_LIMIT = 1800

GEMINI_BASE = os.environ.get(
    "FRIDAY_GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
OPENROUTER_BASE = os.environ.get("FRIDAY_OPENROUTER_BASE", "https://openrouter.ai/api/v1")
# 127.0.0.1, not `localhost` - a `localhost` URL costs a measured 2s connect
# timeout per local vision call on this machine (::1 blackholes SYNs).
LOCAL_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", "http://127.0.0.1:11434/api/chat")
GEMINI_VISION_MODEL = os.environ.get("FRIDAY_GEMINI_VISION_MODEL", "gemini-3.1-flash-lite")
OPENROUTER_VISION_MODEL = os.environ.get(
    # Round N (2026-10-03). The previous default
    # `inclusionai/ling-3.0-flash-vl:free` is RETIRED -
    # `GET https://openrouter.ai/api/v1/models/inclusionai/ling-3.0-flash-vl:free`
    # returns HTTP 404 - so this stage failed instantly with a hard error and the
    # screen pipeline's cloud fallback was dead.
    #
    # The replacement was chosen by probing this module's OWN request shape (its
    # `_openai_vision_payload`, its own describe_screen prompt, and its own
    # `_usable_vision_text` acceptance test) against a real screen capture, NOT
    # by copying the model validated for friday_locate.py. That matters: the
    # locate helper consumes JSON coordinates and qwen3.8-27b is good at it, but
    # for a free-text description qwen3.8 burns the ENTIRE token budget on
    # `reasoning` and returns an empty `content` (measured: 7552 reasoning chars,
    # finish_reason="length", at 4000 tokens) - it would have been dead here in a
    # subtler way.
    #
    # Measured 2026-10-03, real 1280px screen capture, this module's prompt:
    #   dots-studio/dots-3-note-preview:free @300  -> usable 0/4 (empty content)
    #   dots-studio/dots-3-note-preview:free @1200 -> usable 1/4
    #   dots-studio/dots-3-note-preview:free @2000 -> usable 3/3  <-- chosen
    #   nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free @2000 -> 1/2, 29s
    #   qwen/qwen3.8-27b:free                     -> 0 usable at 300/2000/4000
    #   google/gemma-4-31b-it:free / gemma-4-26b-a4b-it:free -> HTTP 429
    # Caveat recorded honestly: at 2000 this averages ~21s against
    # VISION_TIMEOUT_SECONDS (25s), so it is a tight last-resort fallback. It is
    # still strictly better than an instant 404. Override with
    # FRIDAY_OPENROUTER_VISION_MODEL as before.
    "FRIDAY_OPENROUTER_VISION_MODEL", "dots-studio/dots-3-note-preview:free"
)
# Round N (2026-10-03): the OpenRouter stage needs its own token budget. The
# shared payload builder's 300 is enough for Gemini but starves reasoning-style
# models, which return HTTP 200 with an EMPTY `content` when truncated -
# indistinguishable from a broken model. Gemini's budget is unchanged.
OPENROUTER_VISION_MAX_TOKENS = int(
    os.environ.get("FRIDAY_OPENROUTER_VISION_MAX_TOKENS", "2000")
)
# Gemini free-tier quotas are per-account AND per-model: the vision-gemini
# stage walks this chain on the SAME dedicated account before handing off to
# openrouter. Strongest first, env-overridable.
VISION_GEMINI_CHAIN = [
    name.strip()
    for name in os.environ.get(
        "FRIDAY_VISION_GEMINI_CHAIN",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    ).split(",")
    if name.strip()
]
VISION_OPENROUTER_POOL = "vision_openrouter"
VISION_CHAIN = ("vision_gemini", "vision_openrouter")

# LOCAL screen-description chain, tried in order (the screenshot producer the
# brain then reasons over). moondream is ~2-4s and sees the full multi-monitor
# composite; the heavier registered vision model (qwen2.5vl:3b) is the bounded
# deep-read fallback. FRIDAY_LOCAL_VISION_CHAIN overrides.
LOCAL_VISION_CHAIN = [
    m.strip() for m in os.getenv("FRIDAY_LOCAL_VISION_CHAIN", "moondream").split(",") if m.strip()
]
# The brain that composes the FINAL answer from the vision description
# ("screenshot -> local vision -> Qwen3 reasoning -> response").
SCREEN_BRAIN_MODEL = os.getenv("FRIDAY_SCREEN_BRAIN_MODEL", "qwen3:8b")
# A 20-40 word photo caption does not need a 5 GB model. Measured on this
# machine: qwen3:8b did not answer inside 45s, which left the caption EMPTY and
# produced a canned acknowledgement. A 0.4 GB model answers this in ~10s.
SCREEN_CAPTION_MODEL = os.getenv("FRIDAY_SCREEN_CAPTION_MODEL", "qwen2.5:0.5b")
SCREEN_CAPTION_TIMEOUT_SECONDS = float(
    os.getenv("FRIDAY_SCREEN_CAPTION_TIMEOUT", "12")
)
# The vision read is OFF by default for the photo-caption path. Measured: on a
# dense desktop screenshot the 3B local vision model produced a confidently
# WRONG description ("a webpage in a serif font") and cost ~17.6s, while
# Tesseract plus the focused window title were both correct and cost ~1.5s.
# Set FRIDAY_SCREEN_CAPTION_VISION=1 to include it anyway.
SCREEN_CAPTION_USE_VISION = os.getenv("FRIDAY_SCREEN_CAPTION_VISION", "0") not in (
    "0", "false", "False", "no", ""
)
# Short budget: the brain only REFINES the local caption. qwen3:8b on Sir's
# CPU is ~10-20 tok/s warm, so a ~50-word spoken line lands in ~3-6s; keep_alive
# 30m pins qwen3 + moondream resident so a cold 5.2GB reload (30-45s) only
# happens after a long idle. 30s covers even the reload edge once.
SCREEN_BRAIN_TIMEOUT_SECONDS = float(os.getenv("FRIDAY_SCREEN_BRAIN_TIMEOUT", "30"))

# Typographic characters -> ASCII, applied before matching request phrases.
# U+2019 (') alone is the important one: it is what iPhone/Telegram keyboards
# and most STT output produce, and without folding it the most natural phrasing
# of a screen question silently skipped the vision path entirely.
_TEXT_LOOKALIKES = {
    "‘": "'",
    "’": "'",
    "ʼ": "'",
    "′": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    " ": " ",
    "…": "...",
}


def _normalize_request_text(text):
    """Lowercase, collapse whitespace, and fold typographic characters to ASCII.

    Needed because the phrase list is written with ASCII apostrophes, but real
    requests arrive with U+2019: iPhone/Telegram keyboards auto-replace ' with
    ' and STT emits it constantly. Measured: "what's on my screen" (ASCII)
    matched and took the vision path, while "what's on my screen" (U+2019)
    did NOT - so FRIDAY answered questions about the screen WITHOUT ever looking
    at it, which is exactly the ungrounded reply the screen policy forbids. The
    failure was silent and looked like a normal conversation.
    """
    lowered = " ".join(str(text).lower().split())
    for fancy, plain in _TEXT_LOOKALIKES.items():
        lowered = lowered.replace(fancy, plain)
    return lowered.rstrip(".!? ")


def is_screen_request(text):
    normalized = _normalize_request_text(text)
    # A screenshot requested AFTER another action is verification/delivery for
    # that action, not permission to inspect the pre-action desktop.
    screenshot_at = normalized.find("screenshot")
    if screenshot_at > 0:
        before = normalized[:screenshot_at]
        ordered = re.search(
            r"\b(?:then|after(?:ward|wards)?|once|when\s+(?:that(?:'s| is)?|it(?:'s| is)?)\s+done|"
            r"and\s+(?:then\s+)?)\b",
            before,
        )
        action = re.search(
            r"\b(?:open|close|find|search|type|write|click|change|create|run|move|"
            r"rename|delete|install|start|stop|play|pause|send|do|finish)\b",
            before,
        )
        if ordered and action:
            return False
    return any(phrase in normalized for phrase in SCREEN_REQUESTS)


def is_ocr_request(text):
    normalized = _normalize_request_text(text)
    return any(phrase in normalized for phrase in OCR_REQUESTS)


def select_vision_model():
    """Return the configured vision tag, using the explicit registry default."""
    configured = os.getenv("FRIDAY_VISION_MODEL")
    if configured:
        return configured
    selected = select_model(VISION)
    return selected.name if selected else "gemma4:e2b"


def _vision_quota_failure(status):
    # Do not ban on 400 (bad request/model) to avoid aggressive pool lockout
    return status in (401, 402, 403, 429)


def _openai_vision_payload(jpeg_bytes, prompt, model, max_tokens=300):
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }


def _usable_vision_text(raw):
    """Normalize a provider 'content' field. Free vision models sometimes emit
    literal filler ('None', 'N/A', 'null') when the frame stumps them — treat
    that as NO description so callers fall back, never speak 'None'."""
    if isinstance(raw, list):
        chunks = []
        for part in raw:
            if isinstance(part, dict):
                chunk = (part.get("text") or "").strip()
                if chunk:
                    chunks.append(chunk)
            elif part:
                chunks.append(str(part))
        raw = " ".join(chunks)
    text = (raw or "").strip()
    if text.lower() in ("none", "n/a", "null", "no", "na", "-", "...", "..."):
        return ""
    if len(text) < 4:
        return ""
    return text


def _gemini_vision(key, jpeg_bytes, prompt, model, timeout=None):
    if _serving_disabled("vision", "vision_gemini", model, GEMINI_BASE):
        return False, None, "Vision binding is disabled.", False
    headers = {"Authorization": f"Bearer {key}"}
    with _identity_vision_attempt("vision_gemini", model, GEMINI_BASE) as observed_success:
        try:
            response = requests.post(
                f"{GEMINI_BASE}/chat/completions",
                headers=headers,
                json=_openai_vision_payload(jpeg_bytes, prompt, model),
                timeout=timeout or VISION_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            return False, None, f"gemini vision request error: {api_pools.sanitize(error)}", False
        if response.status_code != 200:
            return False, None, f"gemini vision http {response.status_code}", _vision_quota_failure(response.status_code)
        try:
            content = (response.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
        except (ValueError, IndexError, TypeError, AttributeError):
            return False, None, "gemini vision bad payload", False
        text = _usable_vision_text(
            content,
        )
        if not text:
            return False, None, "gemini vision returned no description", False
        observed_success()
        return True, text, "", False


def _openrouter_vision(key, jpeg_bytes, prompt):
    if _serving_disabled("vision", "vision_openrouter", OPENROUTER_VISION_MODEL, OPENROUTER_BASE):
        return False, None, "Vision binding is disabled.", False
    headers = {"Authorization": f"Bearer {key}"}
    with _identity_vision_attempt("vision_openrouter", OPENROUTER_VISION_MODEL, OPENROUTER_BASE) as observed_success:
        try:
            response = requests.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=headers,
                json=_openai_vision_payload(jpeg_bytes, prompt, OPENROUTER_VISION_MODEL,
                                            OPENROUTER_VISION_MAX_TOKENS),
                timeout=VISION_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            return False, None, f"openrouter vision request error: {api_pools.sanitize(error)}", False
        if response.status_code != 200:
            return False, None, f"openrouter vision http {response.status_code}", _vision_quota_failure(response.status_code)
        try:
            content = (response.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
        except (ValueError, IndexError, TypeError, AttributeError):
            return False, None, "openrouter vision bad payload", False
        text = _usable_vision_text(
            content,
        )
        if not text:
            return False, None, "openrouter vision returned no description", False
        observed_success()
        return True, text, "", False


def _call_vision(key, pool_name, jpeg_bytes, prompt, model=None):
    if pool_name == "vision_gemini":
        return _gemini_vision(key, jpeg_bytes, prompt, model or GEMINI_VISION_MODEL)
    if pool_name == "vision_openrouter":
        return _openrouter_vision(key, jpeg_bytes, prompt)
    return False, None, f"unknown vision pool {pool_name}", False


def _modal_vision_model():
    """The model id Modal vision actually sends.

    Single source of truth so the request and the cooldown ban always name the
    SAME model. FRIDAY_MODAL_VISION_MODEL can override the router's default, so
    reading MODAL_MODEL directly would ban a model that was never called.
    """
    try:
        from .brain_router import MODAL_MODEL

        model = MODAL_MODEL
    except Exception:
        model = ""
    return os.environ.get("FRIDAY_MODAL_VISION_MODEL", "") or model


def _modal_vision(jpeg_bytes, prompt, timeout=None):
    """Modal (self-hosted GLM-5.3 Flash NVFP4) vision - PRIMARY (Sir 2026-09-30).

    Same OpenAI-compatible /v1 surface as the brain, so it reuses the pool key
    verbatim and needs no custom auth. Being self-hosted, it has no public
    free-tier quota, so it should answer when Groq/Gemini are throttled.
    """
    pool = api_pools.pool("vision_modal")
    if pool is None or pool.empty:
        return False, None, False, "no modal key"
    base = ""
    model = ""
    try:
        # Single source of truth for the Modal endpoint/model, shared with the
        # brain router (which normalises the base to end in /v1 - without it the
        # endpoint 404s with "route not found").
        from .brain_router import MODAL_BASE, MODAL_MODEL

        base = (MODAL_BASE or "").rstrip("/")
        model = MODAL_MODEL
    except Exception:
        base = (os.environ.get("FRIDAY_MODAL_BASE", "") or "").rstrip("/")
        model = os.environ.get("FRIDAY_MODAL_MODEL", "")
    if not base:
        return False, None, False, "modal base not configured"
    if not base.endswith("/v1"):
        base = base + "/v1"
    model = _modal_vision_model()
    if _serving_disabled("vision", "vision_modal", model, base):
        return False, None, False, "Vision binding is disabled."
    key = pool.next_key()
    if not key:
        return False, None, False, "no modal key"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode()}},
                ],
            }
        ],
        "max_tokens": MODAL_VISION_MAX_TOKENS,
        "temperature": 0.1,
    }
    if MODAL_VISION_REASONING_EFFORT:
        payload["reasoning_effort"] = MODAL_VISION_REASONING_EFFORT
    with _identity_vision_attempt("vision_modal", model, base) as observed_success:
        try:
            response = requests.post(
                # `base` already ends in /v1 (normalised above). This used to append
                # /v1 AGAIN, so every Modal vision call hit
                # .../v1/v1/chat/completions and came back 404 "route not found"
                # in ~0.7s. The brain path never hit this because litellm appends
                # /chat/completions itself - that is why the brain worked and
                # vision silently did not.
                f"{base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=timeout or VISION_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return False, None, False, str(exc)
        if response.status_code >= 400:
            # An endpoint that does not know reasoning_effort may reject the whole
            # request. Drop the parameter and try once more rather than losing vision.
            if response.status_code == 400 and "reasoning_effort" in payload:
                if _serving_disabled("vision", "vision_modal", model, base):
                    return False, None, False, "Vision binding was disabled before retry."
                payload.pop("reasoning_effort", None)
                try:
                    response = requests.post(
                        f"{base}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        timeout=timeout or VISION_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    return False, None, False, str(exc)
            if response.status_code >= 400:
                quota_failure = response.status_code in (401, 403, 429)
                return False, None, quota_failure, f"HTTP {response.status_code}"
        try:
            choice = response.json()["choices"][0]
            content = (choice.get("message", {}).get("content") or "").strip()
            finish = choice.get("finish_reason")
        except Exception as exc:
            return False, None, False, f"bad payload: {exc}"
        if not content and finish == "length":
            if _serving_disabled("vision", "vision_modal", model, base):
                return False, None, False, "Vision binding was disabled before retry."
            # THE MODEL THINKS BEFORE IT ANSWERS, and the reasoning tokens are
            # billed against max_tokens. Measured on a real 1920x1080 desktop
            # capture with the real screen prompt: 400 -> 402 reasoning, 0 chars of
            # answer; 1000 -> 1000 reasoning, still 0 chars; 1600+ -> finished with
            # a real answer. So a tight budget does not return a short answer, it
            # returns NOTHING, and FRIDAY was reporting that to Sir as "cloud vision
            # was unavailable" rather than as the budget bug it was.
            # Retry once with headroom before declaring the provider unusable.
            try:
                payload["max_tokens"] = MODAL_VISION_MAX_TOKENS * 2
                retry = requests.post(
                    f"{base}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    timeout=timeout or VISION_TIMEOUT_SECONDS,
                )
                if retry.status_code < 400:
                    choice = retry.json()["choices"][0]
                    content = (choice.get("message", {}).get("content") or "").strip()
            except Exception:
                pass
        if not content:
            return (
                False,
                None,
                False,
                "empty content (model used the whole token budget reasoning)",
            )
        pool.succeed(key)
        api_pools.model_succeed("vision_modal", model)
        observed_success()
        return True, content, False, ""


def _configured_vision(jpeg_bytes, prompt):
    """Execute only explicit candidates, through existing consent/identity/pool authorities."""
    from .user_routes import candidates
    configured = candidates("vision")
    if configured is None:
        return None
    last_error = "No eligible configured Vision route; check credentials, disabled roles and order."
    deadline = time.monotonic() + VISION_TIMEOUT_SECONDS
    for pool_name, model, base in configured:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _request_current():
            return False, None, "", "Vision request expired or was cancelled; no further route dispatched."
        local = pool_name == "vision_local"
        if not local and not api_pools.cloud_vision_enabled():
            continue
        if _serving_disabled("vision", pool_name, model, base):
            continue
        if api_pools.model_banned(pool_name, model):
            continue
        quota = None if local else api_pools.pool(pool_name)
        key = quota.next_key() if quota is not None and not quota.empty else None
        if not local and not key:
            continue
        with _identity_vision_attempt(pool_name, model, base) as observed_success:
            try:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not _request_current():
                    return False,None,"","Vision request is no longer current; no provider dispatched."
                if local:
                    body = {"model":model,"stream":False,"messages":[{"role":"user","content":prompt,"images":[base64.b64encode(jpeg_bytes).decode("ascii")]}],"options":{"num_predict":300}}
                    response = requests.post(base,json=body,timeout=remaining)
                else:
                    response = requests.post(base.rstrip("/")+"/chat/completions",headers={"Authorization":"Bearer "+key},json=_openai_vision_payload(jpeg_bytes,prompt,model),timeout=remaining)
                if not _request_current() or time.monotonic() > deadline:
                    return False,None,"","Vision result expired/cancelled; remote cancellation is unconfirmed."
                if response.status_code >= 400:
                    last_error = "Configured Vision route returned HTTP " + str(response.status_code)
                    api_pools.fail_model(pool_name,model,cooldown=api_pools.TRANSIENT_COOLDOWN_SECONDS)
                    continue
                data=response.json()
                raw=data.get("message",{}).get("content") if local else data.get("choices",[{}])[0].get("message",{}).get("content")
                text=_usable_vision_text(raw)
                if not text:
                    last_error="Configured Vision route produced no usable description."
                    continue
                if not _request_current():
                    return False,None,"","Late Vision result discarded; remote outcome is not cancellation proof."
                observed_success()
                if quota is not None:quota.succeed(key)
                api_pools.model_succeed(pool_name,model)
                return True,text,pool_name,""
            except Exception:
                last_error="Configured Vision request failed; network/remote outcome unknown."
    return False,None,"",last_error


def _cloud_vision(jpeg_bytes, prompt):
    configured = _configured_vision(jpeg_bytes, prompt)
    if configured is not None:
        return configured
    last_error = ""
    # ROUND LAB-2C: the eligibility consult for the vision lane. Not a second
    # router: the consult only excludes a user-disabled pool from the
    # selection; the real selection stays in this function (modal first,
    # then the Gemini/OpenRouter fallback).
    try:
        from . import lab_authority as _la
        if _la.pool_user_disabled("vision_modal"):
            ok, payload, quota_failure, error = False, None, False, ""
            last_error = "vision_modal is user-disabled by the user"
        else:
            ok, payload, quota_failure, error = _modal_vision(jpeg_bytes, prompt)
    except Exception:
        ok, payload, quota_failure, error = _modal_vision(jpeg_bytes, prompt)
    if ok:
        return True, payload, "vision_modal", ""
    # Ban on a real quota/auth failure, exactly like the Gemini stage below.
    # Without this, a Modal endpoint that returns 401/403/429 was retried on
    # EVERY screenshot forever (it only logged), which is the AI-overheating
    # behaviour the 6-hour ban exists to stop. Transient errors (timeout,
    # connection reset, 5xx) still fall through without a ban, so a brief
    # Modal cold start does not cost us the whole 6 hours.
    if quota_failure:
        api_pools.fail_model("vision_modal", _modal_vision_model())
    if quota_failure and payload is None and error:
        try:
            from .file_logger import friday_logger

            friday_logger().warning("modal vision unavailable: %s", error)
        except Exception:
            pass
    if error:
        last_error = api_pools.sanitize(str(error))
    # Gemini stage: ONE short trial on the strongest available model. Trying
    # the full 5-model chain at the 25s timeout each was stalling screen turns
    # ~60-125s whenever the free-tier Gemini endpoint just hung — then the
    # title fallback answered. (SEARCHES.md / MEMORY.md 2026-09-23.)
    gemini_pool = api_pools.pool("vision_gemini")
    if gemini_pool is not None and not gemini_pool.empty:
        model = next(
            (m for m in VISION_GEMINI_CHAIN
             if not api_pools.model_banned("vision_gemini", m)
             and not _serving_disabled("vision", "vision_gemini", m, GEMINI_BASE)),
            None,
        )
        key = gemini_pool.next_key() if model else None
        if key and model:
            try:
                ok, payload, error, quota_failure = _gemini_vision(
                    key, jpeg_bytes, prompt, model, timeout=GEMINI_VISION_TRIAL_TIMEOUT
                )
            except Exception as exc:
                ok, payload, quota_failure = False, None, False
                error = str(exc)
            if ok:
                gemini_pool.succeed(key)
                api_pools.model_succeed("vision_gemini", model)
                return True, payload, f"vision_gemini/{model}", ""
            if quota_failure:
                api_pools.fail_model("vision_gemini", model)
            last_error = api_pools.sanitize(str(error)) if error else "gemini vision failed"
    # OpenRouter vision as the cloud fallback; merge errors honestly.
    if _serving_disabled("vision", VISION_OPENROUTER_POOL, OPENROUTER_VISION_MODEL, OPENROUTER_BASE):
        return False, None, "", last_error or "Vision binding is disabled."
    ok, payload, provider, error = api_pools.run_chain(
        (VISION_OPENROUTER_POOL,),
        lambda key, pool_name: _call_vision(key, pool_name, jpeg_bytes, prompt),
    )
    if ok:
        return True, payload, provider, ""
    return False, None, provider or "", error or last_error


def _capture_screen():
    """ALL monitors: a dual-screen setup is a 3840x1080 virtual desktop, but
    plain ImageGrab.grab() returns ONLY the primary — the user-visible
    "it didn't look at my second monitor" bug. all_screens=True fixes it."""
    try:
        return ImageGrab.grab(all_screens=True)
    except Exception:
        return ImageGrab.grab()


class _PerMonitorDPI:
    """Temporarily make THIS THREAD per-monitor DPI aware, then restore it.

    WHY THIS EXISTS (2026-10-01): FRIDAY's process runs at SYSTEM_DPI_AWARE, so
    Windows reports a *scaled* monitor in logical pixels. This machine's LEFT
    display is a 1920x1080 panel at 125%, but it was being reported as
    1536x864 - so we were not only getting a black image (see the all_screens
    fix in _capture_regions), we were capturing the WRONG BOX and silently
    dropping 384px off that screen. Measured with the same enumeration:
    system-aware -> 1536x864, per-monitor-v2 -> 1920x1080.

    WHY IT IS SCOPED AND RESTORED RATHER THAN PROCESS-WIDE: mouse and click
    coordinates are computed elsewhere (Open Interpreter's computer module) and
    changing awareness for the whole process would move every click coordinate
    on a scaled monitor. This flips awareness only around the enumeration +
    grab on the calling thread and puts it back, so nothing else can notice.
    """

    def __init__(self):
        self._previous = None
        self._user32 = None

    def __enter__(self):
        try:
            import ctypes

            self._user32 = ctypes.windll.user32
            # CRITICAL: without explicit prototypes ctypes defaults to a 32-bit
            # c_int, which TRUNCATES the DPI_AWARENESS_CONTEXT handle (it is a
            # pointer). Truncating it on the way back left the thread stuck at
            # PER_MONITOR_AWARE after capture instead of restoring the original
            # SYSTEM_DPI_AWARE - which would silently change every later mouse
            # coordinate on a scaled monitor. Measured: awareness 0 before,
            # stuck at 1 after, until these prototypes were declared.
            try:
                self._user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                self._user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
                self._user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            except Exception:
                pass
            # PER_MONITOR_AWARE_V2 is (DPI_AWARENESS_CONTEXT)-4; fall back to
            # PER_MONITOR_AWARE (-3) on older Windows builds.
            for context in (ctypes.c_void_p(-4), ctypes.c_void_p(-3)):
                try:
                    self._previous = self._user32.SetThreadDpiAwarenessContext(
                        context
                    )
                    if self._previous:
                        return self
                except Exception:
                    continue
            self._previous = None
        except Exception:
            self._previous = None
        return self

    def __exit__(self, *exc):
        try:
            # `ctypes` MUST be imported here too: the import inside __enter__ is
            # local to that method, so referencing it here raised NameError and
            # the bare `except: pass` swallowed it - which is why awareness stayed
            # at PER_MONITOR_V2 and never came back. Verified: restoring works
            # once this import exists.
            import ctypes

            if self._user32 is not None and self._previous:
                self._user32.SetThreadDpiAwarenessContext(
                    ctypes.c_void_p(self._previous.value
                                    if hasattr(self._previous, "value")
                                    else self._previous)
                )
        except Exception:
            pass
        return False


def _monitor_layout():
    """Per-monitor geometry, left-to-right, with a human label for each.

    A single 3840x1080 composite is the worst possible input for a vision
    model: every pixel is halved in each direction, so UI text is destroyed.
    Capturing each monitor NATIVELY gives the encoder readable text (measured:
    141 OCR words natively vs 0 from the old 640px composite).
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG),
            ]

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", _RECT), ("rcWork", _RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        MONITORINFOF_PRIMARY = 0x00000001
        user32 = ctypes.windll.user32
        # A bare Python callback is not enough on 64-bit Windows: without an
        # explicit prototype the HANDLE/HMONITOR arguments get truncated and
        # EnumDisplayMonitors silently calls nothing. This exact typing is what
        # makes the per-monitor split work.
        _PROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(_RECT), wintypes.LPARAM,
        )
        user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.POINTER(_RECT), _PROC, wintypes.LPARAM,
        ]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [
            wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO),
        ]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        monitors = []

        def _callback(hmonitor, hdc, lprc, lparam):
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                rect = info.rcMonitor
                monitors.append({
                    "index": len(monitors),
                    "bounds": (rect.left, rect.top,
                               rect.right - rect.left, rect.bottom - rect.top),
                    "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    "device_name": str(info.szDevice),
                })
            return True

        with _PerMonitorDPI():
            user32.EnumDisplayMonitors(None, None, _PROC(_callback), 0)
        if not monitors:
            return []
        monitors.sort(key=lambda m: m["bounds"][0])
        count = len(monitors)
        for position, monitor in enumerate(monitors):
            # Re-index AFTER sorting. The enumeration index is assigned during
            # the ctypes walk, which is NOT left-to-right, so labelling from it
            # swapped "left" and "right" - and "what is the LEFT one about?"
            # then answered about the wrong screen.
            monitor["index"] = position
            if count == 1:
                monitor["label"] = "the only screen"
            elif position == 0:
                monitor["label"] = "the LEFT screen"
            elif position == count - 1:
                monitor["label"] = "the RIGHT screen"
            else:
                monitor["label"] = f"the MIDDLE screen ({position + 1} of {count})"
            # Say which one is the PRIMARY display so "the left one" can never
            # be ambiguous: Windows' X ordering is objective, but the user may
            # be sitting at either monitor.
            if monitor["primary"]:
                monitor["label"] += " (this is the PRIMARY display)"
        return monitors
    except Exception:
        return []


def _live_regions(max_age=None):
    """Regions from the always-live frame buffer, or None if not usable.

    2026-10-02: screen_capture_service keeps one fresh frame per monitor in
    memory (DXcam / Desktop Duplication). When a frame is fresher than
    `max_age` we hand THAT back instead of re-capturing. Measured here: a
    fresh Pillow grab is 51 ms per monitor, and a REGION grab costs the
    same 51 ms because Pillow captures the whole desktop and then crops.
    A live frame is already in memory, so this is effectively free.

    Returns None (never an empty list) so the caller falls through to the
    proven Pillow path. Labels, ordering and primary-display marking still
    come from the existing _monitor_layout(), so the LEFT/RIGHT naming and
    the primary-on-top stacking behaviour are unchanged.
    """
    try:
        from . import screen_capture_service as _cap

        if not _cap.health().get("enabled"):
            return None
        count = _cap.monitor_count()
        if count <= 0:
            return None
        if max_age is None:
            max_age = float(os.getenv("FRIDAY_CAPTURE_MAX_AGE", "5"))
        layout = _monitor_layout()
        # DXGI enumeration order is not Win32's left-to-right order. Only
        # exact device name AND desktop geometry can label a cached image.
        # Unbound/ambiguous evidence uses the existing Pillow fallback.
        return _cap.labelled_frames(layout, max_age=max_age)
    except Exception as error:
        _LOGGER.debug("live regions unavailable: %s", type(error).__name__)
        return None


def _capture_regions():
    """One native-resolution image per monitor, labelled left-to-right.

    Prefers an already-captured live frame (see _live_regions) and otherwise
    falls back to the proven Pillow capture. Falls back to a single untagged
    region when the monitor layout cannot be enumerated, so this is always
    safe to call.
    """
    regions = []
    live = _live_regions()
    if live:
        return live
    layout = _monitor_layout()
    if not layout:
        try:
            return [{"label": "the screen", "image": _capture_screen(), "primary": True}]
        except Exception:
            return []
    for monitor in layout:
        left, top, width, height = monitor["bounds"]
        if width <= 0 or height <= 0:
            continue
        try:
            # all_screens=True is REQUIRED on Windows and was the reason the
            # LEFT monitor arrived as a solid black rectangle. Pillow sizes its
            # screen bitmap from GetDeviceCaps(HORZRES/VERTRES), which report
            # only the PRIMARY monitor - so any bbox reaching outside the
            # primary (i.e. a monitor sitting at NEGATIVE virtual coordinates,
            # like this machine's left display at -1920) came back all-black
            # WITHOUT raising. Verified: extrema (0,0) with all_screens off,
            # (0,255) with it on. Pillow also subtracts the virtual origin for
            # us, so negative coords need no extra maths
            # (python-pillow/Pillow#1547).
            #
            # _PerMonitorDPI is needed for the matching reason: the grab must
            # use the SAME coordinate space the layout was measured in, and
            # without it a 125%-scaled monitor is grabbed at the wrong size.
            with _PerMonitorDPI():
                image = ImageGrab.grab(
                    bbox=(left, top, left + width, top + height),
                    all_screens=True,
                )
            regions.append({
                "label": monitor["label"],
                "image": image,
                "primary": monitor["primary"],
            })
        except Exception:
            continue
    if not regions:
        try:
            return [{"label": "the screen", "image": _capture_screen(), "primary": True}]
        except Exception:
            return []
    return regions


# Vision encoders take the frame as a PIXEL budget, not a max side. Qwen2.5-VL
# resizes to multiples of 28 and honours min_pixels/max_pixels; its own docs
# recommend up to 2048*28*28 for OCR-grade accuracy and 1024*28*28 when
# memory-constrained. The old 640px CAP was ~115k pixels for this machine's
# 3840x1080 desktop, which is far below any useful reading size.
VISION_MAX_PIXELS = int(float(os.getenv("FRIDAY_VISION_MAX_PIXELS", str(1280 * 28 * 28))))
VISION_MAX_SIDE = int(float(os.getenv("FRIDAY_VISION_MAX_SIDE", "1920")))
_VISION_PATCH = 28


def _fit_for_vision(image):
    """Scale an image to the vision pixel budget, rounded to /28 patches."""
    try:
        from PIL import Image

        work = image
        width, height = work.size
        if width <= 0 or height <= 0:
            return work
        longest = max(width, height)
        if longest > VISION_MAX_SIDE:
            scale = VISION_MAX_SIDE / float(longest)
            work = work.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                               Image.LANCZOS)
            width, height = work.size
        pixels = width * height
        if pixels > VISION_MAX_PIXELS:
            scale = (VISION_MAX_PIXELS / float(pixels)) ** 0.5
            work = work.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                               Image.LANCZOS)
        return work
    except Exception:
        return image


def _encode_jpeg(image, quality=90):
    """High-quality JPEG. Text needs q85+; q75 at 640px destroyed it."""
    try:
        from PIL import Image

        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=quality)
        return out.getvalue()
    except Exception:
        return b""


def _downscale_jpeg(jpeg_bytes, max_side=None):
    """Kept for compatibility with older callers.

    The old default of 640px was the single biggest cause of "the AI is dumb
    about my screen": it produced a 640x180 frame from a 3840x1080 desktop, and
    OCR on that returned ZERO words. It now honours the pixel budget instead.
    """
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(jpeg_bytes))
        if max_side is not None:
            image.thumbnail((int(max_side), int(max_side)))
            fitted = image
        else:
            fitted = _fit_for_vision(image)
        out = io.BytesIO()
        fitted.convert("RGB").save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return jpeg_bytes


def _local_vision(prompt, jpeg_bytes, timeout=None):
    from .user_routes import selected
    if selected():
        # Cloud-enabled callers have already exhausted the complete ordered chain.
        if api_pools.cloud_vision_enabled():
            return False, None, "Configured Vision chain exhausted; no unchosen fallback."
        result = _configured_vision(jpeg_bytes, prompt)
        return result[0], result[1], result[3]
    """Local screen-description chain, bounded by ONE total budget so a slow
    GPU/CPU encode can never stall a voice turn.

    moondream gets its native captioning prompt (fast, ~2-4s, sees the dual
    monitor composite); the registered default vision model (qwen2.5vl:3b) is
    a deep-read fallback. Returns (ok, description, error)."""
    budget = timeout or LOCAL_VISION_TIMEOUT_SECONDS
    deadline = time.monotonic() + budget
    registered = ""
    try:
        registered = select_vision_model() or ""
    except Exception:
        registered = ""
    candidates = list(LOCAL_VISION_CHAIN)
    if registered and registered not in candidates:
        candidates.append(registered)
    last_error = ""
    for model in candidates:
        if _serving_disabled("vision", "vision_local", model, LOCAL_CHAT_URL):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        if "moondream" in model:
            chat_prompt = "Describe this image in detail."
            options = {"num_ctx": 512, "num_predict": 80, "temperature": 0}
        else:
            chat_prompt = prompt
            # num_ctx 2048 keeps qwen3 residency small (5.3GB): with the
            # default 16k it ballooned to 7.8GB and evicted moondream, so every
            # screen turn paid two cold loads (~55s) and the brain timed out.
            options = {"num_predict": 250, "temperature": 0, "num_ctx": 2048}
        with _identity_vision_attempt("vision_local", model, LOCAL_CHAT_URL) as observed_success:
            try:
                response = requests.post(
                    LOCAL_CHAT_URL,
                    json={
                        "model": model,
                        "keep_alive": "30m",
                        "stream": False,
                        "messages": [
                            {
                                "role": "user",
                                "content": chat_prompt,
                                "images": [base64.b64encode(jpeg_bytes).decode("ascii")],
                            }
                        ],
                        "options": options,
                    },
                    timeout=max(1.0, remaining),
                )
            except Exception as error:
                last_error = str(error)
                continue
            if response.status_code >= 400:
                last_error = f"{model}: HTTP {response.status_code}"
                continue
            text = (response.json().get("message", {}).get("content") or "").strip()
            if text:
                observed_success()
                return True, text, f"local/{model}"
            last_error = f"{model}: no description"
    return False, None, last_error or "local vision: no description"


def _wants_ocr(question):
    """True for literal transcription AND for "what is this thing DOING" asks.

    The second category used to skip OCR entirely, which is why a real app on
    screen was described as "a simple or minimalistic web page".
    """
    normalized = " ".join(str(question or "").lower().split()).rstrip(".!? ")
    if not normalized:
        return False
    return is_ocr_request(normalized) or any(
        phrase in normalized for phrase in OCR_INTENT_REQUESTS
    )


def _select_regions(question):
    """Pick the monitor(s) the question is actually about.

    "the left one" / "on my right" must resolve to a single screen instead of
    a composite where the answer is unreadable.
    """
    regions = _capture_regions()
    if not regions:
        return []
    if len(regions) == 1:
        return regions
    normalized = " ".join(str(question or "").lower().split())
    wants_left = "left" in normalized
    wants_right = "right" in normalized
    if wants_left and not wants_right:
        return [regions[0]]
    if wants_right and not wants_left:
        return [regions[-1]]
    # No side named: prefer the primary monitor (where the user works), and
    # include the others so a question about "my screens" still sees both.
    primary = [region for region in regions if region.get("primary")]
    ordered = (primary or [regions[0]]) + [
        region for region in regions if region not in (primary or [regions[0]])
    ]
    return ordered[:2]


def _visible_window_titles(limit=8):
    """Real window titles, so "what app is that?" has a factual anchor.

    `list_windows` returns Windows shell/taskbar entries with an EMPTY title,
    and they dominate the head of the list - an earlier version filtered them
    out only after slicing, so it returned [] and contributed nothing. Filter
    FIRST, and always lead with the genuinely focused window.
    """
    titles = []
    seen_titles = set()
    try:
        from .windows_control import list_windows

        # The active window is the single most useful fact, so put it first.
        try:
            active = get_active_window_context()
        except Exception:
            active = None
        if isinstance(active, dict):
            title = str(active.get("title") or "").strip()
            process = str(active.get("process") or "").strip()
            if title:
                entry = f"- {title}"
                if process and process.lower() not in title.lower():
                    entry += f" ({process})"
                titles.append(entry + "  [FOCUSED RIGHT NOW]")
                seen_titles.add(title.lower())

        for window in list_windows(limit=40) or []:
            if not isinstance(window, dict):
                continue
            title = str(window.get("title") or "").strip()
            process = str(window.get("process") or "").strip()
            # Shell/taskbar pseudo-windows carry no title at all.
            if not title or title in ("Program Manager", "Windows Input Experience"):
                continue
            # Dedupe on the TITLE, not the rendered line: the focused window
            # comes back from list_windows too, and keying on the full string
            # let it through a second time with a different process suffix.
            if title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            entry = f"- {title}"
            if process and process.lower() not in title.lower():
                entry += f" ({process})"
            titles.append(entry)
    except Exception:
        return titles[:limit]
    return titles[:limit]


def _mentioned_apps(question):
    """App names a question plausibly refers to. Bounded and best-effort."""
    try:
        from . import windows_control as _wc

        text = str(question or "").strip()
        if not text:
            return []
        out, seen = [], set()
        for entry in _wc.find_windows(text, limit=3):
            process = (entry.get("process") or "").lower()
            stem = process[:-4] if process.endswith(".exe") else process
            if stem and stem not in seen and stem != "explorer":
                seen.add(stem)
                out.append(stem)
        return out
    except Exception:
        return []


def _cheap_desktop_context(question):
    """Factual, pixel-free context: window state + UI Automation.

    2026-10-02. This answers, without any capture at all:
      "is OpenCode minimized"  -> layer 1, IsIconic via window state
      "what is Discord showing" -> layer 2, the app's accessibility tree

    Both are far cheaper than a vision round trip and far more trustworthy,
    because they are read rather than guessed. Bounded and optional by
    construction: a missing UIA backend, an unresolved app name or any
    exception yields "" and changes nothing else.
    """
    out = []
    apps = _mentioned_apps(question)
    try:
        from . import window_events as _we

        for app in apps[:3]:
            state = _we.window_state(app)
            if not state.get("found"):
                continue
            out.append(
                "Window state (read from Windows, no pixels): %r from %s is %s%s"
                % (state.get("title") or "", state.get("process") or "?",
                   "MINIMIZED" if state.get("minimized") else "on screen",
                   ", maximized" if state.get("maximized") else ""))
    except Exception as error:
        _LOGGER.debug("window context unavailable: %s", type(error).__name__)
    try:
        from . import ui_probe as _uip

        if _uip.available().get("available"):
            for app in apps[:2]:
                summary = _uip.summary_for_prompt(
                    _uip.inspect_window(app), max_chars=900)
                if summary and "UI Automation read of" in summary:
                    out.append(summary)
    except Exception as error:
        _LOGGER.debug("uia context unavailable: %s", type(error).__name__)
    return "\n".join(out)[:2000] if out else ""


_MEASUREMENT_CALLS = []


def _note_call(stage, provider, model, elapsed_ms, ok, **extra):
    """PHASE 5 (2026-10-03) MEASUREMENT ONLY - no behaviour change.

    Records one provider call made on the screen path so the number of vision /
    composer / local-fallback calls a real screen turn costs can be COUNTED
    rather than inferred. Before this there was no vision-call log line at all:
    `describe_screen` computed `vision_provider` and used it only to build a
    prose note, so the per-monitor multiplier was unmeasurable.

    Deliberately content-free: stage, provider, model name, duration, outcome.
    """
    entry = {
        "stage": stage,
        "provider": provider,
        "model": model,
        "elapsed_ms": int(elapsed_ms),
        "ok": bool(ok),
    }
    entry.update(extra)
    _MEASUREMENT_CALLS.append(entry)
    _LOGGER.info("screen call: stage=%s provider=%s model=%s elapsed_ms=%s ok=%s%s",
                 stage, provider, model, entry["elapsed_ms"], entry["ok"],
                 (" " + " ".join("%s=%s" % kv for kv in extra.items())) if extra else "")
    return entry


def take_call_ledger():
    """Drain the measurement ledger. The caller (server.py) logs it as one
    ordered line, which is what makes the SEQUENCE of a screen turn readable."""
    drained = list(_MEASUREMENT_CALLS)
    del _MEASUREMENT_CALLS[:]
    return drained


_SCREEN_EVIDENCE = {}


def take_screen_evidence():
    """Return and clear the structured facts about the screen just captured.

    Round M (2026-10-03). `describe_screen()` already knows everything the brain
    needs - which regions it read, whether OCR or vision produced text, how long
    the capture took - but all of it was flattened into one prose string and
    then discarded, so the brain re-derived the same facts through tools.

    This is the SAME hand-off pattern as `take_call_ledger()`, and it deliberately
    reuses the existing `server_state["last_screen_shot"]` structure on the
    server side rather than inventing a second screen memory.

    Returned keys:
        captured_at  epoch seconds the capture completed
        question     what was asked
        regions      human labels of the monitors actually read
        region_count how many were read
        ocr          whether the question asked for OCR
        ocr_chars    characters of verbatim OCR text obtained
        vision_calls how many cloud-vision calls were made
        vision_chars characters of vision description obtained
        vision_ok    whether any vision text came back
        window       active-window metadata at capture time
        report       the full grounded text handed to the brain
    """
    evidence = dict(_SCREEN_EVIDENCE)
    _SCREEN_EVIDENCE.clear()
    return evidence


def describe_screen(question=""):
    """Capture a fresh screen and return only data from this capture.

    OCR is authoritative for text AND for "what does this do" questions. Vision
    is optional and bounded; a timeout is reported instead of being turned into
    an affirmative claim. Each monitor is captured NATIVELY rather than as one
    downscaled composite, because a 3840x1080 composite shrunk to 640px is
    unreadable.
    """
    regions = _select_regions(question)
    if not regions:
        _SCREEN_EVIDENCE.clear()
        return "Screen capture failed, so I have no description of your screen."

    want_ocr = _wants_ocr(question)
    window_context = get_active_window_context()
    # Round M: per-region evidence tallies, so the brain can be told what was
    # actually read rather than being told a screen was "seen".
    evidence_regions = []
    evidence_ocr_chars = 0
    evidence_vision_chars = 0

    # 2026-10-02: cheapest context first. Window state and an app's
    # accessibility tree cost microseconds to a few hundred ms and NO pixels,
    # and both are checked against reality rather than inferred - which is the
    # whole point of putting them ahead of a vision round trip. It is factual
    # context for the brain only; it never replaces the capture below and
    # never turns into an action by itself.
    desktop_context = _cheap_desktop_context(question)

    parts = []
    for region in regions:
        image = region["image"]
        label = region["label"]
        section = []
        # Round M: bound before use. `text` is only assigned inside the
        # `want_ocr` branch, so without this the evidence tally below would
        # either NameError or silently read the PREVIOUS region's OCR text.
        text = ""
        vision_text = ""
        vision_provider = ""

        if want_ocr:
            # OCR runs on the NATIVE per-monitor image: ~1.2s and it is the
            # only thing that can actually read a UI.
            try:
                text = pytesseract.image_to_string(image).strip()
            except Exception:
                text = ""
            if text:
                section.append(f"Text visible on {label} (verbatim OCR):\n{text[:4000]}")
                evidence_ocr_chars += len(text)

        prompt_bits = []
        if label:
            prompt_bits.append(f"This image is {label} of a multi-monitor desktop.")
        if question:
            prompt_bits.append(str(question))
        else:
            prompt_bits.append(
                "Summarize the active application, critical errors, and key visible elements."
            )
        prompt_bits.append(
            "Read any visible text and name the actual application. If you cannot "
            "read the text, say so plainly instead of guessing."
        )
        prompt = ""
        if window_context and region.get("primary"):
            prompt = "Fresh active-window metadata: " + repr(window_context) + "\n"
        prompt += " ".join(prompt_bits)

        jpeg_bytes = _encode_jpeg(_fit_for_vision(image))
        vision_text = ""
        vision_error = ""
        vision_provider = ""
        if not want_ocr:
            # Sir's directive (2026-09-30): Modal is the PRIMARY vision, because
            # it is self-hosted - no public quota to exhaust. This code did the
            # OPPOSITE: it tried the local CPU model first and only reached
            # Modal as a fallback, so the primary vision was effectively never
            # used on a healthy local box. Measured on this machine, Modal is
            # also FASTER (4.9-7.4s) than the local qwen2.5vl:3b (~6s, and ~135s
            # when it is cold on this Ryzen 5 2600), so this is better on both
            # counts. Local stays as the offline fallback so FRIDAY still
            # describes the screen with zero keys and no internet.
            if api_pools.cloud_vision_enabled():
                _t0 = time.monotonic()
                ok, vision_text, vision_provider, vision_error = _cloud_vision(
                    jpeg_bytes, prompt
                )
                _note_call("vision", vision_provider or "cloud", _modal_vision_model(),
                          (time.monotonic() - _t0) * 1000, ok,
                          label=label, jpeg_bytes=len(jpeg_bytes or b""),
                          chars=len(vision_text or ""))
            if not vision_text:
                _t0 = time.monotonic()
                ok, vision_text, vision_error = _local_vision(prompt, jpeg_bytes)
                if ok and vision_text:
                    vision_provider = "local"
                _note_call("vision_local_fallback", "local", select_vision_model(),
                          (time.monotonic() - _t0) * 1000, ok, label=label,
                          chars=len(vision_text or ""))
            if not vision_text and not vision_error:
                vision_error = "vision returned no description"

        if vision_text:
            section.append(f"Visual description of {label}:\n{vision_text}")
            evidence_vision_chars += len(vision_text)
        if vision_error and not vision_text and not want_ocr:
            note = " Cloud vision was unavailable; text above is authoritative." if vision_provider else ""
            section.append(
                f"Visual analysis of this fresh capture of {label} was unavailable; "
                f"do not infer visual details." + note
            )
        if not section:
            section.append(f"No readable content detected on {label} in this fresh capture.")
        parts.append("\n".join(section))
        # Recorded last, once every value for this region is final.
        evidence_regions.append({
            "label": label,
            "vision_provider": vision_provider or "",
            "vision_chars": len(vision_text or ""),
            "ocr_chars": len(text or "") if want_ocr else 0,
        })

    if window_context:
        parts.append("Active window metadata:\n" + repr(window_context))
    titles = _visible_window_titles()
    if titles:
        parts.append(
            "Windows currently open (ground truth, prefer these names over guesses):\n"
            + "\n".join(titles)
        )
    if desktop_context:
        parts.append(desktop_context)
    parts.append(
        "Reminder: answer from the OCR text and window names above. If they do not "
        "identify something, say you cannot read it - never invent a purpose."
    )
    report = "\n\n".join(parts)[:SCREEN_RESPONSE_LIMIT]
    # Round M: publish what this capture actually produced, so the brain can be
    # grounded in it instead of re-capturing. Populated HERE, at the end, so a
    # failure anywhere above leaves the previous evidence untouched rather than
    # half-updated.
    _SCREEN_EVIDENCE.clear()
    _SCREEN_EVIDENCE.update({
        "captured_at": time.time(),
        "question": str(question or ""),
        "regions": [r.get("label") or "" for r in evidence_regions],
        "region_count": len(evidence_regions),
        "region_detail": evidence_regions,
        "ocr": bool(want_ocr),
        "ocr_chars": evidence_ocr_chars,
        "vision_calls": len([c for c in _MEASUREMENT_CALLS
                             if c.get("stage") == "vision"]),
        "vision_chars": evidence_vision_chars,
        "vision_ok": evidence_vision_chars > 0,
        "window": window_context or {},
        "report": report,
    })
    return report


_APP_HINTS = (
    ("youtube", "YouTube"),
    ("netflix", "Netflix"),
    ("prime video", "Prime Video"),
    ("spotify", "Spotify"),
    ("discord", "Discord"),
    ("telegram", "Telegram"),
    ("whatsapp", "WhatsApp"),
    ("github", "GitHub"),
    ("stackoverflow", "Stack Overflow"),
    ("gmail", "Gmail"),
    ("outlook", "Outlook"),
)
_PERSONA_TEMPLATES = (
    "Oh, you're on {app}, Sir — {detail}seems fun to me!",
    "Looks like {app} time, Sir — {detail}nice pick!",
    "I spy {app} on screen, Sir — {detail}carry on, I'm just admiring the taste.",
)
_CODE_PROCESS_HINTS = ("code", "vscode", "pycharm", "editor", "devenv", "opencode")


def _clean_window_title(title):
    """Return a short, spoken-friendly title (video/tab name only)."""
    title = str(title or "").replace("\u200b", " ").split(" - Profile")[0]
    title = title.split(" and ")[0].strip()
    for suffix in (" - YouTube", " - NETFLIX", " - Spotify", " - Discord"):
        if title.lower().endswith(suffix.lower()):
            title = title[: -len(suffix)].strip()
    return title[:120]


def _extract_block(report, header):
    """Return the body of a 'Header: ...' section, tolerating a longer header.

    Kept for compatibility, but note the real report labels are longer than the
    bare word: describe_screen writes "Visual description of {label}:" and
    "Text visible on {label} (verbatim OCR):". A caller that passes
    "Visual description:" and matches with startswith() therefore matches
    NOTHING - which is exactly what happened, silently, to every consumer.
    Prefer _vision_body() / _ocr_body().
    """
    for block in (report or "").split("\n\n"):
        if block.startswith(header):
            head, _, body = block.partition(":")
            return body.strip()
    return ""


# Every header line describe_screen can emit. Used to find where one section's
# body ends and the next one begins, even though a region's OCR and vision
# blocks are joined with a single "\n" (not a blank line).
_REPORT_HEADERS = (
    "Text visible on",
    "Visual description of",
    "Visual description:",
    "Visual analysis of this fresh capture",
    "No readable content detected on",
    "Active window metadata:",
    "Windows currently open",
    "Focused window:",
    "Reminder:",
    "OCR text:",
)

# Both the real per-monitor label ("Visual description of {label}:") and the
# bare "Visual description:" are accepted, because reports come from more than
# one producer and a mismatch here is SILENT - it just yields an empty string
# and a canned reply, which is how the brain went dead for so long.
VISION_HEADERS = ("Visual description of", "Visual description:")
OCR_HEADERS = ("Text visible on", "OCR text:")


def _section_body(report, prefixes):
    """Body of the first section whose header starts with one of `prefixes`.

    Line-based rather than blank-line-block-based, because within one monitor a
    region's OCR and vision sections are joined by a single newline, so the
    vision block is NOT at the start of a "\n\n" block.
    """
    lines = str(report or "").splitlines()
    for index, line in enumerate(lines):
        if not any(line.startswith(p) for p in prefixes):
            continue
        body = []
        for nxt in lines[index + 1:]:
            if any(nxt.startswith(h) for h in _REPORT_HEADERS):
                break
            body.append(nxt)
        text = "\n".join(body).strip()
        if text:
            return text
    return ""


def _vision_body(report):
    """The vision model's own description of the screen ('' if unavailable)."""
    return _section_body(report, VISION_HEADERS)


def _ocr_body(report):
    """Verbatim OCR text ('' if OCR produced nothing)."""
    return _section_body(report, OCR_HEADERS)


def _clean_sentence(text, limit=180):
    """Collapse the text into one spoken sentence if it reads like real prose
    (never raw noise), else ''. Collapses across lines so a vision description
    that line-wraps doesn't get clipped after its first line."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) < 12:
        return ""
    alpha = sum(c.isalpha() or c.isspace() for c in collapsed) / max(1, len(collapsed))
    if alpha < 0.75:
        return ""
    return collapsed[:limit]


def _trim_to_sentence(text, cap=700):
    """Never cut mid-word/mid-sentence: a spoken answer should end at
    punctuation. If over 'cap', cut at the last sentence boundary inside it;
    if none, hard-cut but give it a closing period."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= cap:
        return text
    head = text[:cap]
    ends = []
    for sep in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = head.rfind(sep)
        if idx > 0:
            ends.append(idx + 1)
    if ends and max(ends) > cap * 0.5:
        return head[:max(ends)].strip()
    last_space = head.rfind(" ")
    if last_space > cap * 0.6:
        return head[:last_space].rstrip()
    return head.rstrip(" ,;:-") + "."


# Vision models occasionally hand back useless filler ("this is a screenshot of
# a computer screen") — treat ONLY whole-sentence filler as 'no description'.
# Must NOT reject real captions that mention "a computer screen" in passing:
# that phrase shows up in nearly every genuine desktop description.
_GENERIC_VISION_STARTS = (
    "this is a screenshot",
    "here is a screenshot",
    "that is a screenshot",
    "this appears to be a screenshot",
    "a screenshot of",
    "i am unable",
    "i cannot",
    "i can't",
    "i'm not able",
    "i apologize",
    "unable to determine",
    "no meaningful",
    "the image is not clear",
)
_GENERIC_VISION_BANDS = (
    "black screen",
    "the screen is blank",
    "empty screen",
    "no content visible",
    "nothing meaningful",
    "no visible content",
)


def _is_generic_vision(low):
    """True only for whole-sentence filler; False for real descriptions."""
    if any(low.startswith(p) for p in _GENERIC_VISION_STARTS):
        return True
    if len(low) < 45 and any(m in low for m in _GENERIC_VISION_BANDS):
        return True
    return False


def _vision_answer(report):
    """The vision description as a spoken sentence, or '' if generic/absent."""
    sentence = _clean_sentence(_vision_body(report), limit=700)
    if not sentence:
        return ""
    if _is_generic_vision(sentence.lower()):
        return ""
    return _trim_to_sentence(sentence, cap=700)


def _ocr_answer(report):
    return _clean_sentence(_ocr_body(report))


def _screen_answer(report, window_context, question=""):
    """One personality-flavored sentence about what Sir is up to.

    Priority: a descriptive VISION sentence (what's actually rendered — the
    right answer for "what am I looking at"); else, for 'read the text'
    requests, the OCR text; else warm per-app templates; else the cleaned
    window title. The old behavior — always preferring the window
    title/process and discarding vision — is what made "Right now ...
    (opera.exe) has focus" sound robotic.
    """
    vision = _vision_answer(report)
    if vision:
        lead = "Ah, " + vision[0].lower() + vision[1:] if vision[0].isupper() else "Ah, " + vision
        return lead.rstrip()[:SCREEN_RESPONSE_LIMIT]
    if not isinstance(window_context, dict):
        # No window metadata: OCR is the best remaining signal.
        ocr = _ocr_answer(report)
        if is_ocr_request(question) and ocr:
            return f"Ah, I read this on your screen: {ocr}"[:SCREEN_RESPONSE_LIMIT]
        return (
            _clean_sentence(report)
            or "You're hard to read right now, Sir — the screen gave me nothing useful."
        )
    raw_title = str(window_context.get("title") or "")
    title = _clean_window_title(raw_title)
    process = str(window_context.get("process") or "")
    app = None
    for key, label in _APP_HINTS:
        if key in raw_title.lower():
            app = label
            break
    if app is None and any(h in process.lower() for h in _CODE_PROCESS_HINTS):
        app = "your code editor"
    if app is None:
        app = process.replace(".exe", "") or "your desktop"
    if is_ocr_request(question):
        ocr = _ocr_answer(report)
        if ocr:
            return f"Ah, I read this on your screen: {ocr}"[:SCREEN_RESPONSE_LIMIT]
        return "Ah, I couldn't make out any readable text on your screen, Sir."
    detail = ""
    if title and app in ("YouTube", "Netflix", "Prime Video", "Spotify"):
        detail = f"'{title[:70]}' — "
    if app in {label for _, label in _APP_HINTS} or app == "your code editor":
        try:
            import random

            template = random.choice(_PERSONA_TEMPLATES)
        except Exception:
            template = _PERSONA_TEMPLATES[0]
        return template.format(app=app, detail=detail)
    if title:
        return f"Ah — looks like you're on {title[:70]}, Sir."
    return f"Ah — looks like {app} has your attention, Sir."


def _brain_answer(question, report, window_context=None):
    from .user_routes import selected
    if selected():
        return ""  # Legacy auxiliary models are not authorized by the chosen chains.
    """Let the real brain (qwen3:8b, LOCAL) read the raw caption + the
    definitive active-window metadata and compose the FINAL line — the way a
    human assistant who can SEE the screens would report it: lead with what
    Sir is actively working on, interpret (never describe pixels), and only
    mention the second screen when it's notable. Bounded; on timeout/empty the
    canned reply falls back."""
    description = _vision_body(report)
    ocr = _ocr_body(report)
    if not description and not ocr:
        return ""
    evidence = (description or ocr)[:700]
    win = ""
    if isinstance(window_context, dict):
        title = str(window_context.get("title") or "")
        process = str(window_context.get("process") or "")
        if title or process:
            win = f" Definite active window (trust this): {repr(title or process or '')}."
    prompt = (
        "You are FRIDAY, Sir's personal assistant, and you are looking at his "
        "actual computer screens right now.\n"
        "Fresh visual description: " + evidence + win + "\n"
        "Sir asked: " + (question or "what am I looking at") + "\n\n"
        "Answer as FRIDAY, in YOUR voice, as if you truly see his screens. STRICT rules:\n"
        "- NEVER say 'the screenshot', 'the image/shows', 'computer screen', "
        "'left/right', 'sections', 'monitors', or list what is where.\n"
        "- LEAD with what he is DOING: 'You're currently working on ...' based "
        "on the active window and the content.\n"
        "- INTERPRET: pick the most important thing and say it usefully "
        "('...I can see how X connects with Y...').\n"
        "- Only if a second window is clearly notable, append ONE short "
        "clause: 'you also have ... open for this, looks good - I could improve "
        "it if you'd like.'\n"
        "- 1-3 SHORT sentences, complete (always end punctuation), ~30-50 words, "
        "summarize, never recite."
    )
    # Modal first (primary brain, ~2-3s), local second. The local qwen3:8b path
    # is kept as the offline fallback but is effectively unusable for a screen
    # turn: 49-92s measured on this machine, always over the timeout, so it
    # silently lost every race and the canned line was spoken instead.
    text = _brain_answer_modal(prompt)
    # PHASE 5 measurement: the composer runs on the same Modal endpoint as the
    # vision read and as the brain, so `_modal_vision_model()` names it correctly
    # (it applies FRIDAY_MODAL_VISION_MODEL / FRIDAY_MODAL_MODEL overrides).
    _note_call("composer", "modal", _modal_vision_model(), 0, bool(text),
               chars=len(text or ""))
    if not text and not _serving_disabled("brain", "local", "ollama_chat/" + SCREEN_BRAIN_MODEL, LOCAL_CHAT_URL):
        try:
            response = requests.post(
                LOCAL_CHAT_URL,
                json={
                    "model": SCREEN_BRAIN_MODEL,
                    # qwen3's hidden reasoning phase was meant to be disabled here.
                    # It is NOT: measured on this machine, "think": false still took
                    # 49-92s for this prompt (a two-word prompt is 0.4s), which is
                    # always over SCREEN_BRAIN_TIMEOUT_SECONDS - so this local call
                    # was being thrown away every time and the canned answer used
                    # instead. Modal answers the same prompt in ~2-3s, so the local
                    # model is now only reached when Modal is unavailable.
                    "think": False,
                    "keep_alive": "30m",
                    "stream": False,
                    "messages": neutral_messages(prompt),
                    "options": {"temperature": 0.5, "num_predict": 110, "num_ctx": 2048},
                },
                timeout=SCREEN_BRAIN_TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                return ""
            text = (response.json().get("message", {}).get("content") or "").strip()
        except Exception:
            return ""
    sentence = _clean_sentence(text, limit=900)
    if not sentence:
        return ""
    if _is_generic_vision(sentence.lower()):
        return ""
    low = sentence.lower()
    if low.startswith(("i cannot", "i can't", "i am unable", "i apologize")):
        return ""
    return _trim_to_sentence(sentence[:SCREEN_RESPONSE_LIMIT])


def _brain_answer_modal(prompt):
    from .user_routes import selected
    if selected():
        return ""  # Legacy auxiliary models are not authorized by the chosen chains.
    """Author the spoken screen line with Modal (self-hosted, ~2-3s).

    Modal is the primary brain by Sir's directive, and it is dramatically faster
    than the local qwen3:8b for this exact task (2-3s vs 49-92s measured). It is
    a TEXT call - the vision read has already happened - so there is no image to
    send and no token-budget trap here.
    """
    try:
        from .brain_router import MODAL_BASE

        base = (MODAL_BASE or "").rstrip("/")
    except Exception:
        base = ""
    if not base:
        return ""
    if not base.endswith("/v1"):
        base += "/v1"
    model = _modal_vision_model()
    if _serving_disabled("brain", "brain_modal", model, base):
        return ""
    pool = api_pools.pool("brain_modal")
    if pool is None or pool.empty:
        return ""
    try:
        key = pool.next_key()
    except Exception:
        return ""
    payload = {
        "model": model,
        "messages": neutral_messages(prompt),
        # Short spoken answer; leave room for the model's reasoning phase.
        "max_tokens": 1200,
        "temperature": 0.5,
    }
    if MODAL_VISION_REASONING_EFFORT:
        payload["reasoning_effort"] = MODAL_VISION_REASONING_EFFORT
    try:
        response = requests.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=SCREEN_BRAIN_TIMEOUT_SECONDS,
        )
    except Exception:
        return ""
    if getattr(response, "status_code", 500) >= 400:
        return ""
    try:
        content = response.json()["choices"][0]["message"].get("content") or ""
    except Exception:
        return ""
    return content.strip()


def screen_response(question=""):
    """Return a SHORT, personality-flavored answer for an explicit screen turn.

    Screenshot -> local vision (captions what's rendered on ALL monitors) ->
    the brain (qwen3:8b) reads that and composes the reply; the canned
    builder is the bounded fallback when the brain is busy. FRIDAY speaks this
    verbatim, so it must sound like a one-sentence reply with her signature
    warmth — never raw OCR telemetry or a data dump.
    """
    window_context = get_active_window_context()
    report = describe_screen(question)
    brain = ""
    if _vision_body(report) or _ocr_body(report):
        brain = _brain_answer(question, report, window_context)
    answer = brain or _screen_answer(report, window_context, question)
    answer = answer[:SCREEN_RESPONSE_LIMIT]
    # PHASE 5 (2026-10-03) MEASUREMENT ONLY. Logs what the screen turn actually
    # cost and where its answer came from. Nothing above this line changed.
    #
    # `source` distinguishes the composed answer from the canned builder, so a
    # turn whose composer ran but whose text was never used is visible as
    # `composer_used=False` rather than having to be inferred.
    vision_calls = [c for c in _MEASUREMENT_CALLS if c.get("stage") == "vision"]
    _note_call("screen_turn", "screen_pipeline", "", 0, True,
               source="composer" if brain else "canned_builder",
               composer_used=bool(brain), answer_chars=len(answer or ""),
               vision_calls=len(vision_calls))
    return answer


def _stack_regions_for_phone(regions):
    """Stack monitor captures VERTICALLY for a phone.

    Sir's complaint included "the screenshot it sent me was very low quality".
    The bytes were full resolution, but a 3840x1080 side-by-side composite is
    32:9, so on a phone it renders as a small letterboxed strip with tiny
    unreadable text. Stacking the monitors gives a ~1080x2160 portrait image
    that fills the screen and is actually legible. Thin separators and a label
    bar keep each monitor identifiable.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return regions[0]["image"] if regions else None
    if not regions:
        return None
    if len(regions) == 1:
        return regions[0]["image"]

    gap = 8
    width = max(region["image"].size[0] for region in regions)
    # PRIMARY FIRST (2026-10-01): Sir asked "why is the left one at the top?"
    # because the regions arrive in left-to-right order, so the display he is
    # actually working on - the PRIMARY one - was always buried at the BOTTOM,
    # below a long strip of the other monitor. On a phone the first screen is
    # what you see first, so the primary belongs there. Left-to-right order is
    # preserved among the rest.
    ordered = sorted(
        regions,
        key=lambda r: (0 if r.get("primary") else 1, 0),
    )
    regions = ordered

    # A legible label bar. The bar used to be 30px tall with PIL's DEFAULT
    # bitmap font, which renders around 11px - on a 1920px-wide composite the
    # words "the LEFT screen" came out as an unreadable smudge. Use a real
    # TrueType font at a size that scales with the image.
    scale = max(1.0, min(3.0, width / 1280.0))
    bar = int(max(34, 30 * scale))
    font = None
    try:
        from PIL import ImageFont

        for candidate in (
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ):
            try:
                font = ImageFont.truetype(candidate, int(20 * scale))
                break
            except Exception:
                continue
    except Exception:
        font = None

    height = sum(region["image"].size[1] + bar for region in regions) + gap * (len(regions) - 1)
    canvas = Image.new("RGB", (width, height), (16, 16, 16))
    draw = ImageDraw.Draw(canvas)
    top = 0
    for position, region in enumerate(regions):
        image = region["image"]
        label = region.get("label") or f"Screen {position + 1}"
        draw.rectangle([0, top, width, top + bar], fill=(32, 32, 32))
        try:
            draw.text(
                (10, top + (bar - (font.size if font else 14)) // 2 - 2),
                label,
                fill=(240, 240, 240),
                font=font,
            )
        except Exception:
            pass
        top += bar
        canvas.paste(image, (0, top))
        top += image.size[1]
        if position < len(regions) - 1:
            top += gap
    return canvas


def capture_screen_jpeg():
    """Fresh capture of every monitor -> JPEG bytes, for sending to Sir.

    Monitors are stacked vertically rather than laid out side-by-side: a
    3840x1080 composite is unreadable on a phone, and readability of the thing
    he actually receives was the complaint.
    """
    regions = _capture_regions()
    if not regions:
        return b""
    image = _stack_regions_for_phone(regions)
    if image is None:
        return b""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def _photo_caption_brain(question, report, window_context=None):
    from .user_routes import selected
    if selected():
        return ""  # Legacy auxiliary models are not authorized by the chosen chains.
    """The brain composes a grounded caption for a screen photo being handed
    to Sir (Telegram). Unlike _brain_answer (a spoken reply that must never
    mention the capture), THIS one may acknowledge that the image is right
    there beside the text — it IS the photo's caption.
    """
    description = _vision_body(report)
    ocr = _ocr_body(report)
    if not description and not ocr:
        return ""
    win = ""
    if isinstance(window_context, dict):
        title = str(window_context.get("title") or "")
        process = str(window_context.get("process") or "")
        if title or process:
            win = f" Definite active window (trust this): {repr(title or process or '')}."

    # EVIDENCE ORDER MATTERS, and getting it wrong is what produced a caption
    # about "a webpage in a serif font" while OpenCode was actually focused.
    # Measured on this machine: Tesseract read the screen correctly, while the
    # 3B vision model hallucinated a webpage. So the focused window and the
    # literal OCR text lead, and the vision description is explicitly marked as
    # the least reliable input.
    blocks = []
    if ocr:
        blocks.append("Text literally visible on screen (OCR, TRUST THIS):\n" + ocr[:600])
    if description:
        blocks.append(
            "A vision model's description of the layout (LOWER confidence, it may "
            "misread dense screens):\n" + description[:400]
        )
    evidence = "\n".join(blocks)
    if not evidence.strip():
        return ""

    prompt = (
        "You are FRIDAY, Sir's personal assistant. You just captured your own "
        "look at his computer screens, and this image is being sent to him as a "
        "photo, so it sits right there beside your words.\n"
        + win + "\nEvidence:\n" + evidence + "\n"
        "Sir asked: " + (question or "send me a screenshot") + "\n\n"
        "Write the message for him in YOUR voice. STRICT rules:\n"
        "- NAME the application from the focused window title, and say what the "
        "visible text shows he is doing. If they disagree, believe the window "
        "title and the OCR text.\n"
        "- Describe the focused task only. Do not inventory other open apps or "
        "announce that you can see 'an application on the desktop'. If the "
        "evidence identifies only the app, simply say 'Here is your current "
        "<app> window' and stop rather than guessing an activity.\n"
        "- Never say 'a screenshot of a computer screen', never mention monitors, "
        "'left'/'right'/'sections', fonts, or colours.\n"
        "- 1-2 SHORT sentences, ~20-40 words, warm, complete (always end punctuation)."
    )
    # A 20-40 word caption does not need a 5 GB model. qwen3:8b was measured
    # TIMING OUT past 45s here, which is what left the caption empty and caused
    # the canned acknowledgement. Use the small fast model for this job.
    for model, budget in ((SCREEN_CAPTION_MODEL, SCREEN_CAPTION_TIMEOUT_SECONDS),
                          (SCREEN_BRAIN_MODEL, SCREEN_BRAIN_TIMEOUT_SECONDS)):
        if _serving_disabled("brain", "local", "ollama_chat/" + model, LOCAL_CHAT_URL):
            continue
        try:
            response = requests.post(
                LOCAL_CHAT_URL,
                json={
                    "model": model,
                    "think": False,
                    "keep_alive": "30m",
                    "stream": False,
                    "messages": neutral_messages(prompt),
                    "options": {"temperature": 0.4, "num_predict": 80, "num_ctx": 2048},
                },
                timeout=budget,
            )
            if response.status_code >= 400:
                continue
            text = (response.json().get("message", {}).get("content") or "").strip()
        except Exception:
            continue
        sentence = _clean_sentence(text, limit=300)
        if not sentence or _is_generic_vision(sentence.lower()):
            continue
        return _trim_to_sentence(sentence[:300])
    return ""


def _looks_like_real_text(candidate):
    """Cheap plausibility gate for an OCR line before it is quoted to Sir.

    Tesseract on a real desktop returns plenty of plausible-looking garbage
    (measured: 'Ac (ERB SSEEEETD how nsnn-zo007\" +' from a Notepad window
    that plainly said 'Quarterly Budget Review'). Quoting that is worse than
    saying nothing, because it is confident and wrong. Real prose has several
    words, a sane vowel ratio, and few long consonant runs.
    """
    import re as _re

    text = " ".join(str(candidate or "").split())
    if not text:
        return False
    words = [w for w in _re.findall(r"[A-Za-z]{2,}", text)]
    if len(words) < 3:
        return False
    letters = [c.lower() for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    vowels = sum(1 for c in letters if c in "aeiou")
    ratio = vowels / float(len(letters))
    if not (0.22 <= ratio <= 0.68):
        return False
    # Reject strings dominated by one repeated character/pattern.
    if _re.search(r"(.)\1{4,}", text):
        return False
    # Tesseract's worst failures on dark UIs come out SHOUTING: 'Ac (ERB
    # SSEEEETD how nsnn-zo007'. Real prose is mixed case, and a couple of
    # long ALL-CAPS tokens is a strong garbage signal (one is fine - 'FRIDAY').
    caps_words = [
        w for w in words if len(w) >= 3 and w.isupper()
    ]
    if len(caps_words) >= 2:
        return False
    # Reject long consonant clusters that English rarely produces.
    if _re.search(r"[bcdfghjklmnpqrstvwxz]{4,}", text, _re.IGNORECASE):
        return False
    # Reject lines that are mostly punctuation/paths.
    if sum(1 for c in text if c.isalnum() or c.isspace()) / len(text) < 0.7:
        return False
    return True


def _clean_ocr_snippet(ocr_text, limit=170):
    """Readable, non-noisy slice of OCR for a caption.

    The stacked capture draws a label bar per monitor, so OCR picks up FRIDAY's
    OWN label text. Tesseract renders that bar at a small font and MERGES the
    words ("the LEFTscreen", "PRIMARYdisplay"), so the pattern must tolerate a
    missing space - the first version used `\\s+` and therefore kept the very
    lines it was meant to drop.

    Selection is by usefulness, not length: `max(len)` picked the longest line
    on the screen, which on a developer machine is a shell command, not the
    thing worth captioning. Prefer an early, headline-shaped line.
    """
    import re as _re

    drop = _re.compile(
        r"the\s*(?:only|left|right|middle)?\s*screen\b|primary\s*display|"
        r"^\s*screen\s*\d*\s*$",
        _re.IGNORECASE,
    )
    # Lines that are really paths, shell commands or code - useful on screen but
    # useless as a caption about what Sir is doing.
    noisy = _re.compile(r"[A-Za-z]:\\|\\\\|\b(?:py_compile|pytest|import |def |curl -|pip )\b")
    candidates = []
    for line in str(ocr_text or "").splitlines():
        stripped = " ".join(line.split())
        if not stripped or drop.search(stripped) or noisy.search(stripped):
            continue
        letters = sum(1 for ch in stripped if ch.isalnum())
        if letters < max(6, len(stripped) * 0.5):
            continue
        candidates.append(stripped)
    if not candidates:
        return ""
    headline = [
        line for line in candidates
        if 12 <= len(line) <= 90 and sum(1 for ch in line if ch.isalnum()) >= len(line) * 0.6
    ]
    # Only quote a line that passes the plausibility gate; prefer the earliest
    # good one, since the top of a window is its title.
    pool = headline or candidates
    for line in pool:
        if _looks_like_real_text(line):
            return _re.sub(r"\s+", " ", line).strip(" -—|:;,")[:limit]
    return ""



def _grounded_photo_caption(ocr_text, window_context=None, titles=None):
    """Deterministic-but-GROUNDED caption, used only when the brain cannot
    answer inside its budget.

    This is NOT a canned acknowledgement: every word comes from the actual
    capture (the focused window title and the literal OCR text). It exists
    because the alternative measured on this machine was a 45s timeout followed
    by a canned string, and Sir's own policy forbids canned screen replies.
    """
    title = ""
    if isinstance(window_context, dict):
        title = str(window_context.get("title") or "").strip()
    if not title and titles:
        first = str(titles[0]).lstrip("- ").split("  [")[0]
        title = first.split(" (")[0].strip()
    snippet = _clean_ocr_snippet(ocr_text)
    # The capture spans every monitor, so the readable text can come from any
    # of them. Say "your screens" rather than implying a single display - being
    # precise about what was actually captured is the whole point here.
    if title and snippet:
        return _trim_to_sentence(f"You're in {title} — and I can read “{snippet}” on your screens, Sir.")
    if title:
        return f"You're in {title}, Sir — that's what I've got."
    if snippet:
        return f"Straight off your screens, Sir: {snippet}."
    return ""


def author_screen_photo(jpeg_bytes, question=""):
    """AI-AUTHORED caption for a fresh screen photo being handed to Sir.

    The capture is only an infrastructure primitive; the CONTENT is authored
    here and grounded in the real capture. Returns ``(caption, vision)``.
    ``vision`` is persisted for follow-up turns so "do you see your own
    screenshot?" is answered with real context, never "I don't see a
    screenshot". Both are "" only when nothing at all could be read.

    ORDERING IS EVIDENCE-BASED (measured on this machine, 2026-09-30):
      * Tesseract read the screen CORRECTLY and fast (~1.5s).
      * The 3B vision model read the SAME screen WRONG (invented "a webpage in
        a serif font" while OpenCode was focused) and took ~17.6s.
      * qwen3:8b did not answer the caption prompt inside 45s, which is what
        left the caption empty and caused the canned acknowledgement.
    So the caption is built from the two reliable facts - the focused window
    title and the literal OCR text - and the vision description is only
    consulted when enabled, because on dense desktop screenshots it is the
    least reliable input available.
    """
    window_context = get_active_window_context()
    titles = _visible_window_titles(limit=4)
    ocr_text = _ocr_of_image(jpeg_bytes)

    vision = ""
    if SCREEN_CAPTION_USE_VISION:
        try:
            vision = describe_image_bytes(jpeg_bytes, question)
        except Exception:
            vision = ""
        if vision.startswith("I could not analyze"):
            vision = ""
    vision_usable = bool(vision)

    if not ocr_text and not titles and not vision_usable:
        return "", ""

    report = ""
    if ocr_text:
        report += "OCR text: " + ocr_text + "\n"
    if titles:
        report += "Windows open: " + "; ".join(titles) + "\n"
    if vision_usable:
        report += "Visual description: " + vision.strip() + "\n"

    # The reliable, grounded base line - always available in ~2s.
    grounded = _grounded_photo_caption(ocr_text, window_context, titles)

    # Let the brain try to make the line warmer, but only ACCEPT its version if
    # it is grounded - it must not have drifted into generic vision filler, and
    # when we know the focused app it has to actually mention it. Measured: a
    # 0.5B model handed correct evidence still replied "a webpage in a serif
    # font", so unverified acceptance is how a wrong caption ships.
    caption = ""
    try:
        caption = _photo_caption_brain(question, report, window_context) or ""
    except Exception:
        caption = ""
    focused_title = ""
    if isinstance(window_context, dict):
        focused_title = str(window_context.get("title") or "").strip()
    if caption:
        low = caption.lower()
        if _is_generic_vision(low) or "screenshot" in low or "webpage" in low:
            caption = ""
        elif focused_title and len(focused_title) > 3:
            head = focused_title.split()[0].lower()
            if head not in low:
                caption = ""
    if not caption:
        caption = grounded
    if not caption:
        return "", ""
    persisted = vision.strip() if vision_usable else ""
    if not persisted:
        # Persist a CLEAN, useful context blob. Storing raw OCR shipped
        # FRIDAY's own monitor labels ("the LEFTscreen", "PRIMARY display")
        # into the follow-up context, where the brain then quoted them back.
        snippet = _clean_ocr_snippet(ocr_text, limit=400)
        focused = ""
        if isinstance(window_context, dict):
            focused = str(window_context.get("title") or "").strip()
        parts = []
        if focused:
            parts.append(f"Focused window: {focused}")
        if snippet:
            parts.append("Visible text: " + snippet)
        if titles:
            parts.append("Windows open: " + "; ".join(titles[:4]))
        persisted = "\n".join(parts)
    if not persisted:
        return "", ""
    return _trim_to_sentence(caption[:1024]), persisted


def describe_image_bytes(jpeg_bytes, question=""):
    """Vision for arbitrary JPEG bytes (Telegram photos).

    ORDER IS POLICY, NOT PREFERENCE (2026-10-18):
        1. the cloud chain, Modal first  (`_cloud_vision`)
        2. local qwen2.5vl, offline fallback only
        3. an honest "vision chain is unavailable" line

    This used to run local FIRST, which contradicted the documented Modal-first
    vision policy, the runtime prompt, and the self-report - Sir's Telegram
    photos were being answered by the CPU model while the designated primary sat
    unused, and the local model is both slower (~6s vs 2.4-3.5s) and weaker here.
    Local stays in the chain so FRIDAY can still describe a photo with zero keys,
    but it must never be the one picked while the cloud is reachable.

    The image is fitted to the vision pixel budget FIRST. It used to be passed
    through untouched, and a full 3840x1080 frame crashed the local encoder -
    the project's own _downscale_jpeg comment recorded that "full-res frames
    crashed qwen2.5vl's Vulkan encode on the RX 580". That silent failure is
    why a screen photo could come back with NO description, which left the
    caption empty and produced a canned acknowledgement - a policy violation
    on top of a capability gap.
    """
    prompt = (question or "").strip() or "What is in this image? Answer in one warm sentence."
    try:
        from PIL import Image

        fitted = _fit_for_vision(Image.open(io.BytesIO(jpeg_bytes)))
        buffer = io.BytesIO()
        fitted.convert("RGB").save(buffer, format="JPEG", quality=88)
        payload = buffer.getvalue()
    except Exception:
        payload = jpeg_bytes

    # 1) Canonical chain: Modal first, then Gemini, then the rest of the cloud.
    #    Skipped entirely when no cloud key is configured, so a keyless install
    #    falls straight through to the local model instead of paying timeouts.
    if api_pools.cloud_vision_enabled():
        ok, text, provider, _error = _cloud_vision(payload, prompt)
        if ok and text:
            return f"[{provider}] {text}"

    # 2) Offline fallback: reached ONLY when the cloud chain was disabled or
    #    every cloud stage failed. Never reached on a Modal success.
    ok, text, _error = _local_vision(prompt, payload)
    if ok and text:
        return f"[local] {text}"

    # 3) Nothing worked. Say so - an empty answer here previously produced a
    #    canned acknowledgement, which is exactly what the screen policy forbids.
    return "I could not analyze that image right now, Sir - my vision chain is unavailable."


def _history_readable_image(monitor_index, age_seconds):
    """(age, PIL image) from memory-only history, or None. Never raises."""
    try:
        from . import screen_capture_service as _cap

        return _cap.history_image(monitor_index, age_seconds)
    except Exception as error:
        _LOGGER.debug("screen history image unavailable: %s",
                      type(error).__name__)
        return None


def describe_recent_screen(age_seconds=None, question="", monitor_index=0,
                           mode="ocr", limit=1200):
    """Read the screen as it was `age_seconds` ago. ON DEMAND ONLY.

    This is deliberately not wired into the normal screen pipeline. It costs an
    OCR or a vision round trip, so it runs only when the model has decided a
    past moment actually matters - never speculatively, never per capture.

    The answer always carries the frame's real age, because the single most
    dangerous failure mode here is answering "yes, that error was there" about
    something that has since been fixed. A moment outside the retained window
    returns ok=False rather than the current screen.
    """
    result = {
        "ok": False,
        "age_seconds": None,
        "as_of": None,
        "text": "",
        "source": mode,
        "certain": False,
        "reason": "",
    }
    try:
        from . import screen_capture_service as _cap

        if not _cap.history_enabled():
            result["reason"] = "screen history is disabled"
            result["text"] = _cap.HISTORY_UNAVAILABLE
            return result
        found = _history_readable_image(monitor_index, age_seconds)
        if found is None:
            result["reason"] = (
                "no retained frame for that moment on monitor %s" % monitor_index)
            result["text"] = _cap.HISTORY_UNAVAILABLE
            return result
        age, image = found
        result["age_seconds"] = round(float(age), 2)
        result["as_of"] = time.time() - float(age)
        jpeg = _encode_jpeg(image)
        if not jpeg:
            result["reason"] = "could not encode the retained frame"
            result["text"] = _cap.HISTORY_UNAVAILABLE
            return result
        if str(mode or "ocr").lower() == "vision":
            prompt = question or (
                "Describe what was on this screen, focusing on text, errors "
                "and window titles. Be specific and do not guess.")
            # _cloud_vision returns (ok, payload, provider, error); every other
            # call site unpacks it (L1350, L2275). This one assigned the WHOLE
            # TUPLE to `text`, so `text.strip()` below raised AttributeError,
            # the blanket except swallowed it, and mode="vision" history reads
            # always came back ok=False. OCR mode and the HTTP route were fine.
            ok, vision_text, _provider, vision_error = _cloud_vision(jpeg, prompt)
            if not ok or not str(vision_text or "").strip():
                result["reason"] = vision_error or "vision returned nothing"
                result["text"] = (
                    "I have that frame from %.1fs ago, but my vision chain "
                    "could not read it%s." % (
                        result["age_seconds"],
                        " (%s)" % vision_error if vision_error else ""))
                return result
            text = str(vision_text)
        else:
            text = _ocr_of_image(jpeg, limit=limit)
        if not text:
            result["reason"] = "nothing legible in that frame"
            result["text"] = (
                "I can see that frame from %.1fs ago, Sir, but there is no "
                "legible text in it." % result["age_seconds"])
            return result
        result["text"] = text.strip()
        result["ok"] = True
        # OCR of a downscaled frame can invent characters. Callers are told to
        # hedge unless they have other reason to be confident.
        result["certain"] = (result["source"] == "ocr")
        return result
    except Exception as error:
        _LOGGER.debug("describe_recent_screen failed: %s", type(error).__name__)
        result["reason"] = "%s: %s" % (type(error).__name__, str(error)[:120])
        return result


def _ocr_of_image(jpeg_bytes, limit=2500):
    """Best-effort OCR on raw screenshot bytes. Never raises."""
    try:
        from PIL import Image

        text = pytesseract.image_to_string(
            Image.open(io.BytesIO(jpeg_bytes))
        ).strip()
        return text[:limit]
    except Exception:
        # NOTE: a `return` used to follow this try/except and was unreachable.
        # If OCR fails we return "" so the caller reports "nothing legible"
        # honestly instead of promising text it does not have.
        return ""
