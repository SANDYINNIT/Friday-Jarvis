"""FRIDAY overlay for Open Interpreter's display module.

Patched in place (site-packages) so BOTH the voice server process AND the
IPython kernel process the model uses get the same behavior:

- display.find()      -> FRIDAY vision chain (gemini/openrouter creds from
                        ~/.friday/api_credentials.json) + disk screenshots
- display.find_text() -> local pytesseract OCR only (no remote /point/)
- every capture stored in the FRIDAY_SCRATCH_DIR folder (default
  ~/.friday/screenshots on this machine) (pruned to newest 20) + a
  scratchpad note (~/.friday/scratchpad.md) so provider hops inherit state.

This file replaces Open Interpreter's legacy remote /point/ icon locator,
which is never running locally.
"""

import base64
import io
import json
import os
import re
import time

import requests
from PIL import Image, ImageGrab
import pytesseract

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
SCRATCH = os.path.expanduser("~/.friday/scratchpad.md")
SHOTS_DIR = os.getenv("FRIDAY_SCRATCH_DIR") or os.path.join(
    os.path.expanduser("~"), ".friday", "screenshots"
)
GEMINI_VISION_MODEL = os.getenv("FRIDAY_GEMINI_VISION_MODEL", "gemini-3.1-flash-lite")

# 2026-10-03 (Round M). MODAL FIRST, matching software/AGENTS.md ("Modal is
# primary because it is self-hosted, so it has no public free-tier quota to
# exhaust"; "Vision uses the same Modal endpoint first") and the order
# screen_understanding._cloud_vision already uses. This module previously had no
# Modal stage at all, so the brain's own `computer.vision.query()` could only
# reach Gemini and OpenRouter.
#
# MEASURED 2026-10-03 before choosing: Modal answered a full-desktop screenshot
# in 4.2s. It is also the only stage on this machine with no third-party quota.
# The endpoint is resolved LAZILY because `_creds()` is defined further down this
# module; reading it at import time would raise NameError.
MODAL_VISION_MODEL = os.getenv(
    "FRIDAY_MODAL_VISION_MODEL", "nvidia/GLM-5.3-Flash-NVFP4"
)
MODAL_VISION_MAX_TOKENS = int(os.getenv("FRIDAY_MODAL_VISION_MAX_TOKENS", "2000"))

# The previous default here was
# `meta-llama/llama-3.2-11b-vision-instruct:free`, which OpenRouter has since
# RETIRED: `GET /api/v1/models/meta-llama/llama-3.2-11b-vision-instruct:free`
# now returns HTTP 404, so every brain-authored `computer.vision.query()`
# failed instantly with `vision http 404`.
#
# VERIFIED 2026-10-03 against the live provider with a real full-desktop
# screenshot and this module's own _LOCATE_PROMPT (3 runs each):
#   qwen/qwen3.8-27b:free        3/3 HTTP 200, JSON every run  (1/3 correct locate)
#   google/gemma-4-31b-it:free   0/3 - HTTP 429 capacity-blocked at test time
#   google/gemma-4-26b-a4b-it:free 0/3 - HTTP 429
#   thinkingmachines/inkling*:free  HTTP 403 "only available on agentic harnesses"
#   nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free  returned y_percent=950,
#       i.e. an out-of-range coordinate that would click off-screen
# qwen3.8-27b is chosen because it is the only candidate that was reliably
# reachable AND protocol-correct, and it is already in FRIDAY's documented brain
# chain in software/AGENTS.md. It is a LAST-RESORT fallback behind Modal and
# Gemini, so its 1/3 locate accuracy is honest reporting rather than a protocol
# failure. Override with FRIDAY_OPENROUTER_VISION_MODEL as before.
OPENROUTER_VISION_MODEL = os.getenv(
    "FRIDAY_OPENROUTER_VISION_MODEL",
    "qwen/qwen3.8-27b:free",
)
# Reasoning-style models spend the budget on `reasoning` and return an EMPTY
# `content` when truncated. Measured with 300 tokens: qwen3.8 came back
# finish_reason="length" with content="" (empty) in 2 of 3 runs; at 2000 it
# returned parseable JSON every time. Gemini's budget is unchanged at 300.
OPENROUTER_VISION_MAX_TOKENS = int(
    os.getenv("FRIDAY_OPENROUTER_VISION_MAX_TOKENS", "2000")
)
VISION_TIMEOUT = 25.0

GEMINI_CHAIN = [
    name.strip()
    for name in os.getenv(
        "FRIDAY_VISION_GEMINI_CHAIN",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    ).split(",")
    if name.strip()
]

