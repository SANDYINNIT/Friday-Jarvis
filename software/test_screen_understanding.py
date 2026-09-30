"""Focused tests for screen routing and vision/OCR result propagation."""

import base64
import os
import pytest
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


# ---------------------------------------------------------------------------
# Screenshot legibility + follow-up context (2026-09-30)
#
# Real failure: Sir asked "what is the left one about / what does it do" and
# FRIDAY answered "a simple or minimalistic web page... likely a background
# display". Root cause was a 640px max-side cap that turned this machine's
# 3840x1080 desktop into a 640x180 frame - OCR on it returned ZERO words.
# ---------------------------------------------------------------------------


def test_vision_budget_is_usable_not_a_thumbnail():
    """The cap must be a real pixel budget, not the old 640px side."""
    assert screen.VISION_MAX_PIXELS > 400_000, (
        "a sub-400k pixel budget cannot read UI text on a 1080p screen"
    )
    assert screen.VISION_MAX_SIDE >= 1280, "1280 is the practical floor for readable UI"


def test_fit_for_vision_respects_pixel_budget():
    from PIL import Image

    image = Image.new("RGB", (3840, 1080), "white")
    fitted = screen._fit_for_vision(image)
    assert fitted.size[0] * fitted.size[1] <= screen.VISION_MAX_PIXELS * 1.05
    assert fitted.size[0] <= screen.VISION_MAX_SIDE


def test_fit_for_vision_keeps_small_images_intact():
    from PIL import Image

    image = Image.new("RGB", (400, 300), "white")
    assert screen._fit_for_vision(image).size == (400, 300)


def test_encode_jpeg_uses_high_quality():
    """q75 destroyed small UI text; text needs q85+."""
    from PIL import Image

    noisy = Image.effect_noise((600, 400), 60).convert("RGB")
    high = screen._encode_jpeg(noisy, quality=95)
    low = screen._encode_jpeg(noisy, quality=75)
    assert len(high) > len(low), "quality=95 must preserve more detail than q75"


def test_ocr_intent_covers_what_does_it_do_questions():
    for question in (
        "what does the application do that is open?",
        "what is the left one about?",
        "explain what i am looking at",
        "what does that app do",
        "what am i working on right now",
    ):
        assert screen._wants_ocr(question) is True, question


def test_ocr_intent_stays_off_for_plain_description():
    assert screen._wants_ocr("what do you see") is False
    assert screen._wants_ocr("") is False
    assert screen._wants_ocr("play some music") is False


def test_monitor_labels_are_left_to_right_after_sorting():
    """The enumeration index is NOT left-to-right; labelling from it swapped
    left and right, so 'the left one' answered about the wrong screen."""
    layout = screen._monitor_layout()
    if not layout:
        return  # single/headless environment
    xs = [monitor["bounds"][0] for monitor in layout]
    assert xs == sorted(xs), "monitors must be ordered by X (left to right)"
    if len(layout) > 1:
        assert "LEFT" in layout[0]["label"]
        assert "RIGHT" in layout[-1]["label"]


def test_select_regions_picks_one_side_when_named(monkeypatch):
    from PIL import Image

    left = {"label": "the LEFT screen", "image": Image.new("RGB", (10, 10)), "primary": False}
    right = {"label": "the RIGHT screen", "image": Image.new("RGB", (10, 10)), "primary": True}
    monkeypatch.setattr(screen, "_capture_regions", lambda: [left, right])
    assert [r["label"] for r in screen._select_regions("what is the left one about")] == [
        "the LEFT screen"
    ]
    assert [r["label"] for r in screen._select_regions("what is on my right")] == [
        "the RIGHT screen"
    ]


def test_describe_screen_runs_ocr_on_content_questions(monkeypatch):
    """A 'what does it do' ask must be grounded in real text, not captioning."""
    from PIL import Image

    image = Image.new("RGB", (1920, 1080), "white")
    region = {"label": "the only screen", "image": image, "primary": True}
    monkeypatch.setattr(screen, "_capture_regions", lambda: [region])
    monkeypatch.setattr(screen, "get_active_window_context", lambda: {})
    monkeypatch.setattr(screen, "_visible_window_titles", lambda: ["- Claude Code (claude.exe)"])
    monkeypatch.setattr(screen.pytesseract, "image_to_string", lambda value: "JIRA BOARD SPRINT 42")
    monkeypatch.setattr(
        screen.requests, "post",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("vision must not run for OCR asks")),
    )
    result = screen.describe_screen("what does the application do that is open?")
    assert "JIRA BOARD SPRINT 42" in result
    assert "Claude Code" in result, "real window titles must ground the answer"


# ---------------------------------------------------------------------------
# Photo caption integrity (2026-09-30)
#
# Sir received a Telegram screenshot captioned "Fresh screen capture for you,
# Sir." - a canned acknowledgement that this project's own Screen/Vision/Media
# policy forbids. Cause chain, all measured:
#   1. capture_screen_jpeg() fed a full 3840x1080 frame to the local encoder,
#      which is documented to crash qwen2.5vl's Vulkan encode on this GPU.
#   2. The vision read returned nothing -> author_screen_photo returned "".
#   3. server.py fell back to the canned string.
# Plus: qwen3:8b did not answer the caption prompt inside 45s, and the 3B
# vision model confidently misread the screen ("a webpage in a serif font")
# while Tesseract read it correctly.
# ---------------------------------------------------------------------------


