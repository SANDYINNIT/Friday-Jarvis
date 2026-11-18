"""Headless tests for deterministic window awareness/control."""

from unittest.mock import patch

from source.server import windows_control as windows


class FakeWindow:
    def __init__(self, handle, title, process="notepad.exe", *, minimized=False, maximized=False):
        self.handle = handle
        self.title = title
        self.pid = handle + 1000
        self.process = process
        self.isMinimized = minimized
        self.isMaximized = maximized
        self.isVisible = True
        self.left, self.top, self.width, self.height = 1, 2, 800, 600
        self.calls = []

    def getHandle(self):
        return self.handle

    def minimize(self):
        self.calls.append("minimize")
        self.isMinimized = True

    def maximize(self):
        self.calls.append("maximize")
        self.isMaximized = True

    def restore(self):
        self.calls.append("restore")
        self.isMinimized = False
        self.isMaximized = False

    def activate(self):
        self.calls.append("activate")

    def close(self):
        self.calls.append("close")
        self.closed = True

    def moveTo(self, x, y):
        self.calls.append(("moveTo", x, y))
        self.left, self.top = x, y

    def resizeTo(self, width, height):
        self.calls.append(("resizeTo", width, height))
        self.width, self.height = width, height


def _process_info(pid):
    return ("notepad.exe", r"C:\Windows\notepad.exe") if pid else ("", "")


def test_list_is_bounded_and_structured():
    items = [FakeWindow(index, f"Window {index}") for index in range(4)]
    with patch.object(windows.pywinctl, "getAllWindows", return_value=items), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.list_windows(2)

    assert len(result) == 2
    assert result[0]["handle"] == 0
    assert result[0]["bounds"] == [1, 2, 800, 600]
    assert result[0]["visible"] is True


def test_ambiguous_title_returns_candidates_without_mutation():
    items = [FakeWindow(1, "Editor"), FakeWindow(2, "Editor")]
    with patch.object(windows.pywinctl, "getAllWindows", return_value=items), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.minimize_window("Editor")

    assert result["success"] is False
    assert len(result["candidates"]) == 2
    assert items[0].calls == [] and items[1].calls == []


def test_exact_title_process_mutation_reports_before_after():
    item = FakeWindow(1, "Editor")
    with patch.object(windows.pywinctl, "getAllWindows", return_value=[item]), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.minimize_window({"title": "Editor", "process": "notepad"})

    assert result["success"] is True
    assert result["changed"] is True
    assert result["before"]["minimized"] is False
    assert result["after"]["minimized"] is True
    assert item.calls == ["minimize"]


def test_unchanged_state_is_not_claimed_as_success():
    item = FakeWindow(1, "Editor", minimized=True)
    with patch.object(windows.pywinctl, "getAllWindows", return_value=[item]), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.minimize_window({"title": "Editor", "process": "notepad.exe"})

    assert result["success"] is False
    assert result["changed"] is False
    assert item.calls == []


def test_focus_verifies_active_handle():
    item = FakeWindow(1, "Editor")
    active = {"handle": None}

    def activate():
        item.calls.append("activate")
        active["handle"] = item.handle

    item.activate = activate
    with patch.object(windows.pywinctl, "getAllWindows", return_value=[item]), patch.object(windows, "_process_info", side_effect=_process_info), patch.object(windows, "get_active_window", side_effect=lambda: dict(active)):
        result = windows.focus_window({"title": "Editor", "process": "notepad"})

    assert result["success"] is True
    assert result["changed"] is True
    assert item.calls == ["activate"]


def test_new_actions_are_denied_without_confirmation():
    item = FakeWindow(1, "Editor")
    with patch.object(windows.pywinctl, "getAllWindows", return_value=[item]), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.move_window({"title": "Editor", "process": "notepad"}, 20, 30)

    assert not result["success"]
    assert "confirmation" in result["failure_reason"]
    assert item.calls == []


def test_approved_move_is_verified_against_requested_bounds():
    item = FakeWindow(1, "Editor")
    with patch.object(windows.pywinctl, "getAllWindows", return_value=[item]), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.move_window({"title": "Editor", "process": "notepad"}, 20, 30, confirmation_callback=lambda *_: True)

    assert result["success"] and result["changed"]
    assert result["after"]["bounds"][:2] == [20, 30]


def test_close_ambiguous_target_does_not_mutate():
    items = [FakeWindow(1, "Editor"), FakeWindow(2, "Editor")]
    with patch.object(windows.pywinctl, "getAllWindows", return_value=items), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.close_window("Editor", confirmation_callback=lambda *_: True)

    assert not result["success"]
    assert len(result["candidates"]) == 2
    assert all(not item.calls for item in items)


def test_unchanged_move_fails_verification():
    item = FakeWindow(1, "Editor")
    item.moveTo = lambda x, y: item.calls.append(("moveTo", x, y))
    with patch.object(windows.pywinctl, "getAllWindows", return_value=[item]), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.move_window({"title": "Editor", "process": "notepad"}, 20, 30, confirmation_callback=lambda *_: True)

    assert not result["success"]
    assert "bounds" in result["failure_reason"]


def test_approved_close_requires_window_disappearance():
    item = FakeWindow(1, "Editor")
    def current_windows():
        return [] if getattr(item, "closed", False) else [item]

    with patch.object(windows.pywinctl, "getAllWindows", side_effect=current_windows), patch.object(windows, "_process_info", side_effect=_process_info):
        result = windows.close_window({"title": "Editor", "process": "notepad"}, confirmation_callback=lambda *_: True)

    assert result["success"]
    assert result["window_disappeared"]
    assert item.calls == ["close"]


def test_unavailable_backend_is_truthful_for_mutation():
    with patch.object(windows.pywinctl, "getAllWindows", side_effect=OSError("backend missing")):
        result = windows.resize_window({"title": "Editor", "process": "notepad"}, 100, 100, confirmation_callback=lambda *_: True)

    assert not result["success"]
    assert result["available"] is False
    assert "unavailable" in result["failure_reason"]


def test_terminate_uses_exact_pid_and_process_api():
    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid
            self.running = True

        def name(self):
            return "notepad.exe"

        def exe(self):
            return r"C:\Windows\notepad.exe"

        def is_running(self):
            return self.running

        def terminate(self):
            self.running = False

    process = FakeProcess(4321)
    with patch.object(windows.psutil, "Process", return_value=process) as process_factory:
        result = windows.terminate_process({"pid": 4321, "process": "notepad.exe"}, confirmation_callback=lambda *_: True)

    process_factory.assert_called_once_with(4321)
    assert result["success"]


print("PASS: headless Windows window awareness/control tests")
