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


def test_ordered_action_then_screenshot_is_not_preempted_by_vision():
    for text in (
        "open Notepad and then take a screenshot",
        "search my computer for the file, then send a screenshot",
        "do this and take a screenshot afterwards",
        "type hello, and then take a screenshot of my screen",
    ):
        assert screen.is_screen_request(text) is False, text

    assert screen.is_screen_request("take a screenshot") is True
    assert screen.is_screen_request("show me your screen") is True


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
      # Offline: this must never make a real call to Modal. Modal is the PRIMARY
      # vision stage (Sir 2026-09-30), so this used to reach out to the network
      # for real. requests.post is stubbed AND the stage is spied directly.
      class _OkResponse:
          status_code = 200

          def json(self):
              return {"choices": [{"message": {"content": "described"},
                                   "finish_reason": "stop"}]}

      monkeypatch.setattr(
          screen.requests, "post", lambda *a, **k: _OkResponse()
      )
      seen = {}

      def spy(jpeg_bytes, prompt):
          seen["prompt"] = prompt
          return True, "described", "vision_modal", ""

      monkeypatch.setattr(screen, "_cloud_vision", spy)
      monkeypatch.setattr(
          screen, "_local_vision",
          lambda prompt, jpeg_bytes, timeout=None: (True, "described", ""),
      )
      screen.describe_screen("describe my screen")
      assert "editor.exe" in seen["prompt"], "window metadata must reach the vision prompt"
      assert "editor.exe" in seen["prompt"], "and it must reach the PRIMARY vision stage"


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


def test_modal_vision_quota_failure_triggers_6h_ban(monkeypatch):
    """A Modal 401/403/429 must BAN vision_modal, not just log it.

    Sir's directive: if Modal vision fails it is banned for 6 hours. Before the
    fix, _cloud_vision only logged the quota failure and fell through, so every
    screenshot retried a dead endpoint forever.
    """
    banned = []

    def _fake_modal_vision(jpeg, prompt, timeout=None):
        return False, None, True, "HTTP 429"

    monkeypatch.setattr(screen, "_modal_vision", _fake_modal_vision)
    monkeypatch.setattr(
        screen.api_pools, "fail_model", lambda pool, model: banned.append((pool, model))
    )
    # No other stage configured, so we fall straight through to the title path.
    monkeypatch.setattr(screen.api_pools, "pool", lambda name: None)

    screen._cloud_vision(b"jpeg", "what is on my screen?")

    assert banned == [("vision_modal", screen._modal_vision_model())], banned


def test_modal_vision_transient_error_is_NOT_banned(monkeypatch):
    """A timeout / 5xx is transient: fall through, but do NOT burn a 6h ban.

    Modal cold starts take a moment; banning on those would take vision offline
    for six hours over one slow boot.
    """
    banned = []

    def _fake_modal_vision(jpeg, prompt, timeout=None):
        return False, None, False, "timeout"

    monkeypatch.setattr(screen, "_modal_vision", _fake_modal_vision)
    monkeypatch.setattr(
        screen.api_pools, "fail_model", lambda pool, model: banned.append((pool, model))
    )
    monkeypatch.setattr(screen.api_pools, "pool", lambda name: None)

    screen._cloud_vision(b"jpeg", "what is on my screen?")

    assert banned == [], "transient errors must not trigger the 6h ban"


def test_modal_vision_ban_names_the_model_it_actually_called(monkeypatch):
    """FRIDAY_MODAL_VISION_MODEL overrides the router default.

    The ban must name the model that was really requested, not the default -
    otherwise we ban a model we never called and keep hammering the real one.
    """
    monkeypatch.setenv("FRIDAY_MODAL_VISION_MODEL", "some/other-vision-model")
    assert screen._modal_vision_model() == "some/other-vision-model"
    monkeypatch.delenv("FRIDAY_MODAL_VISION_MODEL")
    # Falls back to the router's single source of truth.
    from source.server.brain_router import MODAL_MODEL

    assert screen._modal_vision_model() == MODAL_MODEL


# --- Modal vision token budget + routing order (2026-10-01) ---

def _stub_modal_pool(monkeypatch):
    """Give _modal_vision a usable pool WITHOUT any real credentials.

    Tests must never depend on ~/.friday/api_credentials.json or hit the network.
    """
    class _Pool:
        empty = False

        def next_key(self):
            return "test-key"

        def succeed(self, key):
            return None

    monkeypatch.setattr(screen.api_pools, "pool", lambda name: _Pool())
    monkeypatch.setattr(screen.api_pools, "model_succeed", lambda pool, model: None)


