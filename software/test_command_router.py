"""WP10: risk-gated command router tests."""
import pytest

from source.server import command_router as router


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch):
    monkeypatch.setattr(router, "log_action", lambda **kwargs: None)


def test_list_windows_is_read_only(monkeypatch):
    monkeypatch.setattr(router, "list_windows", lambda limit=100: [
        {"title": "Notepad - notes", "process": "notepad.exe"},
        {"title": "Visual Studio Code", "process": "Code.exe"},
    ])
    for text in ("what windows are open", "list windows", "show me the windows"):
        result = router.route(text, actor="voice")
        assert result is not None
        assert result["action"] == "list_windows"
        assert result["risk"] == router.RISK_READ_ONLY
        assert "Notepad" in result["response"]


def test_natural_conversation_passes_through():
    for text in ("what time is it", "tell me a joke", "how does the weather look"):
        assert router.route(text, actor="voice") is None


def test_destructive_phrasing_is_refused():
    for text in ("quit the browser", "kill that window", "terminate the app",
                 "exit the notepad", "shut down the app"):
        result = router.route(text, actor="voice")
        assert result is not None
        assert result["action"] == "refuse"
        assert result["risk"] == router.RISK_DESTRUCTIVE
        assert result["allowed"] is False


def test_open_close_now_routes_to_the_brain_per_user_directive():
    """NEW contract (user-driven): app open/close is NOT deterministic — the
    brain authors its own python every time. The router must fall through."""
    assert router.route("close spotify friday", actor="voice") is None
    assert router.route("open spotify", actor="voice") is None
    # destructive wording stays guarded
    result = router.route("kill that window", actor="voice")
    assert result is not None
    assert result["action"] == "refuse"


def test_minimize_ambiguous_never_mutates(monkeypatch):
    def fake_minimize(query, *, exclude_dev=True):
        return {"success": False, "changed": False, "before": None, "after": None,
                "failure_reason": "several windows match that description",
                "candidates": [{"title": "A", "process": "x"}, {"title": "B", "process": "y"}]}

    monkeypatch.setattr(router, "minimize_by_query", fake_minimize)
    result = router.route("minimize the editor", actor="voice")
    assert result is not None
    assert result["action"] == "minimize"
    assert result["changed"] is False
    assert "more than one window" in result["response"]


def test_minimize_already_minimized_reports_truthfully(monkeypatch):
    def fake_minimize(query, *, exclude_dev=True):
        return {"success": False, "changed": False, "before": None, "after": None,
                "failure_reason": "window is already minimized", "candidates": []}

    monkeypatch.setattr(router, "minimize_by_query", fake_minimize)
    result = router.route("minimize notepad", actor="voice")
    assert result is not None
    assert result["action"] == "minimize"
    assert result["changed"] is False
    assert "already minimized" in result["response"]


def test_focus_confident_unique_acts_and_verifies(monkeypatch):
    def fake_focus(query, *, exclude_dev=True):
        return {"success": True, "changed": True, "before": {}, "after": {"title": "Notepad"}}

    monkeypatch.setattr(router, "focus_by_query", fake_focus)
    result = router.route("focus notepad", actor="voice")
    assert result["handled"] is True
    assert result["action"] == "focus"
    assert result["allowed"] is True
    assert result["changed"] is True
    assert "Notepad" in result["response"]


def test_focus_ambiguous_never_mutates(monkeypatch):
    def fake_focus(query, *, exclude_dev=True):
        return {"success": False, "changed": False, "before": None, "after": None,
                "failure_reason": "several windows match that description",
                "candidates": [{"title": "A", "process": "x"}, {"title": "B", "process": "y"}]}

    monkeypatch.setattr(router, "focus_by_query", fake_focus)
    result = router.route("focus editor", actor="voice")
    assert result["handled"] is True
    assert result["changed"] is False
    assert "more than one window" in result["response"]


def test_focus_failure_reports_truthfully(monkeypatch):
    def fake_focus(query, *, exclude_dev=True):
        return {"success": False, "changed": False, "before": None, "after": None,
                "failure_reason": "no window matches that description", "candidates": []}

    monkeypatch.setattr(router, "focus_by_query", fake_focus)
    result = router.route("focus zebra", actor="voice")
    assert result["handled"] is True
    assert "couldn't focus" in result["response"]


def test_type_requires_for_me(monkeypatch):
    import pyautogui

    written = []

    def fake_write(text, interval=0.03):
        written.append(text)

    monkeypatch.setattr(pyautogui, "write", fake_write)
    result = router.route("type hello world for me", actor="voice")
    assert result is not None
    assert result["action"] == "type"
    assert result["changed"] is True
    assert written == ["hello world"]
    # Without the explicit "for me" tail, dictation is not hijacked.
    assert router.route("type hello world", actor="voice") is None


def test_oversized_dictation_is_refused(monkeypatch):
    long_text = "word " * 500
    result = router.route("type " + long_text + " for me", actor="voice")
    assert result is not None
    assert result["action"] == "type"
    assert result["allowed"] is False