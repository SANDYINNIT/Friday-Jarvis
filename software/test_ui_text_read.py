"""Accessibility-first reading, input targeting, and staleness.

Sir's request 2026-11-02: FRIDAY should read long app output and locate the
right input box from the accessibility tree (as ChatGPT did through Windows UI
Automation/TextPattern) instead of guessing coordinates from screenshots, and a
stale frame or stale element must never be able to trigger a click.

Screen history itself is deliberately untouched by this work - see
`test_screen_history.py` - and one of these tests pins that the vision mode of
`describe_recent_screen` now actually works rather than silently failing.
"""

import time

from source.server import ui_automation as ua
from source.server.ui_automation import UIAutomation


# ---------------------------------------------------------------------------
# Fakes. A control WITHOUT iface_text is the normal case (no Text pattern) and
# is exactly how a canvas/remote app behaves; one WITH it stands in for a real
# accessible document.
# ---------------------------------------------------------------------------
class _Doc:
    def __init__(self, text):
        self._text = text

    def GetText(self, _what):
        return self._text


class _Pattern:
    def __init__(self, text):
        self.DocumentRange = _Doc(text)


class FakeControl:
    def __init__(self, name, control_type="Button", automation_id="", value="",
                 text=None):
        self.name = name
        self.window_text = name
        self.control_type = control_type
        self.automation_id = automation_id
        self.value = value
        self.handle = None
        self.is_enabled = True
        self.is_visible = True
        self.calls = []
        info = type("Info", (), {"runtime_id": (id(self),)})()
        info.name = name
        info.control_type = control_type
        info.automation_id = automation_id
        self.element_info = info
        if text is not None:
            self.iface_text = _Pattern(text)

    def click_input(self):
        self.calls.append("click")
        self.value = "clicked"

    def invoke(self):
        self.calls.append("invoke")
        self.value = "invoked"

    def type_keys(self, value):
        self.calls.append(("type", value))
        self.value += value

    def set_edit_text(self, value):
        self.calls.append(("set_value", value))
        self.value = value


class FakeWindow:
    handle = 10
    window_text = "Editor"
    name = "Editor"
    control_type = "Window"
    automation_id = "editor"
    process_id = 99

    def __init__(self, controls):
        self._controls = controls

    def descendants(self):
        return self._controls


class FakeProvider:
    def __init__(self, windows):
        self._windows = windows

    def windows(self, **kwargs):
        return self._windows


def _reader(controls, title="Editor"):
    window = FakeWindow(controls)
    window.window_text = title
    return UIAutomation(FakeProvider([window]))


# ---------------------------------------------------------------------------
# Long output, read in bounded chunks (no scrolling required)
# ---------------------------------------------------------------------------
def test_long_output_is_read_in_bounded_chunks():
    body = "\n".join("line %04d of a very long program output" % i
                     for i in range(400))            # ~11k chars
    ui = _reader([FakeControl("Document", control_type="Document", text=body)])

    first = ui.read_text(10, offset=0, limit=500)
    assert first["available"] is True
    assert first["mechanism"] == "uia_textpattern"
    assert first["total"] == len(body)
    assert len(first["text"]) == 500
    assert first["complete"] is False
    assert first["offset"] == 0

    # Walk the whole thing the way a caller must, and prove it reassembles.
    chunks, offset, guard = [], 0, 0
    while True:
        page = ui.read_text(10, offset=offset, limit=500)
        chunks.append(page["text"])
        assert page["total"] == len(body), "total changed between reads"
        if page["complete"]:
            break
        offset = page["offset"] + page["length"]
        guard += 1
        assert guard < 100, "read never completed - chunking is broken"

    assert "".join(chunks) == body, "chunks did not reassemble into the original"
    assert len(chunks) > 1, "long output was not actually chunked"


def test_chunk_limit_is_bounded_even_when_a_caller_asks_for_everything():
    body = "x" * 50000
    ui = _reader([FakeControl("Document", control_type="Document", text=body)])
    page = ui.read_text(10, offset=0, limit=10 ** 9)
    assert len(page["text"]) == ua.READ_CHUNK, "read was not bounded"
    assert page["total"] == 50000
    assert page["complete"] is False


def test_text_is_read_without_touching_the_window():
    """Reading must not scroll, click or type - it is a pure read."""
    control = FakeControl("Document", control_type="Document", text="hello")
    ui = _reader([control])
    ui.read_text(10, offset=0, limit=10)
    ui.read_text(10, offset=5, limit=10)
    assert control.calls == [], "a read performed an action on the control"


