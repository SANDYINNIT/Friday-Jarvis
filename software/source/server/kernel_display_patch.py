"""KERNEL-SIDE display patch payload.

The model's authored python runs inside the IPython kernel process, whose
`computer` object is a DIFFERENT instance from the voice server's — the
server-side display_patch never reached it. This payload is exec'd INSIDE
the kernel (via interpreter.computer.run) so it patches THE computer the
model actually uses: vision-chain icon locate, local OCR find_text, and
disk-backed screenshots (D:\01\screenshots, pruned) with scratchpad
entries that survive provider hops.

Self-contained: no server package imports (kernel runs the same venv but
not the server package).
"""

import base64
import io
import json as _json
import os as _os
import re as _re
import time as _time

import requests as _requests
from PIL import Image as _Image, ImageGrab as _ImageGrab
import pytesseract as _pytesseract

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
SCRATCH = _os.path.expanduser("~/.friday/scratchpad.md")
SHOTS_DIR = _os.getenv("FRIDAY_SCRATCH_DIR", r"D:\01\screenshots")
GEMINI_VISION_MODEL = _os.getenv("FRIDAY_GEMINI_VISION_MODEL", "gemini-3.1-flash-lite")
OPENROUTER_VISION_MODEL = _os.getenv(
    "FRIDAY_OPENROUTER_VISION_MODEL",
    "inclusionai/ling-3.0-flash-vl:free",
)
VISION_TIMEOUT = float(_os.getenv("FRIDAY_VISION_TIMEOUT", "25"))

_GEMINI_CHAIN = [
    name.strip()
    for name in _os.getenv(
        "FRIDAY_VISION_GEMINI_CHAIN",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    ).split(",")
    if name.strip()
]

_LOCATE_PROMPT = (
    'Locate the element described as "{desc}" in this screenshot. '
    "It may be an app icon, taskbar button, menu item, card, or text label. "
    "Find the BEST match. Reply ONLY raw JSON: "
    '{{"found": true, "x_percent": <0-100>, "y_percent": <0-100>}} as the '
    'CENTER POINT, or {{"found": false}}.'
)


def _scratch_note(text, tag):
    line = " ".join(str(text or "").split())[:300]
    if not line:
        return
    try:
        _os.makedirs(_os.path.dirname(SCRATCH), exist_ok=True)
        with open(SCRATCH, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"- [{_time.strftime('%H:%M:%S')}] {tag}: {line}\n")
    except OSError:
        pass


def _creds_dotenv():
    path = _os.path.expanduser("~/.friday/api_credentials.json")
    try:
        with open(path) as handle:
            return _json.load(handle)
    except Exception:
        return {}


def _jpeg_bytes(image):
    rgb = image.convert("RGB")
    stream = io.BytesIO()
    rgb.save(stream, format="JPEG", quality=80)
    return stream.getvalue()


def _shots_dir():
    try:
        _os.makedirs(SHOTS_DIR, exist_ok=True)
        names = sorted(
            (_os.path.join(SHOTS_DIR, n) for n in _os.listdir(SHOTS_DIR)
             if n.lower().endswith((".png", ".jpg", ".jpeg"))),
            key=_os.path.getmtime,
        )
        for stale in names[: max(0, len(names) - 20)]:
            try:
                _os.remove(stale)
            except OSError:
                pass
        return SHOTS_DIR
    except OSError:
        return "~"


def save_screenshot(image, label):
    try:
        path = _os.path.join(
            _shots_dir(),
            f"{_time.strftime('%Y%m%d-%H%M%S')}_"
            f"{_re.sub(r'[^a-zA-Z0-9_-]+', '_', str(label))[:24]}.png",
        )
        image.save(path, format="PNG")
        _scratch_note(f"screenshot saved: {path} (REUSE it; do not re-shoot)", "capture")
        return path
    except Exception:
        return None


def _vision_payload(jpeg_bytes, prompt, model):
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