_LOCATE_PROMPT = (
    'Locate the element described as "{desc}" in this screenshot. '
    "It may be an app icon, taskbar button, menu item, card, or text label. "
    "Find the BEST match on screen. Reply ONLY raw JSON: "
    '{{"found": true, "x_percent": <0-100>, "y_percent": <0-100>}} for the '
    'CENTER POINT, or {{"found": false}}.'
)


def _scratch_note(text, tag):
    line = " ".join(str(text or "").split())[:300]
    if not line:
        return
    try:
        os.makedirs(os.path.dirname(SCRATCH), exist_ok=True)
        with open(SCRATCH, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"- [{time.strftime('%H:%M:%S')}] {tag}: {line}\n")
    except OSError:
        pass


def _flatten(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    if isinstance(value, str):
        return [value]
    return []


def _creds():
    path = os.path.expanduser("~/.friday/api_credentials.json")
    try:
        with open(path) as handle:
            return json.load(handle)
    except OSError:
        return {}


def _modal_base(creds=None):
    """The Modal endpoint, or "" when Modal is not configured.

    Same precedence as brain_router._modal_api_base(): the env override wins,
    then the stored credential. The trailing `/v1` is REQUIRED - without it
    Modal answers `404 {"error": "route not found"}`.
    """
    base = os.getenv("FRIDAY_MODAL_BASE", "").strip().rstrip("/")
    if not base:
        creds = creds if creds is not None else _creds()
        base = str((creds or {}).get("modal_endpoint") or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/v1") else base + "/v1"


def _modal_keys(creds=None):
    creds = creds if creds is not None else _creds()
    return _flatten(creds.get("modal_vision")) + _flatten(creds.get("modal_brain"))


def _percent_in_range(value):
    """A locate result must be a real on-screen percentage.

    HARDENED 2026-10-03: a probed vision model returned
    `{"found": true, "x_percent": 10, "y_percent": 950}`, and 216 is turned
    into an absolute pixel coordinate by `int(height * 950 / 100)`. That is a
    click far outside the screen. A value outside 0-100 is a model error, so it
    is rejected rather than clamped - clamping would aim at a plausible-looking
    but invented point."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and 0.0 <= float(value) <= 100.0


def _jpeg_bytes(image):
    rgb = image.convert("RGB")
    stream = io.BytesIO()
    rgb.save(stream, format="JPEG", quality=80)
    return stream.getvalue()


def save_screenshot(image, label="screen"):
    try:
        os.makedirs(SHOTS_DIR, exist_ok=True)
        names = sorted(
            (os.path.join(SHOTS_DIR, n) for n in os.listdir(SHOTS_DIR)
             if n.lower().endswith((".png", ".jpg", ".jpeg"))),
            key=os.path.getmtime,
        )
        for stale in names[: max(0, len(names) - 20)]:
            try:
                os.remove(stale)
            except OSError:
                pass
        path = os.path.join(
            SHOTS_DIR,
            f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', str(label))[:24]}.png",
        )
        image.save(path, format="PNG")
        _scratch_note(f"screenshot saved: {path} (REUSE it; do not re-shoot)", "capture")
        return path
    except Exception:
        return None


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


def _call_api(base, key, payload):
    headers = {"Authorization": f"Bearer {key}"}
    response = requests.post(
        f"{base}/chat/completions", headers=headers, json=payload, timeout=25
    )
    if response.status_code != 200:
        raise RuntimeError(f"vision http {response.status_code}")
    content = (response.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
    text = "".join(part.get("text", "") for part in content) if isinstance(content, list) else str(content)
    return (text or "").strip()


def _point_from_text(text):
    match = re.search(r"\{[^{}]*\}", str(text or ""), re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    x, y = data.get("x_percent"), data.get("y_percent")
    if data.get("found") and _percent_in_range(x) and _percent_in_range(y):
        return (float(x), float(y))
    return None


def locate(description, screenshot=None):
    if os.environ.get("FRIDAY_USER_ROUTES"):
        raise RuntimeError("Legacy image tools do not implement the chosen Vision chain; use the configured native screen reader instead.")
    """Icon/button locate via FRIDAY's vision chain. Never raises."""
    started = time.time()
    try:
        screen = screenshot if isinstance(screenshot, Image.Image) else ImageGrab.grab()
    except Exception as error:
        print(f"[icon locate error: {error}]", flush=True)
        return []
    if description.startswith('"') and description.endswith('"'):
        description = description[1:-1]
    width, height = screen.size
    save_screenshot(screen, str(description))
    jpeg = _jpeg_bytes(screen)
    prompt = _LOCATE_PROMPT.format(desc=description)
    creds = _creds()
    modal_base = _modal_base(creds)
    modal_keys = _modal_keys(creds) if modal_base else []
    gemini_keys = _flatten(creds.get("gemini_vision")) + _flatten(creds.get("gemini_brain"))
    or_keys = _flatten(creds.get("openrouter_vision")) + _flatten(creds.get("openrouter_brain"))
    payload_text = ""
    # Modal first (self-hosted, no free-tier quota, 4.2s measured here).
    for key in modal_keys:
        try:
            payload_text = _call_api(
                modal_base, key,
                _openai_vision_payload(jpeg, prompt, MODAL_VISION_MODEL,
                                       MODAL_VISION_MAX_TOKENS),
            )
            break
        except Exception as error:
            payload_text = str(error)
    for key in gemini_keys:
        if payload_text and "{" in payload_text:
            break
        for model in GEMINI_CHAIN:
            try:
                payload_text = _call_api(GEMINI_BASE, key, _openai_vision_payload(jpeg, prompt, model))
                break
            except Exception as error:
                payload_text = str(error)
        if payload_text and "{" in payload_text:
            break
    if not payload_text or "{" not in payload_text:
        for key in or_keys:
            try:
                payload_text = _call_api(
                    OPENROUTER_BASE, key,
                    _openai_vision_payload(jpeg, prompt, OPENROUTER_VISION_MODEL,
                                           OPENROUTER_VISION_MAX_TOKENS),
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


def find_text_locally(text, screenshot=None):
    """OCR find; no remote calls. Never raises."""
    text = str(text or "").strip().strip('"')
    if not text:
        return []
    try:
        image = screenshot if isinstance(screenshot, Image.Image) else ImageGrab.grab()
        rgb = image.convert("RGB")
        save_screenshot(rgb, f"findtext_{text[:18]}")
        data = pytesseract.image_to_data(rgb, output_type=pytesseract.Output.DICT)
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
            print(f'[friday find_text] "{text}" not visible on screen.', flush=True)
        return results
    except Exception as error:
        print(f"[find_text error: {error}]", flush=True)
        return []


def describe_image(question, *, base_64=None, path=None, lmc=None, pil_image=None):
    if os.environ.get("FRIDAY_USER_ROUTES"):
        raise RuntimeError("Legacy image tools do not implement the chosen Vision chain; use the configured native screen reader instead.")
    """Answer a question about an image via FRIDAY's cloud vision chain."""
    image = None
    try:
        if lmc and isinstance(lmc, dict) and "base64" in str(lmc.get("format", "")):
            image = Image.open(io.BytesIO(base64.b64decode(lmc["content"])))
        elif pil_image is not None:
            image = pil_image
        elif base_64:
            image = Image.open(io.BytesIO(base64.b64decode(base_64)))
        elif path:
            image = Image.open(path)
        image = image.convert("RGB") if image is not None else ImageGrab.grab()
    except Exception as error:
        print(f"[friday vision error: {error}]", flush=True)
        return ""
    save_screenshot(image, str(question)[:20])
    prompt = (
        str(question or "Describe this image concisely.")
        + " Answer in plain, short sentences."
    )
    jpeg = _jpeg_bytes(image)
    creds = _creds()
    modal_base = _modal_base(creds)
    modal_keys = _modal_keys(creds) if modal_base else []
    gemini_keys = _flatten(creds.get("gemini_vision")) + _flatten(creds.get("gemini_brain"))
    or_keys = _flatten(creds.get("openrouter_vision")) + _flatten(creds.get("openrouter_brain"))
    payload_text = ""
    # Modal first (self-hosted, no free-tier quota, 4.2s measured here).
    for key in modal_keys:
        try:
            payload_text = _call_api(
                modal_base, key,
                _openai_vision_payload(jpeg, prompt, MODAL_VISION_MODEL,
                                       MODAL_VISION_MAX_TOKENS),
            )
            break
        except Exception as error:
            payload_text = str(error)
    for key in gemini_keys:
        if payload_text.strip():
            break
        for model in GEMINI_CHAIN:
            try:
                payload_text = _call_api(GEMINI_BASE, key, _openai_vision_payload(jpeg, prompt, model))
                break
            except Exception as error:
                payload_text = str(error)
    if not payload_text.strip():
        for key in or_keys:
            try:
                payload_text = _call_api(
                    OPENROUTER_BASE, key,
                    _openai_vision_payload(jpeg, prompt, OPENROUTER_VISION_MODEL,
                                           OPENROUTER_VISION_MAX_TOKENS),
                )
                break
            except Exception as error:
                payload_text = str(error)
    payload_text = (payload_text or "").strip()
    if not payload_text:
        print("[friday vision] unavailable for this image", flush=True)
        return ""
    return payload_text
