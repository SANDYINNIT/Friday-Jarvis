"""Headless tests for the optional UIA boundary."""

from source.server.ui_automation import UIAutomation


class FakeControl:
    def __init__(self, name, control_type="Button", automation_id="", value=""):
        self.name = name
        self.window_text = name
        self.control_type = control_type
        self.automation_id = automation_id
        self.value = value
        self.handle = None
        self.is_enabled = True
        self.is_visible = True
        self.calls = []
        self.element_info = type("Info", (), {"runtime_id": (id(self),)})()

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


def test_dom_is_bounded_and_exactly_filters_controls():
    controls = [FakeControl(f"Button {i}") for i in range(5)]
    ui = UIAutomation(FakeProvider([FakeWindow(controls)]), control_limit=2)
    result = ui.find_controls({"title": "Editor"}, control_type="Button", name="Button 0")
    assert len(result["controls"]) == 1
    assert result["controls"][0]["target_token"]


def test_stale_token_is_rejected():
    control = FakeControl("Save")
    ui = UIAutomation(FakeProvider([FakeWindow([control])]))
    token = ui.dom("Editor")["controls"][0]["target_token"]
    control.name = "Changed"
    result = ui.perform("click", token)
    assert not result["success"]
    assert "stale" in result["failure_reason"]


def test_before_after_change_is_verified():
    control = FakeControl("Save")
    ui = UIAutomation(FakeProvider([FakeWindow([control])]))
    token = ui.dom("Editor")["controls"][0]["target_token"]
    result = ui.perform("set_value", token, "done")
    assert result["success"] and result["changed"] and result["verified"]
    assert result["before"]["value"] == ""
    assert result["after"]["value"] == "done"


def test_unavailable_backend_is_truthful():
    result = UIAutomation(provider=None)._unavailable("dom")
    assert result["available"] is False
    assert "unavailable" in result["failure_reason"]


def test_ambiguous_window_and_unknown_action_confirmation_boundary():
    ui = UIAutomation(FakeProvider([FakeWindow([]), FakeWindow([])]))
    result = ui.dom("Editor")
    assert result["failure_reason"] == "window target is ambiguous or not found"
    action = ui.perform("delete", "not-a-token")
    assert action["risk"]["confirmation_required"] is True
    assert not action["success"]


print("PASS: headless UI Automation tests")
