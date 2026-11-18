"""LAB-3R7: /open_app must resume presence=open (window visible == open).

The HTTP raise path never set presence, so a persisted away pause left the
town frozen while the HUD was on screen - the user's 'characters appear
stationary' after restart. The tray _show_app already did this; the route
must match it."""
import ast
import pathlib
import types

import pytest

SERVER = pathlib.Path(__file__).parent / "source" / "server" / "server.py"


def _route_body(name):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SERVER.read_text(encoding="utf-8"), node)
    raise AssertionError("route %s not found" % name)


def test_open_app_route_sets_presence_open():
    body = _route_body("open_app")
    assert "_guard(request)" in body, "guard must stay first"
    assert 'set_presence("open")' in body, (
        "/open_app must resume presence=open like the tray _show_app path")


def test_open_app_no_window_does_not_touch_presence():
    # the early 'no window' return happens BEFORE the presence call
    body = _route_body("open_app")
    first_return = body.index('return PlainTextResponse("no window")')
    presence = body.index('set_presence("open")')
    assert first_return < presence, (
        "presence must only change when a window was actually raised")


@pytest.mark.parametrize("denied,hwnd,expected", [(True, 7, "denied"), (False, 0, "no window"),
                                                (False, 7, "FRIDAY window raised")])
def test_actual_open_route_records_presentation_only_after_show(monkeypatch, denied, hwnd, expected):
    """Run the shipped route body without starting the server or importing its devices."""
    import asyncio
    import sys
    import source.server as package

    effects = []
    lab = types.SimpleNamespace(set_presence=lambda state: effects.append(("presence", state)))
    monkeypatch.setitem(sys.modules, "source.server.lab_state", lab)
    monkeypatch.setattr(package, "lab_state", lab, raising=False)
    monkeypatch.setitem(sys.modules, "source.server.status_ui", types.SimpleNamespace(
        note_window_open=lambda: effects.append(("opening",))))
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(SW_RESTORE=9, SW_SHOW=5))
    monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(
        FindWindow=lambda *_: hwnd,
        ShowWindow=lambda handle, action: effects.append(("show", handle, action)),
        SetForegroundWindow=lambda *_: None))
    node = ast.parse(_route_body("open_app")).body[0]
    node.decorator_list = []
    namespace = {"__name__": "source.server.server", "__package__": "source.server",
                 "Request": object, "PlainTextResponse": lambda text: text,
                 "_guard": lambda _: "denied" if denied else None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 str(SERVER), "exec"), namespace)
    assert asyncio.run(namespace["open_app"](object())) == expected
    if expected == "FRIDAY window raised":
        assert effects == [("show", 7, 9), ("show", 7, 5), ("presence", "open"), ("opening",)]
    else:
        assert effects == []
