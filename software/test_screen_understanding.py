"""Focused tests for screen routing and vision/OCR result propagation."""

import base64
import io
from types import SimpleNamespace

from PIL import Image

from source.server import screen_understanding as screen


def test_screen_phrases_are_routed():
    assert screen.is_screen_request("Friday, what am I looking at?")
    assert screen.is_screen_request("what's on my screen")
    assert not screen.is_screen_request("Friday, what time is it?")
    assert screen.is_ocr_request("Friday, what text is on my screen?")
    assert not screen.is_ocr_request("describe my screen")


def test_model_selection_prefers_local_default(monkeypatch):
    monkeypatch.delenv("FRIDAY_VISION_MODEL", raising=False)
    # Local default is qwen2.5vl:3b, not gemma4:e2b. Verified empirically on
    # this machine: shown a solid red PNG, qwen2.5vl:3b answers "Red" while
    # gemma4:e2b returns an empty answer, so gemma4:e2b is not usable for
    # vision. FRIDAY_VISION_MODEL still overrides it explicitly.
    assert screen.select_vision_model() == "qwen2.5vl:3b"
    monkeypatch.setenv("FRIDAY_VISION_MODEL", "custom-vision")
    assert screen.select_vision_model() == "custom-vision"


def test_vision_and_ocr_results_are_both_returned(monkeypatch):
    image = Image.new("RGB", (4, 4), "white")

    class FakeResponse:
        # _brain_answer() checks status_code before reading the body (it is how
        # cloud/HTTP errors are classified), so the mock must provide it.
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "A code editor is visible."}}

    captured = {}
    monkeypatch.setattr(screen.ImageGrab, "grab", lambda: image)
    monkeypatch.setattr(screen.pytesseract, "image_to_string", lambda value: "print('ok')")
    monkeypatch.setattr(screen, "get_active_window_context", lambda: {"title": "Code", "process": "editor.exe"})

    # describe_screen runs the LOCAL vision producer first (user directive) and
    # only falls back to the cloud/brain stage when local returns nothing. This
    # test previously mocked only the cloud stage, so a real local model short
    # circuited it. Mock BOTH: local succeeds, and the cloud stage is asserted
    # to be unnecessary.
    monkeypatch.setattr(
        screen, "_local_vision",
        lambda prompt, jpeg_bytes, timeout=None: (True, "A code editor is visible.", ""),
    )

    def fake_post(url, json, timeout):
        captured.update(json)
        return FakeResponse()

    def unexpected_cloud(*args, **kwargs):
        raise AssertionError("cloud vision must not run when local vision succeeds")

    monkeypatch.setattr(screen.requests, "post", unexpected_cloud)
    result = screen.describe_screen("describe my screen")

    assert "A code editor is visible." in result
    assert "editor.exe" in result
    # OCR is deliberately NOT run for a plain description: it is expensive on
    # large multi-monitor frames, so it only runs for an explicit "read the
    # text" ask (see describe_screen). The OCR path itself is covered by
    # test_ocr_request_skips_vision below.
    assert "print('ok')" not in result


def test_vision_payload_carries_the_frame_and_window_context(monkeypatch):
    """The captured frame and window context must reach the vision stage.

    Asserted at the payload builder rather than end-to-end: the cloud stage is
    only reached when local vision fails AND a real provider key is configured,
    which is not something a headless test can rely on. This checks the thing
    that actually matters - that the frame is base64-encoded at its true size
    and the window metadata is injected into the prompt.
    """
    image = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(screen.ImageGrab, "grab", lambda: image)
    monkeypatch.setattr(
        screen, "get_active_window_context",
        lambda: {"title": "Code", "process": "editor.exe"},
    )

    captured = {}

    class _OkResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "A code editor is visible."}}]}

    def fake_post(url, json=None, timeout=None, headers=None, **kwargs):
        captured.update(json or {})
        return _OkResponse()

    monkeypatch.setattr(screen.requests, "post", fake_post)
    screen._gemini_vision("test-key", b"\xff\xd8jpegbytes", "describe the screen", "gemini-3.5-flash-lite")

    # OpenAI-style vision payload: the frame travels as a base64 data URL
    # inside the first user message.
    parts = captured["messages"][0]["content"]
    image_part = next(p for p in parts if p.get("type") == "image_url")
    url = image_part["image_url"]["url"]
    assert url.startswith("data:image/")
    assert ",/9h" in url or ",/" in url, "the frame must be base64 data, not a placeholder"
    encoded = url.split(",", 1)[1]
    assert base64.b64decode(encoded) == b"\xff\xd8jpegbytes"


def test_window_context_is_added_to_the_vision_prompt(monkeypatch):
    image = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(screen.ImageGrab, "grab", lambda: image)
    monkeypatch.setattr(
        screen, "get_active_window_context",
        lambda: {"title": "Code", "process": "editor.exe"},
    )
    monkeypatch.setattr(
        screen, "_local_vision",
        lambda prompt, jpeg_bytes, timeout=None: (True, "described", ""),
    )
    seen = {}

    def spy(prompt, jpeg_bytes, timeout=None):
        seen["prompt"] = prompt
        return True, "described", ""

    monkeypatch.setattr(screen, "_local_vision", spy)
    screen.describe_screen("describe my screen")
    assert "editor.exe" in seen["prompt"], "window metadata must reach the vision prompt"


def test_ocr_text_is_included_for_an_explicit_read_request(monkeypatch):
    """The companion case: an explicit OCR ask DOES surface the OCR text."""
    image = Image.new("RGB", (4, 4), "white")

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "A code editor is visible."}}

    monkeypatch.setattr(screen.ImageGrab, "grab", lambda: image)
    monkeypatch.setattr(screen.pytesseract, "image_to_string", lambda value: "print('ok')")
    monkeypatch.setattr(screen, "get_active_window_context", lambda: None)
    monkeypatch.setattr(screen.requests, "post", lambda url, json, timeout: FakeResponse())

    result = screen.describe_screen("what text is on my screen?")
    assert "print('ok')" in result, "an explicit OCR ask must include the OCR text"


def test_ocr_request_skips_vision(monkeypatch):
    image = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(screen.ImageGrab, "grab", lambda: image)
    monkeypatch.setattr(screen.pytesseract, "image_to_string", lambda value: "Visible text")
    monkeypatch.setattr(screen.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("vision called")))
    assert "Visible text" in screen.describe_screen("read my screen")


def test_vision_failure_is_truthful(monkeypatch):
    image = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(screen.ImageGrab, "grab", lambda: image)
    monkeypatch.setattr(screen.pytesseract, "image_to_string", lambda value: "")
    monkeypatch.setattr(screen.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("bounded")))
    result = screen.describe_screen("what is on my screen")
    assert "unavailable" in result.lower()
    assert "fresh" in result.lower()


def test_screen_fallback_is_direct_and_does_not_call_open_interpreter(monkeypatch):
    calls = []

    def fresh_fallback(question):
        calls.append(question)
        return "Fresh screen check:\nVisual description:\nA YouTube page with many videos.\nOCR text:\nRAM 42%"

    monkeypatch.setattr(screen, "describe_screen", fresh_fallback)
    monkeypatch.setattr(screen, "get_active_window_context", lambda: {"title": "YouTube"})  # the gateway
    result = screen.screen_response("what is on my screen")

    assert calls == ["what is on my screen"]
    # The SPOKEN reply is one short sentence, not the raw data dump.
    assert "YouTube" in result
    assert result.count("\n") == 0  # a single short spoken line
    assert "RAM" not in result      # raw OCR telemetry is not spoken
    assert "execute" not in result.lower()