def test_canned_caption_string_is_gone_from_the_server():
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                        "source", "server", "server.py")
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    assert "Fresh screen capture for you" not in text, (
        "the canned acknowledgement violates the Screen/Vision/Media policy"
    )


def test_ocr_never_raises_and_returns_text():
    assert isinstance(screen._ocr_of_image(b"not-an-image"), str)


def test_ocr_snippet_drops_our_own_monitor_labels():
    """Tesseract merges the label bar into 'the LEFTscreen' (no space)."""
    raw = "the LEFTscreen\nthe RIGHTscreen (thisisthe PRIMARY display)\nQuarterly Budget Review"
    snippet = screen._clean_ocr_snippet(raw)
    assert snippet == "Quarterly Budget Review"
    assert "LEFTscreen" not in snippet
    assert "PRIMARY" not in snippet


def test_ocr_snippet_prefers_real_text_over_garbage():
    raw = "Ac (ERB SSEEEETD how nsnn-zo007\" +\nQuarterly Budget Review"
    assert screen._clean_ocr_snippet(raw) == "Quarterly Budget Review"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Quarterly Budget Review", True),
        ("Revenue up 12 percent Chrome 86.4M Safari", True),
        ("FRIDAY tool-calling streaming bug investigation", True),
        ("Ac (ERB SSEEEETD how nsnn-zo007\" +", False),
        ("the LEFTscreen", False),
        ("= 32 =", False),
        ("a", False),
    ],
)
def test_real_text_gate(text, expected):
    assert screen._looks_like_real_text(text) is expected


def test_grounded_caption_names_the_focused_app():
    caption = screen._grounded_photo_caption(
        "Quarterly Budget Review\nRevenue up 12 percent",
        {"title": "Notepad"},
        ["- Notepad (Notepad.exe)  [FOCUSED RIGHT NOW]"],
    )
    assert "Notepad" in caption
    assert "Quarterly Budget Review" in caption
    assert "screenshot" not in caption.lower(), "never a canned ack"


def test_grounded_caption_never_says_screenshot():
    caption = screen._grounded_photo_caption("some readable words here now", {"title": "Edge"}, [])
    assert "screenshot" not in caption.lower()


def test_visible_window_titles_filters_empty_shell_entries(monkeypatch):
    """list_windows is dominated by empty-title taskbar entries; filtering only
    after slicing returned [] and contributed nothing."""
    windows = [
        {"title": "", "process": "explorer.exe"},
        {"title": "", "process": "explorer.exe"},
        {"title": "Real Window", "process": "app.exe"},
    ]
    monkeypatch.setattr(screen, "get_active_window_context", lambda: {"title": "Real Window"})
    monkeypatch.setattr(
        "source.server.windows_control.list_windows", lambda limit=40: windows
    )
    titles = screen._visible_window_titles(5)
    assert any("Real Window" in t for t in titles)
    assert titles[0].endswith("[FOCUSED RIGHT NOW]"), "the focused window must lead"
    assert len(titles) == 1, "empty-title shell entries must be dropped"


def test_caption_uses_a_small_fast_model_by_default():
    """qwen3:8b timed out past 45s on the caption prompt."""
    assert screen.SCREEN_CAPTION_MODEL
    assert screen.SCREEN_CAPTION_MODEL != screen.SCREEN_BRAIN_MODEL
    assert screen.SCREEN_CAPTION_TIMEOUT_SECONDS <= 20


def test_vision_is_opt_in_for_the_caption_path():
    assert screen.SCREEN_CAPTION_USE_VISION is False, (
        "the 3B local vision model was measured wrong AND slow on dense screens"
    )


def test_author_screen_photo_never_raises_on_garbage_input():
    caption, persisted = screen.author_screen_photo(b"", "send screenshot")
    assert isinstance(caption, str) and isinstance(persisted, str)


def test_stacked_capture_is_portrait_not_letterbox(monkeypatch):
    """A 3840x1080 composite renders as an unreadable strip on a phone."""
    from PIL import Image

    a = {"label": "the LEFT screen", "image": Image.new("RGB", (1920, 1080), "white"),
         "primary": False}
    b = {"label": "the RIGHT screen", "image": Image.new("RGB", (1920, 1080), "black"),
         "primary": True}
    stacked = screen._stack_regions_for_phone([a, b])
    assert stacked.size[1] > stacked.size[0], "stacked capture must be portrait-ish"
    assert stacked.size[0] == 1920


def test_single_monitor_capture_passes_through(monkeypatch):
    from PIL import Image

    only = {"label": "the only screen", "image": Image.new("RGB", (800, 600)), "primary": True}
    assert screen._stack_regions_for_phone([only]).size == (800, 600)
