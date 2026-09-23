"""Replace Open Interpreter's remote /point/ display APIs.

OI's `computer.display.find()` posts screenshots to a legacy "icon locating
API" at computer.api_base that is never running locally — every call
JSONDecodes on an error page, wastes ~30s, and FRIDAY's model then loops
the same broken approach (which is how single turns burned entire Groq
8k-TPM buckets). These helpers route pointing at FRIDAY's own vision
chain (screen_understanding) and local OCR instead.
"""

import io
import json
import os
import re
import time

from PIL import Image, ImageGrab
import pytesseract

from . import screen_understanding as _su
from . import self_improve as _si


def _snapshot_to_disk(image, label):
    """Persist the capture to D:\\01\\screenshots (pruned) so later agents
    can REUSE it instead of re-shooting the screen. Returns path or None."""
    try:
        directory = _si.save_screenshot_folder()
        path = os.path.join(
            directory,
            f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', label)[:24]}.png",
        )
        image.save(path, format="PNG")
        _si.scratchpad_note(
            f"screenshot saved: {path} (reuse instead of re-shooting)",
            tag="capture",
        )
        return path
    except Exception:
        return None


_LOCATE_PROMPT = (
    'Locate the element described as "{desc}" in this screenshot. '
    "It may be an app icon, taskbar button, menu item, card, or text label. "
    "Find the BEST match on screen. Reply ONLY with raw lowercase JSON and "
    'nothing else: {{"found": true, "x_percent": <0-100>, "y_percent": '
    "<0-100>}} using the CENTER POINT of that element. "
    'If it is genuinely not visible anywhere, reply {{"found": false}}.'
)


def _jpeg_bytes(image):
    rgb = image.convert("RGB")
    stream = io.BytesIO()
    rgb.save(stream, format="JPEG", quality=80)
    return stream.getvalue()