# ---------------------------------------------------------------------------
# No accessible text -> honest fallback, never a silent empty string
# ---------------------------------------------------------------------------
def test_a_window_with_no_accessible_text_reports_a_fallback():
    # The default fakes expose no Text pattern: this is the canvas/remote case.
    ui = _reader([FakeControl("Button 0"), FakeControl("Button 1")])
    page = ui.read_text(10, offset=0, limit=500)

    assert page["available"] is True
    assert page["mechanism"] == "none"
    assert page["text"] == ""
    assert page["total"] == 0
    assert page["complete"] is True
    assert "screenshot" in page["fallback"] and "vision" in page["fallback"], (
        "an empty window and 'this exposes nothing' must not look the same: %r"
        % page["fallback"])


def test_text_nodes_are_used_when_there_is_no_document_pattern():
    ui = _reader([
        FakeControl("first sentence", control_type="Text"),
        FakeControl("second sentence", control_type="Text"),
        FakeControl("Save", control_type="Button"),
    ])
    page = ui.read_text(10, offset=0, limit=500)
    assert page["mechanism"] == "uia_text_nodes"
    assert "first sentence" in page["text"] and "second sentence" in page["text"]
    assert page["fallback"] == ""


def test_unknown_window_is_reported_as_not_found():
    ui = _reader([FakeControl("Save")])
    page = ui.read_text("No Such Window")
    assert page.get("failure_reason") == "window target is ambiguous or not found"


# ---------------------------------------------------------------------------
# Freshness + window identity (so a read cannot be applied to another window)
# ---------------------------------------------------------------------------
def test_reads_carry_their_age_and_their_window_identity():
    ui = _reader([FakeControl("Document", control_type="Document", text="hi")])
    page = ui.read_text(10, offset=0, limit=10)
    assert page["captured_at"] <= time.time() + 1.0
    assert page["captured_at"] >= time.time() - 5.0
    assert page["window"] == {"handle": 10, "title": "Editor", "process_id": 99}


# ---------------------------------------------------------------------------
# Input-box targeting by name
# ---------------------------------------------------------------------------
def test_find_inputs_returns_only_editable_controls():
    ui = _reader([
        FakeControl("Save", control_type="Button"),
        FakeControl("Prompt", control_type="Edit"),
        FakeControl("Editor body", control_type="Document"),
        FakeControl("Model", control_type="ComboBox"),
    ])
    found = ui.find_inputs(10)

    kinds = {c["control_type"] for c in found["controls"]}
    assert "Edit" in kinds and "Document" in kinds and "ComboBox" in kinds
    assert "Button" not in kinds, "a button is not somewhere you can type"
    for control in found["controls"]:
        assert control.get("index") is not None, "act(control_index=) needs an index"


def test_find_inputs_matches_a_name_substring():
    ui = _reader([
        FakeControl("Prompt", control_type="Edit"),
        FakeControl("Command line", control_type="Edit"),
        FakeControl("Search box", control_type="Edit"),
    ])
    named = ui.find_inputs(10, name="prompt")
    assert [c["name"] for c in named["controls"]] == ["Prompt"]


def test_find_inputs_says_so_when_there_is_no_input_box(monkeypatch):
    """'No input box here' must be stated in the layer the model actually calls."""
    from source.server import ui_probe as up

    inner = _reader([FakeControl("Save", control_type="Button")])

    class _UA:
        def UIAutomation(self, **_kwargs):
            return inner

    monkeypatch.setattr(up, "available",
                        lambda: {"available": True, "backend": "test"})
    monkeypatch.setattr(up, "find_window",
                        lambda q, limit=8: [{"handle": 10, "title": "Editor"}])
    monkeypatch.setattr(up, "_ua", _UA())

    found = up.find_inputs("Editor")
    assert found["available"] is True
    assert found["found"] is False
    assert found["controls"] == []
    assert found["reason"], "'no input box' must be stated, not implied by []"


def test_probe_read_surfaces_not_found_uniformly(monkeypatch):
    """read_window_text must map the adapter's failure shape to reason+found."""
    from source.server import ui_probe as up

    inner = _reader([FakeControl("Save")])

    class _UA:
        def UIAutomation(self, **_kwargs):
            return inner

    monkeypatch.setattr(up, "available",
                        lambda: {"available": True, "backend": "test"})
    monkeypatch.setattr(up, "find_window",
                        lambda q, limit=8: [{"handle": 999, "title": "Ghost"}])
    monkeypatch.setattr(up, "_ua", _UA())

    result = up.read_window_text("Ghost")
    # Handle 999 does not exist in the fake tree -> adapter reports not-found.
    assert result.get("found") is False
    assert result.get("reason"), "a not-found read must carry a reason key"