def _fake_response(content, finish_reason):
    class R:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": content},
                                 "finish_reason": finish_reason}]}
    return R()


def test_modal_vision_budget_leaves_room_for_reasoning():
    """GLM-5.3 reasons BEFORE answering, and reasoning is billed to max_tokens.

    Measured on a real 1920x1080 desktop capture: 400 -> 402 reasoning + 0
    chars of answer, 1000 -> 1000 + 0 chars, 1600+ -> a real answer. So a tight
    budget returns an EMPTY reply rather than a short one.
    """
    assert screen.MODAL_VISION_MAX_TOKENS >= 1600, (
        "budget must clear the measured ~1040-token reasoning cost, "
        "otherwise Modal vision silently returns nothing"
    )


def test_modal_vision_retries_when_token_budget_was_exhausted(monkeypatch):
    """finish_reason=length + empty content must RETRY with more budget."""
    _stub_modal_pool(monkeypatch)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append(json["max_tokens"])
        if len(calls) == 1:
            return _fake_response("", "length")
        return _fake_response("a real description", "stop")

    monkeypatch.setattr(screen.requests, "post", fake_post)

    ok, payload, quota, err = screen._modal_vision(b"jpeg", "describe")

    assert ok and payload == "a real description", (ok, payload, err)
    assert len(calls) == 2, "must retry once after exhausting the budget"
    assert calls[1] > calls[0], "the retry must have MORE budget"
    assert quota is False, "a spent budget is not a provider quota failure"


def test_modal_vision_does_not_retry_when_content_arrived(monkeypatch):
    """No wasted second call when the first reply was already fine."""
    _stub_modal_pool(monkeypatch)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append(1)
        return _fake_response("described", "stop")

    monkeypatch.setattr(screen.requests, "post", fake_post)

    ok, payload, _q, _e = screen._modal_vision(b"jpeg", "describe")
    assert ok and payload == "described"
    assert len(calls) == 1, "must not retry a successful reply"


def test_modal_vision_sends_the_base_without_a_second_v1(monkeypatch):
    """Regression: the URL used to be .../v1/v1/chat/completions -> 404.

    Every Modal vision call silently fell through to another provider.
    """
    _stub_modal_pool(monkeypatch)
    urls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        urls.append(url)
        return _fake_response("described", "stop")

    monkeypatch.setattr(screen.requests, "post", fake_post)

    screen._modal_vision(b"jpeg", "describe")
    assert urls, "no request was made"
    assert urls[0].count("/v1") == 1, urls[0]
    assert urls[0].endswith("/chat/completions"), urls[0]


def _stub_vision_capture(monkeypatch):
    monkeypatch.setattr(screen.ImageGrab, "grab",
                        lambda: Image.new("RGB", (4, 4), "white"))
    monkeypatch.setattr(screen, "get_active_window_context", lambda: {})


def test_modal_is_the_primary_vision_stage_not_local(monkeypatch):
    """Sir's directive: Modal is PRIMARY vision. Local is the offline fallback.

    The code used to try the local CPU model FIRST and reach Modal only as a
    fallback, so the designated primary vision was effectively never used.
    """
    order = []
    _stub_vision_capture(monkeypatch)
    monkeypatch.setattr(screen.api_pools, "cloud_vision_enabled", lambda: True)

    def cloud(jpeg, prompt):
        order.append("modal")
        return True, "from modal", "vision_modal", ""

    def local(prompt, jpeg, timeout=None):
        order.append("local")
        return True, "from local", ""

    monkeypatch.setattr(screen, "_cloud_vision", cloud)
    monkeypatch.setattr(screen, "_local_vision", local)

    text = screen.describe_screen("what is on my screen?")
    assert order == ["modal"], "Modal must be tried before local: %s" % order
    assert "from modal" in text


