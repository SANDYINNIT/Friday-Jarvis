"""Small, on-demand screen description bridge for the voice server."""

import base64
import io
import os
import time

import requests
from PIL import ImageGrab
import pytesseract

from .windows_context import get_active_window_context
from .model_registry import VISION, select_model
from . import api_pools


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
LOCAL_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434/api/chat")
GEMINI_VISION_MODEL = os.environ.get("FRIDAY_GEMINI_VISION_MODEL", "gemini-3.1-flash-lite")
OPENROUTER_VISION_MODEL = os.environ.get(
    "FRIDAY_OPENROUTER_VISION_MODEL", "inclusionai/ling-3.0-flash-vl:free"
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
# Short budget: the brain only REFINES the local caption. qwen3:8b on Sir's
# CPU is ~10-20 tok/s warm, so a ~50-word spoken line lands in ~3-6s; keep_alive
# 30m pins qwen3 + moondream resident so a cold 5.2GB reload (30-45s) only
# happens after a long idle. 30s covers even the reload edge once.
SCREEN_BRAIN_TIMEOUT_SECONDS = float(os.getenv("FRIDAY_SCREEN_BRAIN_TIMEOUT", "30"))


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
    headers = {"Authorization": f"Bearer {key}"}
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
    text = _usable_vision_text(
        content,
    )
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
    # Gemini stage: ONE short trial on the strongest available model. Trying
    # the full 5-model chain at the 25s timeout each was stalling screen turns
    # ~60-125s whenever the free-tier Gemini endpoint just hung — then the
    # title fallback answered. (SEARCHES.md / MEMORY.md 2026-09-23.)
    gemini_pool = api_pools.pool("vision_gemini")
    if gemini_pool is not None and not gemini_pool.empty:
        key = gemini_pool.next_key()
        model = next(
            (m for m in VISION_GEMINI_CHAIN if not api_pools.model_banned("vision_gemini", m)),
            None,
        )
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
                })
            return True

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


def _capture_regions():
    """One native-resolution image per monitor, labelled left-to-right.

    Falls back to a single untagged region when the monitor layout cannot be
    enumerated, so this is always safe to call.
    """
    regions = []
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
            image = ImageGrab.grab(bbox=(left, top, left + width, top + height))
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
    """Real window titles, so "what app is that?" has a factual anchor."""
    try:
        from .windows_control import list_windows

        titles = []
        for window in list_windows(limit=limit) or []:
            if not isinstance(window, dict):
                continue
            title = str(window.get("title") or "").strip()
            process = str(window.get("process") or "").strip()
            if not title:
                continue
            entry = f"- {title}"
            if process and process.lower() not in title.lower():
                entry += f" ({process})"
            if entry not in titles:
                titles.append(entry)
        return titles[:limit]
    except Exception:
        return []


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
        return "Screen capture failed, so I have no description of your screen."

    want_ocr = _wants_ocr(question)
    window_context = get_active_window_context()

    parts = []
    for region in regions:
        image = region["image"]
        label = region["label"]
        section = []

        if want_ocr:
            # OCR runs on the NATIVE per-monitor image: ~1.2s and it is the
            # only thing that can actually read a UI.
            try:
                text = pytesseract.image_to_string(image).strip()
            except Exception:
                text = ""
            if text:
                section.append(f"Text visible on {label} (verbatim OCR):\n{text[:4000]}")

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
            ok, vision_text, vision_error = _local_vision(prompt, jpeg_bytes)
            if ok and vision_text:
                vision_provider = "local"
            if not vision_text and api_pools.cloud_vision_enabled():
                ok, vision_text, vision_provider, vision_error = _cloud_vision(
                    jpeg_bytes, prompt
                )
            if not vision_text and not vision_error:
                vision_error = "vision returned no description"

        if vision_text:
            section.append(f"Visual description of {label}:\n{vision_text}")
        if vision_error and not vision_text and not want_ocr:
            note = " Cloud vision was unavailable; text above is authoritative." if vision_provider else ""
            section.append(
                f"Visual analysis of this fresh capture of {label} was unavailable; "
                f"do not infer visual details." + note
            )
        if not section:
            section.append(f"No readable content detected on {label} in this fresh capture.")
        parts.append("\n".join(section))

    if window_context:
        parts.append("Active window metadata:\n" + repr(window_context))
    titles = _visible_window_titles()
    if titles:
        parts.append(
            "Windows currently open (ground truth, prefer these names over guesses):\n"
            + "\n".join(titles)
        )
    parts.append(
        "Reminder: answer from the OCR text and window names above. If they do not "
        "identify something, say you cannot read it - never invent a purpose."
    )
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


def _extract_block(report, header):
    """Return the body of a '\n\n' separated 'Header:\n...' section."""
    for block in (report or "").split("\n\n"):
        if block.startswith(header):
            return block.split(":", 1)[1].strip()
    return ""


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
    sentence = _clean_sentence(_extract_block(report, "Visual description:"), limit=700)
    if not sentence:
        return ""
    if _is_generic_vision(sentence.lower()):
        return ""
    return _trim_to_sentence(sentence, cap=700)


