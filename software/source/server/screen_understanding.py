"""Small, on-demand screen description bridge for the voice server."""

import base64
import io
import os

import requests
from PIL import ImageGrab
import pytesseract

from .windows_context import get_active_window_context
from .model_registry import MODELS, VISION, select_model
from . import api_pools


SCREEN_REQUESTS = (
    "take a look at my screen",
    "look at my screen",
    "what am i looking at",
    "what am i looking at right now",
    "what is on my screen",
    "what's on my screen",
    "read my screen",
    "describe my screen",
    "take a screenshot",
    "take a screenshot of my screen",
    "screenshot my screen",
    "what do you think i'm doing",
    "what do you think im doing",
    "what do you think i am doing",
    "what am i doing",
    "what am i doing right now",
    "catch a look at my screen",
    "glance at my screen",
    "see my screen",
)

OCR_REQUESTS = (
    "read my screen",
    "read the text on my screen",
    "what text is on my screen",
    "what does my screen say",
)

VISION_TIMEOUT_SECONDS = float(os.getenv("FRIDAY_VISION_TIMEOUT", "25"))
SCREEN_RESPONSE_LIMIT = 1800

GEMINI_BASE = os.environ.get(
    "FRIDAY_GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
OPENROUTER_BASE = os.environ.get("FRIDAY_OPENROUTER_BASE", "https://openrouter.ai/api/v1")
LOCAL_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434/api/chat")
GEMINI_VISION_MODEL = os.environ.get("FRIDAY_GEMINI_VISION_MODEL", "gemini-3.1-flash-lite")
OPENROUTER_VISION_MODEL = os.environ.get(
    "FRIDAY_OPENROUTER_VISION_MODEL", "meta-llama/llama-3.2-11b-vision-instruct:free"
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


def is_screen_request(text):
    normalized = " ".join(text.lower().split()).rstrip(".!? ")
    return any(phrase in normalized for phrase in SCREEN_REQUESTS)


def is_ocr_request(text):
    normalized = " ".join(text.lower().split()).rstrip(".!? ")
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


def _openai_vision_payload(jpeg_bytes, prompt, model):
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")
    return {
        "model": model,
        "max_tokens": 300,
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


def _gemini_vision(key, jpeg_bytes, prompt, model):
    headers = {"Authorization": f"Bearer {key}"}
    try:
        response = requests.post(
            f"{GEMINI_BASE}/chat/completions",
            headers=headers,
            json=_openai_vision_payload(jpeg_bytes, prompt, model),
            timeout=VISION_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        return False, None, f"gemini vision request error: {api_pools.sanitize(error)}", False
    if response.status_code != 200:
        return False, None, f"gemini vision http {response.status_code}", _vision_quota_failure(response.status_code)
    try:
        content = (response.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
    except (ValueError, IndexError, TypeError, AttributeError):
        return False, None, "gemini vision bad payload", False
    text = "".join(part.get("text", "") for part in content) if isinstance(content, list) else str(content)
    text = (text or "").strip()
    if not text:
        return False, None, "gemini vision returned no description", False
    return True, text, "", False


def _openrouter_vision(key, jpeg_bytes, prompt):
    headers = {"Authorization": f"Bearer {key}"}
    try:
        response = requests.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers=headers,
            json=_openai_vision_payload(jpeg_bytes, prompt, OPENROUTER_VISION_MODEL),
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
    text = "".join(part.get("text", "") for part in content) if isinstance(content, list) else str(content)
    text = (text or "").strip()
    if not text:
        return False, None, "openrouter vision returned no description", False
    return True, text, "", False


def _call_vision(key, pool_name, jpeg_bytes, prompt, model=None):
    if pool_name == "vision_gemini":
        return _gemini_vision(key, jpeg_bytes, prompt, model or GEMINI_VISION_MODEL)
    if pool_name == "vision_openrouter":
        return _openrouter_vision(key, jpeg_bytes, prompt)
    return False, None, f"unknown vision pool {pool_name}", False


def _cloud_vision(jpeg_bytes, prompt):
    last_error = ""
    # Gemini stage first: walk the per-model chain on the dedicated account
    # (each model has its own quota bucket; model-banned entries are skipped).
    gemini_pool = api_pools.pool("vision_gemini")
    if gemini_pool is not None and not gemini_pool.empty:
        key = gemini_pool.next_key()
        if key:
            for model in VISION_GEMINI_CHAIN:
                if api_pools.model_banned("vision_gemini", model):
                    continue
                try:
                    ok, payload, error, quota_failure = _gemini_vision(key, jpeg_bytes, prompt, model)
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
    ok, payload, provider, error = api_pools.run_chain(
        (VISION_OPENROUTER_POOL,),
        lambda key, pool_name: _call_vision(key, pool_name, jpeg_bytes, prompt),
    )
    if ok:
        return True, payload, provider, ""
    return False, None, provider or "", error or last_error


def _local_vision(prompt, vision_model, jpeg_bytes):
    payload = {
        "model": vision_model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(jpeg_bytes).decode("ascii")],
            }
        ],
    }
    try:
        response = requests.post(LOCAL_CHAT_URL, json=payload, timeout=VISION_TIMEOUT_SECONDS)
        response.raise_for_status()
        text = response.json().get("message", {}).get("content", "").strip()
        return (True, text, "") if text else (False, None, "local vision returned no description")
    except Exception as error:
        return False, None, str(error)


def describe_screen(question=""):
    """Capture a fresh screen and return only data from this capture.

    OCR is authoritative for text requests. Vision is optional and bounded; a
    timeout is reported instead of being turned into an affirmative claim.
    """
    image = ImageGrab.grab()
    ocr_text = pytesseract.image_to_string(image).strip()
    window_context = get_active_window_context()
    window_hint = ""
    if window_context:
        window_hint = (
            "Fresh active-window metadata: "
            + repr(window_context)
            + "\n"
        )

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=75)
    prompt = window_hint + (question or "Summarize the active application, critical errors, and key visible elements. Be extremely concise. Avoid boilerplate.")
    vision_model = select_vision_model()
    model_info = MODELS.get(vision_model)
    jpeg_bytes = buffer.getvalue()

    vision_text = ""
    vision_error = ""
    vision_provider = ""
    if not is_ocr_request(question):
        if api_pools.cloud_vision_enabled():
            ok, vision_text, vision_provider, vision_error = _cloud_vision(jpeg_bytes, prompt)
            if ok and vision_text:
                print(f"[screen vision ok: {vision_provider}]", flush=True)
        if not vision_text:
            ok, vision_text, vision_error = _local_vision(prompt, vision_model, jpeg_bytes)
            if ok and vision_text:
                print(f"[screen vision ok: local {vision_model}]", flush=True)
        if not vision_text and not vision_error:
            vision_error = "vision returned no description"

    parts = []
    if vision_text:
        parts.append("Visual description:\n" + vision_text)
    if ocr_text:
        parts.append("OCR text:\n" + ocr_text)
    if window_context:
        parts.append("Active window metadata:\n" + repr(window_context))
    if vision_error and not vision_text:
        timeout_note = ""
        if vision_provider:
            timeout_note = " Cloud vision was unavailable; OCR below is the authoritative fallback."
        elif model_info and model_info.known_timeout:
            timeout_note = " The selected vision model has a known timeout on this machine; OCR is the bounded fallback."
        parts.append("Visual analysis unavailable for this fresh capture; do not infer visual details." + timeout_note)
    if not parts:
        return "Fresh screen capture completed, but it contained no readable text and visual analysis was unavailable."
    return "\n\n".join(parts)[:SCREEN_RESPONSE_LIMIT]


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


def _screen_answer(vision_text, window_context):
    """One personality-flavored sentence about what Sir is up to.

    Window metadata is the most reliable signal; vision and OCR text are
    only used when the window title gives no obvious activity (and only if
    they read like real sentences — never raw OCR noise).
    """
    if isinstance(window_context, dict):
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
        try:
            import random
            template = random.choice(_PERSONA_TEMPLATES)
        except Exception:
            template = _PERSONA_TEMPLATES[0]
        detail = ""
        if title and app in ("YouTube", "Netflix", "Prime Video", "Spotify"):
            detail = f"'{title[:70]}' — "
        return template.format(app=app, detail=detail)
    # No window metadata: first that reads like a real sentence from vision/OCR.
    for line in (vision_text or "").splitlines():
        line = line.strip()
        if len(line) >= 12:
            alpha_ratio = sum(c.isalpha() or c.isspace() for c in line) / max(1, len(line))
            if alpha_ratio >= 0.75:
                return line[:180]
    return "You're hard to read right now, Sir — the screen gave me nothing useful."


def screen_response(question=""):
    """Return a SHORT, personality-flavored answer for an explicit screen turn.

    FRIDAY speaks this verbatim, so it must sound like a one-sentence reply
    with her signature warmth — never raw OCR telemetry or a data dump.
    """
    window_context = get_active_window_context()
    report = describe_screen(question)
    return _screen_answer(report, window_context)[:SCREEN_RESPONSE_LIMIT]


def capture_screen_jpeg():
    """Fresh full-screen capture → JPEG bytes (for Telegram send-back)."""
    import io as _io

    image = ImageGrab.grab()
    buffer = _io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def describe_image_bytes(jpeg_bytes, question=""):
    """Vision for arbitrary JPEG bytes (Telegram photos): SAME chain as the
    screen system — vision_gemini model chain → vision_openrouter → local."""
    prompt = (question or "").strip() or "What is in this image? Answer in one warm sentence."
    if api_pools.cloud_vision_enabled():
        ok, text, provider, _error = _cloud_vision(jpeg_bytes, prompt)
        if ok and text:
            return f"[{provider}] {text}"
    ok, text, _error = _local_vision(prompt, select_vision_model(), jpeg_bytes)
    if ok and text:
        return text
    return "I could not analyze that image right now, Sir — my vision chain is unavailable."