def _parse_point(text):
    """Extract (x_percent, y_percent) or None from a vision reply."""
    if not text:
        return None
    match = re.search(r"\{[^{}]*\}", str(text), re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    x, y = data.get("x_percent"), data.get("y_percent")
    if data.get("found") and isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return (float(x), float(y))
    return None


def _locate_via_vision(description, screenshot=None):
    """Return ((x, y) screen pixels, provider) or (None, reason)."""
    screen = screenshot if isinstance(screenshot, Image.Image) else ImageGrab.grab()
    width, height = screen.size
    jpeg = _jpeg_bytes(screen)
    prompt = _LOCATE_PROMPT.format(desc=description)
    started = time.time()

    try:
        ok, payload, provider, _error = _su._cloud_vision(jpeg, prompt)
    except Exception as error:
        ok, payload, provider, _error = False, None, "", str(error)
    if not ok or not payload:
        try:
            ok, payload, local_error = _su._local_vision(
                prompt, _su.select_vision_model(), jpeg
            )
        except Exception as error:
            ok, payload, local_error = False, None, str(error)
        provider = f"local/{_su.select_vision_model()}"
        if not ok or not payload:
            return None, f"vision unavailable ({local_error or provider})"

    point = _parse_point(payload)
    elapsed = time.time() - started
    if point is None:
        return None, "not on screen"
    return (
        (int(width * point[0] / 100.0), int(height * point[1] / 100.0)),
        (provider or "vision", f"{elapsed:.1f}s"),
    )


def install_display_helpers(interpreter):
    """Patch computer.display.find / find_text on the live interpreter."""
    display = interpreter.computer.display
    if getattr(display, "_friday_patched", False) is True:
        return
    try:
        select_vision_model = _su.select_vision_model
        cloud_vision = _su._cloud_vision
        local_vision = _su._local_vision
    except Exception as bind_error:  # defensive: never break interpreter boot
        print(f"[display helpers] bind failed, keeping OI defaults: {bind_error}", flush=True)
        return

    def find(description, screenshot=None, **kwargs):
        description = str(description or "").strip().strip('"')
        if not description:
            return []
        try:
            vision_screen = screenshot if isinstance(screenshot, Image.Image) else None
            if vision_screen is None:
                vision_screen = ImageGrab.grab()
            width, height = vision_screen.size
            snapshot_path = _snapshot_to_disk(vision_screen, description)
            jpeg = _jpeg_bytes(vision_screen)
            prompt = _LOCATE_PROMPT.format(desc=description)
            started = time.time()
            ok, payload, provider = False, None, ""
            try:
                ok, payload, provider_name, _err = cloud_vision(jpeg, prompt)
                provider = provider_name
            except Exception as cloud_error:
                _ = cloud_error
            if not ok or not payload:
                ok, payload, local_error = local_vision(
                    prompt, select_vision_model(), jpeg
                )
                provider = f"local/{select_vision_model()}"
            elapsed = time.time() - started
            if not ok or not payload:
                print(
                    f'[icon locate] vision unavailable for "{description}" '
                    f"({elapsed:.1f}s) — try keyboard search or Start menu instead.",
                    flush=True,
                )
                return []
            point = _parse_point(payload)
            if point is None:
                _si.scratchpad_note(
                    f"locate '{description}' failed (not on screen) — pivot, do NOT retry same call",
                    tag="locate",
                )
                print(
                    f'[icon locate] "{description}" NOT on screen '
                    f"({elapsed:.1f}s). Launch via Start menu / keyboard instead.",
                    flush=True,
                )
                return []
            coordinates = (int(width * point[0] / 100.0), int(height * point[1] / 100.0))
            _si.scratchpad_note(
                f"locate '{description}' FOUND at {coordinates}",
                tag="locate",
            )
            print(
                f'[icon locate] "{description}" at {coordinates} '
                f"({elapsed:.1f}s via {provider})",
                flush=True,
            )
            return [
                {"coordinates": coordinates, "text": description, "similarity": 1.0}
            ]
        except Exception as find_error:
            print(f"[icon locate error: {find_error}]", flush=True)
            return []

    def find_text(text, screenshot=None, **kwargs):
        text = str(text or "").strip('"')
        if not text:
            return []
        try:
            image = screenshot if isinstance(screenshot, Image.Image) else ImageGrab.grab()
            rgb = image.convert("RGB")
            data = pytesseract.image_to_data(rgb, output_type=pytesseract.Output.DICT)
            needle = text.lower()
            results = []
            for index, word in enumerate(data.get("text", [])):
                word = (word or "").strip()
                if not word or needle not in word.lower():
                    continue
                left, top = data["left"][index], data["top"][index]
                wide, tall = data["width"][index], data["height"][index]
                center = (int(left + wide / 2), int(top + tall / 2))
                results.append({"coordinates": center, "text": word, "similarity": 1.0})
            if not results:
                print(f'[find_text] "{text}" not visible on screen.', flush=True)
            return results
        except Exception as find_error:
            print(f"[find_text error: {find_error}]", flush=True)
            return []

    display.find = find
    display.find_text = find_text

    # Wrap display.screenshot / display.view so EVERY capture (hers or the
    # helpers') lands on disk + scratchpad → later models reuse instead of
    # re-shooting. Depth counter suppresses double-save when view()
    # delegates to screenshot() internally.
    _shot_depth = {"n": 0}
    original_screenshot = getattr(display, "screenshot", None)

    def screenshot_guard(*args, **kwargs):
        _shot_depth["n"] += 1
        try:
            if _shot_depth["n"] > 1 and original_screenshot is not None:
                return original_screenshot(*args, **kwargs)
            if original_screenshot is not None:
                shot = original_screenshot(*args, **kwargs)
            else:
                shot = ImageGrab.grab()
        finally:
            _shot_depth["n"] -= 1
        try:
            if isinstance(shot, Image.Image):
                _snapshot_to_disk(
                    shot, str(kwargs.get("purpose") or (args[0] if args else "screen"))
                )
        except Exception:
            pass
        return shot

    try:
        display.screenshot = screenshot_guard
        if callable(getattr(display, "view", None)):
            display.view = screenshot_guard
    except Exception:
        pass

    display._friday_patched = True
    print("[display helpers] OI /point/ replaced with FRIDAY vision+OCR", flush=True)