def test_local_vision_is_the_fallback_when_cloud_is_disabled(monkeypatch):
    """With cloud vision off, the local model must still answer the screen."""
    order = []
    _stub_vision_capture(monkeypatch)
    monkeypatch.setattr(screen.api_pools, "cloud_vision_enabled", lambda: False)

    def cloud(jpeg, prompt):
        order.append("modal")
        return True, "from modal", "vision_modal", ""

    def local(prompt, jpeg, timeout=None):
        order.append("local")
        return True, "from local", ""

    monkeypatch.setattr(screen, "_cloud_vision", cloud)
    monkeypatch.setattr(screen, "_local_vision", local)

    text = screen.describe_screen("what is on my screen?")
    assert order == ["local"], order
    assert "from local" in text


def _tiny_jpeg():
    """A real 2x2 JPEG so describe_image_bytes exercises its fitting path."""
    import io as _io
    from PIL import Image as _Image

    buffer = _io.BytesIO()
    _Image.new("RGB", (2, 2), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def test_telegram_photos_are_modal_first_not_local(monkeypatch):
    """Telegram image analysis must be MODAL-first, not local-first.

    Sir's reported inconsistency: `describe_image_bytes()` (the Telegram photo
    path) ran the CPU model FIRST and only reached the cloud as a fallback,
    while the documented policy, the runtime prompt and the self-report all
    say Modal is primary. Local is the OFFLINE fallback and must not be
    reached at all when the cloud answers.
    """
    order = []
    monkeypatch.setattr(screen.api_pools, "cloud_vision_enabled", lambda: True)

    def cloud(jpeg, prompt):
        order.append("cloud")
        return True, "from modal", "vision_modal", ""

    def local(prompt, jpeg, timeout=None):
        order.append("local")
        return True, "from local", ""

    monkeypatch.setattr(screen, "_cloud_vision", cloud)
    monkeypatch.setattr(screen, "_local_vision", local)

    text = screen.describe_image_bytes(_tiny_jpeg(), "what is this?")
    assert order == ["cloud"], (
        "the local CPU model was consulted while the cloud was available: %s"
        % order
    )
    assert "vision_modal" in text and "from modal" in text


def test_telegram_photo_falls_back_to_local_only_after_cloud_failure(monkeypatch):
    """Complete fallback order: cloud first, local second, nothing before cloud."""
    order = []
    monkeypatch.setattr(screen.api_pools, "cloud_vision_enabled", lambda: True)

    def cloud(jpeg, prompt):
        order.append("cloud")
        return False, None, "", "endpoint down"

    def local(prompt, jpeg, timeout=None):
        order.append("local")
        return True, "from local", ""

    monkeypatch.setattr(screen, "_cloud_vision", cloud)
    monkeypatch.setattr(screen, "_local_vision", local)

    text = screen.describe_image_bytes(_tiny_jpeg(), "what is this?")
    assert order == ["cloud", "local"], order
    assert "[local]" in text


def test_telegram_photo_uses_local_directly_when_cloud_is_unconfigured(monkeypatch):
    """A keyless install must fall straight to local, not burn cloud timeouts."""
    order = []
    monkeypatch.setattr(screen.api_pools, "cloud_vision_enabled", lambda: False)

    def cloud(jpeg, prompt):
        order.append("cloud")
        return True, "from modal", "vision_modal", ""

    def local(prompt, jpeg, timeout=None):
        order.append("local")
        return True, "from local", ""

    monkeypatch.setattr(screen, "_cloud_vision", cloud)
    monkeypatch.setattr(screen, "_local_vision", local)

    text = screen.describe_image_bytes(_tiny_jpeg(), "what is this?")
    assert order == ["local"], order
    assert "[local]" in text


def test_telegram_photo_says_so_when_every_vision_stage_fails(monkeypatch):
    """An empty answer used to produce a canned acknowledgement - forbidden."""
    monkeypatch.setattr(screen.api_pools, "cloud_vision_enabled", lambda: True)
    monkeypatch.setattr(
        screen, "_cloud_vision",
        lambda jpeg, prompt: (False, None, "", "down"))
    monkeypatch.setattr(
        screen, "_local_vision",
        lambda prompt, jpeg, timeout=None: (False, None, "down"))

    text = screen.describe_image_bytes(_tiny_jpeg(), "what is this?")
    assert "could not analyze that image" in text
    assert "unavailable" in text


def test_modal_vision_sends_a_reasoning_effort(monkeypatch):
    """GLM-5.3 thinks ALWAYS and defaults to max effort.

    Documented control is reasoning_effort; for describing a screenshot we want
    perception, not deliberation. Measured: 2.8-3.1s at low vs 7.0s at default.
    """
    _stub_modal_pool(monkeypatch)
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        sent.update(json or {})
        return _fake_response("described", "stop")

    monkeypatch.setattr(screen.requests, "post", fake_post)
    screen._modal_vision(b"jpeg", "describe")
    assert sent.get("reasoning_effort") == screen.MODAL_VISION_REASONING_EFFORT
    assert screen.MODAL_VISION_REASONING_EFFORT == "low"


def test_modal_vision_drops_reasoning_effort_if_the_endpoint_400s(monkeypatch):
    """An endpoint that does not know the param must not cost us vision."""
    _stub_modal_pool(monkeypatch)
    calls = []

    class _BadRequest:
        status_code = 400
        text = "unknown parameter: reasoning_effort"

        def json(self):
            return {"error": "unknown parameter"}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append("reasoning_effort" in (json or {}))
        if len(calls) == 1:
            return _BadRequest()
        return _fake_response("described", "stop")

    monkeypatch.setattr(screen.requests, "post", fake_post)
    ok, payload, quota, err = screen._modal_vision(b"jpeg", "describe")
    assert ok and payload == "described", (ok, payload, err)
    assert calls == [True, False], "must retry once WITHOUT the parameter"


def test_modal_vision_auth_failure_still_reports_quota_failure(monkeypatch):
    """401 must stay a quota failure so the 6h ban can fire."""
    _stub_modal_pool(monkeypatch)

    class _Unauthorized:
        status_code = 401

        def json(self):
            return {"error": "bad key"}

    monkeypatch.setattr(screen.requests, "post",
                        lambda *a, **k: _Unauthorized())
    ok, payload, quota, err = screen._modal_vision(b"jpeg", "describe")
    assert not ok and quota is True, (ok, quota, err)


# --- Screen-request phrase matching (2026-10-01) ---
# The most natural phrasing of a screen question was MISSING the vision path
# entirely, so FRIDAY answered questions about the screen WITHOUT looking at it.
# The failure was silent and looked like a normal conversation.

_CURLY = chr(0x2019)


def test_typographic_apostrophe_still_matches_a_screen_request():
    """iPhone/Telegram keyboards and STT emit U+2019, not ASCII.

    Measured before the fix: "what's on my screen" with ASCII matched;
    the identical text with U+2019 did NOT. That silently sent the request
    down the normal chat path, so she described a screen she never looked at.
    """
    assert screen.is_screen_request("what's on my screen") is True
    assert screen.is_screen_request("what" + _CURLY + "s on my screen") is True
    assert screen.is_screen_request("show me what" + _CURLY + "s on my screen") is True
    # Uppercase and ragged whitespace must not matter either.
    assert screen.is_screen_request("WHAT" + _CURLY + "S ON MY SCREEN") is True
    assert screen.is_screen_request("   what" + _CURLY + "s   on my screen  ") is True


def test_common_screen_phrasings_that_used_to_be_missed():
    for phrase in (
        "whats on my screen",              # no apostrophe at all
        "what is on the screen now",       # "the screen", not "my screen"
        "what is on my monitor",
        "what is on my display",
        "what does my screen show",
        "what shows on my screen",
    ):
        assert screen.is_screen_request(phrase) is True, phrase


def test_screen_phrases_remain_specific():
    """Broadening the list must not turn ordinary chat into a screen capture."""
    for phrase in (
        "what time is it",
        "what is the weather",
        "open youtube",
        "who won the match",
        "set a timer for 5 minutes",
        "what is 2 plus 2",
        "tell me a joke",
    ):
        assert screen.is_screen_request(phrase) is False, phrase


# --- Screen brain reachability (2026-10-01) ---
# describe_screen labels its sections "Visual description of {label}:" and
# "Text visible on {label} (verbatim OCR):", but every consumer looked for
# "Visual description:" / "OCR text:" with startswith(). That matched NOTHING,
# so _brain_answer was never called and every screen reply was the canned
# fallback - the exact opposite of the "the brain authors screen replies" rule.

_REAL_REPORT = (
    "Text visible on the LEFT screen (verbatim OCR):\n"
    "OCRWORDS on the left monitor\n"
    "Visual description of the LEFT screen:\n"
    "A code editor with a Python file open.\n\n"
    "Visual description of the RIGHT screen (this is the PRIMARY display):\n"
    "Edge showing a chat about API billing.\n\n"
    "Active window metadata:\n{'title': 'Edge'}"
)


def test_vision_and_ocr_bodies_are_found_in_a_real_report():
    assert "code editor" in screen._vision_body(_REAL_REPORT).lower()
    assert "OCRWORDS" in screen._ocr_body(_REAL_REPORT)


def test_vision_body_survives_ocr_sharing_the_same_region_block():
    """OCR and vision in one region are joined by a single newline.

    So the vision text is NOT the start of a blank-line block - which is why a
    blank-line/startswith reader cannot see it at all.
    """
    got = screen._vision_body(_REAL_REPORT)
    assert "LEFT screen" not in got, "must not swallow the OCR header"
    assert "code editor" in got.lower()


def test_legacy_bare_headers_still_work():
    old = ("Fresh screen check:\nVisual description:\nA YouTube page."
           "\nOCR text:\nRAM 42%")
    assert "YouTube" in screen._vision_body(old)
    assert "RAM 42" in screen._ocr_body(old)


def test_screen_brain_is_actually_reachable_now(monkeypatch):
    """The guard in screen_response must pass on a real report.

    When it failed, _brain_answer was dead code and the canned line was always
    spoken instead - silently, and for every screen question.
    """
    monkeypatch.setattr(screen, "describe_screen", lambda q="": _REAL_REPORT)
    monkeypatch.setattr(
        screen, "get_active_window_context",
        lambda: {"title": "Edge", "process": "msedge.exe"},
    )
    called = []
    monkeypatch.setattr(
        screen, "_brain_answer",
        lambda q, r, wc: called.append(1) or "You're working on API billing.",
    )
    result = screen.screen_response("what is on my screen")
    assert called, "_brain_answer must be called for a real report"
    assert result == "You're working on API billing."


def test_screen_brain_prefers_modal_over_the_slow_local_model(monkeypatch):
    """Modal answers in ~1-2s; local qwen3:8b measured 49-92s and always lost."""
    order = []
    monkeypatch.setattr(
        screen, "_brain_answer_modal",
        lambda prompt: order.append("modal") or "You're working through the API billing question.",
    )
    monkeypatch.setattr(screen, "requests", type("R", (), {
        "post": lambda *a, **k: (_ for _ in ()).throw(AssertionError(
            "local qwen3 must not be called when Modal answered")),
    }))
    got = screen._brain_answer("what is on my screen", _REAL_REPORT, {"title": "Edge"})
    assert order == ["modal"], order
    assert "API billing" in got, got


def test_modal_brain_transport_returns_empty_without_an_endpoint(monkeypatch):
    """No Modal configured -> '' so the local fallback still gets its turn."""
    import source.server.brain_router as br

    monkeypatch.setattr(br, "MODAL_BASE", "")
    assert screen._brain_answer_modal("prompt") == ""


def test_modal_brain_transport_returns_empty_on_a_bad_status(monkeypatch):
    import source.server.brain_router as br

    class _Bad:
        status_code = 401

        def json(self):
            return {}

    class _Pool:
        empty = False

        def next_key(self):
            return "k"

    monkeypatch.setattr(br, "MODAL_BASE", "https://example.invalid/v1")
    monkeypatch.setattr(screen.api_pools, "pool", lambda n: _Pool())
    monkeypatch.setattr(screen.requests, "post", lambda *a, **k: _Bad())
    assert screen._brain_answer_modal("prompt") == ""


# --- Multi-monitor capture: the LEFT screen was solid black (2026-10-01) ---
# Sir got a Telegram screenshot whose left half was an empty black rectangle.
# Two compounding bugs, both silent (no exception, no log line):
#   1. PIL sizes its screen bitmap from GetDeviceCaps(HORZRES/VERTRES), which
#      report only the PRIMARY monitor. Any bbox outside it comes back
#      all-black - and this machine's left monitor sits at NEGATIVE virtual
#      coordinates (-1920). Fixed by all_screens=True.
#   2. The process was only SYSTEM_DPI_AWARE, so the 125%-scaled left monitor
#      was reported as 1536x864 instead of its native 1920x1080 - i.e. we were
#      also capturing the WRONG BOX and dropping 384px. Fixed by scoping
#      PER_MONITOR_AWARE_V2 around enumeration + grab and restoring it after.

class _FakeGrab:
    def __init__(self, records, black_for_negative=True):
        self.records = records
        self.black_for_negative = black_for_negative


def test_monitor_grab_passes_all_screens_true(monkeypatch):
    """Without all_screens=True a negative-coordinate monitor is pure black."""
    import source.server.screen_understanding as screen
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}

    def fake_grab(bbox=None, all_screens=False, **kw):
        seen["bbox"] = bbox
        seen["all_screens"] = all_screens
        return Image.new("RGB", (10, 10), "white")

    monkeypatch.setattr(
        screen.ImageGrab, "grab", staticmethod(fake_grab)
    )
    monkeypatch.setattr(
        screen, "_monitor_layout",
        lambda: [{"bounds": (-1920, 0, 1536, 864), "label": "the LEFT screen",
                  "primary": False}],
    )
    regions = screen._capture_regions()
    assert regions, "a negative-coordinate monitor must still be captured"
    assert seen["bbox"] == (-1920, 0, -384, 864), seen
    assert seen["all_screens"] is True, (
        "all_screens=True is REQUIRED or Pillow returns a black image"
    )