def _ocr_answer(report):
    return _clean_sentence(_extract_block(report, "OCR text:"))


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
    """Let the real brain (qwen3:8b, LOCAL) read the raw caption + the
    definitive active-window metadata and compose the FINAL line — the way a
    human assistant who can SEE the screens would report it: lead with what
    Sir is actively working on, interpret (never describe pixels), and only
    mention the second screen when it's notable. Bounded; on timeout/empty the
    canned reply falls back."""
    description = _extract_block(report, "Visual description:")
    ocr = _extract_block(report, "OCR text:")
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
    try:
        response = requests.post(
            LOCAL_CHAT_URL,
            json={
                "model": SCREEN_BRAIN_MODEL,
                # qwen3's hidden reasoning phase (think ON by default) costs
                # 30-120s and caps below num_predict — the top-level "think":
                # False flag turns it off (verified: 0.7s vs 54s + empty reply).
                "think": False,
                "keep_alive": "30m",
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
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
    if _extract_block(report, "Visual description:") or _extract_block(report, "OCR text:"):
        brain = _brain_answer(question, report, window_context)
        if brain:
            return brain[:SCREEN_RESPONSE_LIMIT]
    return _screen_answer(report, window_context, question)[:SCREEN_RESPONSE_LIMIT]


def capture_screen_jpeg():
    """Fresh capture of ALL monitors -> JPEG bytes (for Telegram send-back)."""
    import io as _io

    image = _capture_screen()
    buffer = _io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _photo_caption_brain(question, report, window_context=None):
    """The brain composes a grounded caption for a screen photo being handed
    to Sir (Telegram). Unlike _brain_answer (a spoken reply that must never
    mention the capture), THIS one may acknowledge that the image is right
    there beside the text — it IS the photo's caption.
    """
    description = _extract_block(report, "Visual description:")
    ocr = _extract_block(report, "OCR text:")
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
        "You are FRIDAY, Sir's personal assistant. You just captured your "
        "own look at his computer screens, and this image is being sent to him "
        "as a photo, so it sits right there beside your words.\n"
        "Fresh visual description: " + evidence + win + "\n"
        "Sir asked: " + (question or "send me a screenshot") + "\n\n"
        "Write the message for him in YOUR voice. STRICT rules:\n"
        "- LEAD with what he is actually doing/looking at on screen right now "
        "('You're on ...', 'opencode is the focused window ...'), grounded ONLY "
        "in the description above.\n"
        "- Do NOT say generic filler like 'a screenshot of a computer screen', "
        "nor list monitors/'left and right'/'sections'.\n"
        "- 1-2 SHORT sentences, ~20-40 words, warm, complete (always end punctuation)."
    )
    try:
        response = requests.post(
            LOCAL_CHAT_URL,
            json={
                "model": SCREEN_BRAIN_MODEL,
                "think": False,
                "keep_alive": "30m",
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": 0.5, "num_predict": 80, "num_ctx": 2048},
            },
            timeout=SCREEN_BRAIN_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            return ""
        text = (response.json().get("message", {}).get("content") or "").strip()
    except Exception:
        return ""
    sentence = _clean_sentence(text, limit=300)
    if not sentence:
        return ""
    if _is_generic_vision(sentence.lower()):
        return ""
    return _trim_to_sentence(sentence[:300])


def author_screen_photo(jpeg_bytes, question=""):
    """AI-AUTHORED caption for a fresh screen photo being handed to Sir.

    The capture is only an infrastructure primitive; the CONTENT is authored
    here: vision reads the actual image, then the brain (qwen3:8b) composes
    a grounded caption from what it truly sees. Returns ``(caption, vision)``.
    ``vision`` is persisted for follow-up turns so "do you see your own
    screenshot?" is answered with real context, never "I don't see a
    screenshot". Both are "" if the vision chain is cold.
    """
    try:
        vision = describe_image_bytes(jpeg_bytes, question)
    except Exception:
        vision = ""
    if not vision or vision.startswith("I could not analyze"):
        return "", ""
    report = "Visual description: " + vision.strip() + "\n"
    try:
        caption = _photo_caption_brain(question, report, get_active_window_context())
    except Exception:
        caption = ""
    if not caption:
        caption = _clean_sentence(vision.split("] ", 1)[-1] if "] " in vision else vision, limit=300)
    if not caption:
        return "", ""
    return _trim_to_sentence(caption[:1024]), vision.strip()


def describe_image_bytes(jpeg_bytes, question=""):
    """Vision for arbitrary JPEG bytes (Telegram photos): local producer first,
    then the cloud vision chain, then the bounded local deep-read fallback."""
    prompt = (question or "").strip() or "What is in this image? Answer in one warm sentence."
    ok, text, _error = _local_vision(prompt, jpeg_bytes)
    if ok and text:
        return f"[local] {text}"
    if api_pools.cloud_vision_enabled():
        ok, text, provider, _error = _cloud_vision(jpeg_bytes, prompt)
        if ok and text:
            return f"[{provider}] {text}"
    return "I could not analyze that image right now, Sir — my vision chain is unavailable."
