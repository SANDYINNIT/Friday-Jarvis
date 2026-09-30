"""WP4/WP9: fuzzy window resolution, dev-filter, and safe focus."""
from source.server import windows_control as wc


class FakeWindow:
    def __init__(self, handle, title, process, exe):
        self.handle = handle
        self.title = title
        self.pid = handle
        self._process = process
        self._exe = exe
        self.left = 0
        self.top = 0
        self.width = 800
        self.height = 600
        self.isMinimized = False
        self.isMaximized = False
        self.isVisible = True

    def getHandle(self):
        return self.handle


WINDOWS = [
    FakeWindow(10, "Notepad - notes.txt", "notepad.exe", "C:\\x\\notepad.exe"),
    FakeWindow(20, "Visual Studio Code - app.py", "Code.exe", "C:\\x\\Code.exe"),
    FakeWindow(30, "PowerShell", "powershell.exe", "C:\\x\\powershell.exe"),
    FakeWindow(40, "Edge - Docs", "msedge.exe", "C:\\x\\msedge.exe"),
    FakeWindow(50, "Edge - YouTube", "msedge.exe", "C:\\x\\msedge.exe"),
]


def _patch(monkeypatch):
    by_pid = {win.pid: (win._process, win._exe) for win in WINDOWS}

    def fake_process_info(pid):
        return by_pid.get(pid, ("", ""))

    monkeypatch.setattr(wc, "_windows_status", lambda: (list(WINDOWS), None))
    monkeypatch.setattr(wc, "_process_info", fake_process_info)


def test_fuzzy_score_matches_and_misses():
    snapshot = wc._snapshot(WINDOWS[0])
    assert wc._fuzzy_score(snapshot, wc._tokenize("notepad")) > 0.4
    assert wc._fuzzy_score(snapshot, wc._tokenize("zebra")) == 0.0


def test_dev_window_filter():
    assert wc._is_dev_window(wc._snapshot(WINDOWS[2])) is True
    assert wc._is_dev_window(wc._snapshot(WINDOWS[0])) is False


def test_find_windows_ranks_and_excludes_dev(monkeypatch):
    _patch(monkeypatch)
    results = wc.find_windows("notepad")
    assert results and results[0]["title"] == "Notepad - notes.txt"
    assert all(item["process"] != "powershell.exe" for item in results)


def test_find_windows_powershell_is_dev_excluded(monkeypatch):
    _patch(monkeypatch)
    results = wc.find_windows("powershell")
    assert results == []


def test_resolve_fuzzy_unique(monkeypatch):
    _patch(monkeypatch)
    match, failure = wc.resolve_fuzzy("notepad")
    assert failure is None
    assert match["handle"] == 10


def test_resolve_fuzzy_ambiguous(monkeypatch):
    _patch(monkeypatch)
    match, failure = wc.resolve_fuzzy("edge")
    assert match is None
    assert failure is not None
    assert len(failure["candidates"]) >= 2


def test_focus_by_query_ambiguous_does_not_mutate(monkeypatch):
    _patch(monkeypatch)
    called = []

    def fake_focus(target):
        called.append(target)
        return {"success": True, "changed": True}

    monkeypatch.setattr(wc, "focus_window", fake_focus)
    result = wc.focus_by_query("edge")
    assert called == []
    assert result["success"] is False
    assert result["candidates"]


def test_focus_by_query_unique_calls_focus_window(monkeypatch):
    _patch(monkeypatch)
    captured = {}

    def fake_focus(target):
        captured["target"] = target
        return {"success": True, "changed": True, "before": {}, "after": {}}

    monkeypatch.setattr(wc, "focus_window", fake_focus)
    result = wc.focus_by_query("notepad")
    assert result == {"success": True, "changed": True, "before": {}, "after": {}}
    assert captured["target"]["handle"] == 10
    assert captured["target"]["title"] == "Notepad - notes.txt"


def test_minimize_by_query_ambiguous_does_not_mutate(monkeypatch):
    _patch(monkeypatch)
    called = []

    def fake_minimize(target):
        called.append(target)
        return {"success": True, "changed": True}

    monkeypatch.setattr(wc, "minimize_window", fake_minimize)
    result = wc.minimize_by_query("edge")
    assert called == []
    assert result["success"] is False
    assert result["candidates"]


def test_minimize_by_query_unique_calls_minimize_window(monkeypatch):
    _patch(monkeypatch)
    captured = {}

    def fake_minimize(target):
        captured["target"] = target
        return {"success": True, "changed": True, "before": {}, "after": {}}

    monkeypatch.setattr(wc, "minimize_window", fake_minimize)
    result = wc.minimize_by_query("notepad")
    assert result == {"success": True, "changed": True, "before": {}, "after": {}}
    assert captured["target"]["handle"] == 10
    assert captured["target"]["title"] == "Notepad - notes.txt"