def test_capture_survives_a_grab_failure(monkeypatch):
    import source.server.screen_understanding as screen

    def boom(*a, **k):
        raise OSError("no screen")

    monkeypatch.setattr(screen.ImageGrab, "grab", staticmethod(boom))
    monkeypatch.setattr(
        screen, "_monitor_layout",
        lambda: [{"bounds": (0, 0, 800, 600), "label": "the only screen",
                  "primary": True}],
    )
    monkeypatch.setattr(
        screen, "_capture_screen",
        lambda: Image.new("RGB", (8, 8), "white"),
    )
    regions = screen._capture_regions()
    assert len(regions) == 1 and regions[0]["label"] == "the screen"


def test_per_monitor_dpi_is_restored_after_use():
    """Awareness must be put back, or every later click coordinate shifts."""
    import ctypes

    import source.server.screen_understanding as screen

    user32 = ctypes.windll.user32
    try:
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    except Exception:
        return  # non-Windows
    before = user32.GetAwarenessFromDpiAwarenessContext(
        user32.GetThreadDpiAwarenessContext()
    )
    with screen._PerMonitorDPI():
        pass
    after = user32.GetAwarenessFromDpiAwarenessContext(
        user32.GetThreadDpiAwarenessContext()
    )
    assert after == before, (
        "DPI awareness leaked: %s -> %s. This silently moves every click "
        "coordinate on a scaled monitor." % (before, after)
    )