# ---------------------------------------------------------------------------
# Stale elements cannot act
# ---------------------------------------------------------------------------
def test_a_control_that_changed_between_read_and_act_is_refused():
    control = FakeControl("Prompt", control_type="Edit")
    ui = _reader([control])
    token = ui.find_inputs(10)["controls"][0]["target_token"]

    # The element moved/changed. `_field` prefers element_info (the UIA source
    # of truth), so mutate what the snapshot actually reads.
    control.name = "Prompt (now something else)"
    control.window_text = control.name
    control.element_info.name = control.name

    result = ui.perform("set_value", token, "text that must not land anywhere")

    assert result["success"] is False
    assert "stale" in result["failure_reason"]
    assert control.calls == [], "a stale element was acted upon"


def test_a_single_use_token_cannot_be_replayed():
    control = FakeControl("Prompt", control_type="Edit")
    ui = _reader([control])
    token = ui.find_inputs(10)["controls"][0]["target_token"]
    ui.perform("set_value", token, "first")
    second = ui.perform("set_value", token, "second")
    assert second["success"] is False
    assert "stale" in second["failure_reason"] or "token" in second["failure_reason"]


def test_focus_is_not_required_on_the_injected_headless_path():
    """Guard against the focus check breaking provider-injected tests.

    On a real desktop provider is None and the focus branch runs; with an
    injected provider the HWNDs are fakes with no foreground window at all, so
    insisting on focus would make every headless typing test fail rather than
    testing the pipeline.
    """
    control = FakeControl("Prompt", control_type="Edit")
    ui = _reader([control])
    token = ui.find_inputs(10)["controls"][0]["target_token"]
    result = ui.perform("set_value", token, "hello")
    assert result["success"] is True, result.get("failure_reason")
    assert control.value == "hello"


# ---------------------------------------------------------------------------
# Screen history: untouched, and its vision mode now actually works
# ---------------------------------------------------------------------------
def test_recent_screen_history_vision_mode_returns_text(monkeypatch):
    """Regression: this used to assign _cloud_vision's 4-tuple to `text`.

    `text.strip()` then raised AttributeError, the blanket except swallowed it,
    and mode="vision" history reads always came back ok=False - so one of the
    two history modes was silently dead.
    """
    from source.server import screen_understanding as su

    monkeypatch.setattr(su, "_history_readable_image",
                        lambda *a, **k: (4.0, object()))
    monkeypatch.setattr(su, "_encode_jpeg", lambda *a, **k: b"jpegbytes")
    monkeypatch.setattr(su, "_cloud_vision",
                        lambda *a, **k: (True, "VISION TEXT HERE",
                                         "vision_modal", ""))

    result = su.describe_recent_screen(4.0, "what is this", mode="vision")

    assert result["ok"] is True, result.get("reason")
    assert result["text"] == "VISION TEXT HERE"
    assert result["age_seconds"] == 4.0


def test_recent_screen_history_vision_failure_is_reported_not_raised(monkeypatch):
    from source.server import screen_understanding as su

    monkeypatch.setattr(su, "_history_readable_image",
                        lambda *a, **k: (4.0, object()))
    monkeypatch.setattr(su, "_encode_jpeg", lambda *a, **k: b"jpegbytes")
    monkeypatch.setattr(su, "_cloud_vision",
                        lambda *a, **k: (False, "", "", "quota exhausted"))

    result = su.describe_recent_screen(4.0, "what is this", mode="vision")

    assert result["ok"] is not True
    assert "quota exhausted" in (result.get("reason") or "")


def test_recent_screen_history_ocr_mode_is_untouched(monkeypatch):
    """The OCR path must keep working exactly as before."""
    from source.server import screen_understanding as su

    monkeypatch.setattr(su, "_history_readable_image",
                        lambda *a, **k: (4.0, object()))
    monkeypatch.setattr(su, "_encode_jpeg", lambda *a, **k: b"jpegbytes")
    monkeypatch.setattr(su, "_ocr_of_image", lambda *a, **k: "OCR TEXT")

    result = su.describe_recent_screen(4.0, "read this", mode="ocr")

    assert result["ok"] is True, result.get("reason")
    assert result["text"] == "OCR TEXT"