def _call_api(base, key, payload):
    headers = {"Authorization": f"Bearer {key}"}
    response = _requests.post(
        f"{base}/chat/completions", headers=headers, json=payload, timeout=25
    )
    if response.status_code != 200:
        raise RuntimeError(f"vision http {response.status_code}")
    content = (response.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
    text = "".join(part.get("text", "") for part in content) if isinstance(content, list) else str(content)
    return (text or "").strip()


def _point_from_text(text):
    match = _re.search(r"\{[^{}]*\}", str(text or ""), _re.DOTALL)
    if not match:
        return None
    try:
        data = _json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    x, y = data.get("x_percent"), data.get("y_percent")
    if data.get("found") and isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return (float(x), float(y))
    return None


def _flatten(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    if isinstance(value, str):
        return [value]
    return []


def locate_with_vision(description, screenshot=None):
    """Locate DS['label'] via FRIDAY's cloud/local vision chain. Returns
    OI-format [{"coordinates": (x, y), ...}] or [] (never raises)."""
    started = _time.time()
    try:
        screen = screenshot if isinstance(screenshot, _Image.Image) else _ImageGrab.grab()
    except Exception as error:
        print(f"[icon locate error: {error}]", flush=True)
        return []
    width, height = screen.size
    save_screenshot(screen, str(description))
    jpeg = _jpeg_bytes(screen)
    prompt = _LOCATE_PROMPT.format(desc=description)
    creds = _creds_dotenv()
    gemini_keys = _flatten(creds.get("gemini_vision")) + _flatten(creds.get("gemini_brain"))
    or_keys = _flatten(creds.get("openrouter_vision")) + _flatten(creds.get("openrouter_brain"))
    payload_text = ""
    for key in gemini_keys:
        for model in _GEMINI_CHAIN:
            try:
                payload_text = _call_api(GEMINI_BASE, key, _vision_payload(jpeg, prompt, model))
                break
            except Exception as error:
                payload_text = str(error)
        if payload_text and "{" in payload_text:
            break
    if not payload_text or "{" not in payload_text:
        for key in or_keys:
            try:
                payload_text = _call_api(
                    OPENROUTER_BASE, key, _vision_payload(jpeg, prompt, OPENROUTER_VISION_MODEL)
                )
                break
            except Exception as error:
                payload_text = str(error)
    point = _point_from_text(payload_text)
    if point is None:
        _scratch_note(
            f"locate '{description}' failed (not on screen) - pivot; do NOT retry same call",
            "locate",
        )
        print(f'[icon locate] "{description}" NOT on screen - Start menu/keyboard instead', flush=True)
        return []
    coordinates = (int(width * point[0] / 100.0), int(height * point[1] / 100.0))
    _scratch_note(f"locate '{description}' FOUND at {coordinates}", "locate")
    print(f'[icon locate] "{description}" at {coordinates}', flush=True)
    return [{"coordinates": coordinates, "text": description, "similarity": 1.0}]


def install_k(computer):
    display = computer.display

    if getattr(display, "_friday_kernel_patched", False) is True:
        return "already-patched"

    def find(description, screenshot=None, **kwargs):
        description = str(description or "").strip().strip('"')
        if not description:
            return []
        return locate_with_vision(description, screenshot)

    def find_text(text, screenshot=None, **kwargs):
        text = str(text or "").strip('"')
        if not text:
            return []
        try:
            image = screenshot if isinstance(screenshot, _Image.Image) else _ImageGrab.grab()
            rgb = image.convert("RGB")
            save_screenshot(rgb, f"findtext_{text[:18]}")
            data = _pytesseract.image_to_data(rgb, output_type=_pytesseract.Output.DICT)
            needle = text.lower()
            results = []
            for index, word in enumerate(data.get("text", [])):
                word = (word or "").strip()
                if word and needle in word.lower():
                    left, top = data["left"][index], data["top"][index]
                    wide, tall = data["width"][index], data["height"][index]
                    results.append(
                        {"coordinates": (int(left + wide / 2), int(top + tall / 2)), "text": word, "similarity": 1.0}
                    )
            if not results:
                _scratch_note(f"find_text '{text}' not visible", "locate")
                print(f'[find_text] "{text}" not visible on screen.', flush=True)
            return results
        except Exception as error:
            print(f"[find_text error: {error}]", flush=True)
            return []

    _shot_depth = {"n": 0}
    original_screenshot = getattr(display, "screenshot", None)

    def screenshot_guard(*args, **kwargs):
        _shot_depth["n"] += 1
        try:
            if _shot_depth["n"] > 1:
                return original_screenshot(*args, **kwargs)
            if original_screenshot is not None:
                shot = original_screenshot(*args, **kwargs)
            else:
                shot = _ImageGrab.grab()
        finally:
            _shot_depth["n"] -= 1
        try:
            if isinstance(shot, _Image.Image):
                save_screenshot(shot, str(kwargs.get("purpose") or (args[0] if args else "screen")))
        except Exception:
            pass
        return shot

    display.find = find
    display.find_text = find_text
    try:
        display.screenshot = screenshot_guard
        if callable(getattr(display, "view", None)):
            display.view = screenshot_guard
    except Exception:
        pass
    display._friday_kernel_patched = True
    print("[kernel display patch] find/find_text/screenshots -> FRIDAY vision+OCR+disk", flush=True)
    return "patched"