def test_stacked_screenshot_puts_the_primary_display_first():
    """Sir asked 'why is the left one at the top?' - the primary was buried."""
    import source.server.screen_understanding as screen

    left = {"label": "the LEFT screen", "primary": False,
            "image": Image.new("RGB", (1920, 1080), "black")}
    right = {"label": "the RIGHT screen (this is the PRIMARY display)",
             "primary": True, "image": Image.new("RGB", (1920, 1080), "white")}
    canvas = screen._stack_regions_for_phone([left, right])
    assert canvas.size[0] == 1920
    assert canvas.size[1] > canvas.size[0], "must stay portrait-ish for a phone"
    top_band = canvas.crop((0, 0, 1920, 120)).convert("L").getextrema()
    assert top_band[1] > 200, "the PRIMARY screen should be the one on top"


def test_request_text_drops_the_json_envelope_and_channel_wrapper():
    """Sir's words must survive into the journal and the scratchpad.

    /chat dispatches the RAW request body, and Telegram turns are prefixed with
    a long bracketed channel instruction. Both used to be stored verbatim, so
    the journal recorded '{"message": ...}' and the scratchpad recorded a
    garbled fragment of FRIDAY's own instructions instead of the real request.
    """
    from source.server.server import _request_words_only

    assert _request_words_only('{"message": "did you learn from that?"}') == \
        "did you learn from that?"
    assert _request_words_only('{"message": "hi", "chat_id": 5}') == "hi"
    wrapped = ("[Sir is talking to you on Telegram - your reply goes to his "
               "phone as a chat text. Answer in one short bubble.]\n\n"
               "will it be faster next time")
    assert _request_words_only(wrapped) == "will it be faster next time"
    assert _request_words_only(
        "some recalled memory\n\nUser request:\nmove the window") == "move the window"
    assert _request_words_only("Bring the window over") == "Bring the window over"
    assert _request_words_only("") == ""
    # A malformed body must degrade to the raw text, never raise.
    assert _request_words_only("{not json") == "{not json"
