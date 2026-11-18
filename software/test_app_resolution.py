"""Priority-2 app resolution: installed apps before PATH, honest ambiguity.

Bugs proven from the 2026-11-02 logs that this covers:

* "Hey Friday open, open code" launched the npm CLI from PATH. The model's
  own script globbed Program Files and ran `where.exe opencode`; the real
  desktop app is StartApps Name=OpenCode / AppID=ai.opencode.desktop.
  `find_app_executable` never consulted StartApps/AUMID, and its
  `rstrip(".exe")` mangled the stem to "opencod", so the Program Files scan
  could never match OpenCode.exe either.
* "Friday open Microsoft Edge" was answered with webbrowser.open(google.com)
  because "edge" sat in `_BROWSER_APP_TOKENS`.

Nothing here launches an app or touches the real system: subprocess, winreg
sources and webbrowser are all monkeypatched.
"""
import inspect

import pytest

from source.server import command_router as router
from source.server import windows_control as windows


@pytest.fixture(autouse=True)
def _fresh_installed_cache():
    windows._reset_installed_cache()
    yield
    windows._reset_installed_cache()


def _fake_installed(monkeypatch, startapps=(), uninstall=()):
    """Replace both installed-app sources; no real PowerShell, no winreg."""
    monkeypatch.setattr(
        windows, "_startapps_query",
        lambda: [{"name": name, "app_id": app_id, "source": "startapps"}
                 for name, app_id in startapps],
    )
    monkeypatch.setattr(
        windows, "_uninstall_display_names",
        lambda: [{"name": name, "app_id": "", "source": "uninstall"}
                 for name in uninstall],
    )


# --- resolve_app: found / ambiguous / not_found ---------------------------- #

def test_desktop_app_beats_same_name_path_cli_shim(monkeypatch):
    """StartApps OpenCode/ai.opencode.desktop must win over a PATH npm shim."""
    _fake_installed(monkeypatch, startapps=[("OpenCode", "ai.opencode.desktop")])
    consulted = []

    def _path_ladder(query, timeout=8):
        consulted.append(query)
        return r"C:\npm\opencode.cmd", "path"

    monkeypatch.setattr(windows, "_find_app_executable_detail", _path_ladder)

    result = windows.resolve_app("opencode")

    assert result["status"] == "found"
    assert result["method"] == "startapps"
    assert result["app_id"] == "ai.opencode.desktop"
    assert result["path"] is None
    assert "opencode.cmd" not in str(result)
    assert consulted == [], "the PATH ladder must not run once StartApps matched"


def test_unknown_app_is_not_found_and_never_raises(monkeypatch):
    _fake_installed(monkeypatch)
    monkeypatch.setattr(windows, "_find_app_executable_detail",
                        lambda query, timeout=8: (None, None))

    result = windows.resolve_app("definitely not installed xyzq")

    assert result["status"] == "not_found"
    assert result.get("reason")
    # An empty name is an honest not_found too, never an exception.
    assert windows.resolve_app("")["status"] == "not_found"
    assert windows.resolve_app("").get("reason")


def test_duplicate_display_names_are_ambiguous_not_guessed(monkeypatch):
    """Two distinct installed apps, same display name -> ASK, don't pick."""
    _fake_installed(monkeypatch, startapps=[
        ("OpenCode", "ai.opencode.desktop"),
        ("OpenCode", "com.example.other"),
    ])

    result = windows.resolve_app("opencode")

    assert result["status"] == "ambiguous"
    assert result.get("reason")
    assert len(result["candidates"]) == 2
    assert {c["app_id"] for c in result["candidates"]} == {
        "ai.opencode.desktop", "com.example.other",
    }
    assert all(c["name"] == "OpenCode" for c in result["candidates"])


def test_installed_candidates_order_exact_prefix_substring(monkeypatch):
    _fake_installed(monkeypatch, startapps=[
        ("Another Code", "d.another"),
        ("Codec Suite", "c.codec"),
        ("Code Helper", "b.helper"),
        ("Code", "a.code"),
    ])

    names = [c["name"] for c in windows.find_installed_app("code")]

    assert names == ["Code", "Code Helper", "Codec Suite", "Another Code"]


def test_uninstall_display_names_are_a_secondary_source(monkeypatch):
    _fake_installed(monkeypatch, uninstall=["Old Timer App"])

    result = windows.resolve_app("old timer")

    assert result["status"] == "found"
    assert result["method"] == "registry"
    assert result["app_id"] is None


def test_startapps_row_supersedes_same_name_uninstall_row(monkeypatch):
    """The same app listed in StartApps AND Uninstall is ONE candidate."""
    _fake_installed(monkeypatch,
                    startapps=[("OpenCode", "ai.opencode.desktop")],
                    uninstall=["OpenCode"])

    result = windows.resolve_app("opencode")

    assert result["status"] == "found"
    assert result["method"] == "startapps"
    assert result["app_id"] == "ai.opencode.desktop"


def test_versioned_uninstall_name_does_not_fake_ambiguity(monkeypatch):
    """Measured live 2026-11-02: Uninstall lists "OpenCode 1.19.4" while
    StartApps lists "OpenCode" — the SAME app. The version suffix must not
    turn the one real query this exists for into an ambiguous ask."""
    _fake_installed(monkeypatch,
                    startapps=[("OpenCode", "ai.opencode.desktop")],
                    uninstall=["OpenCode 1.19.4"])

    result = windows.resolve_app("opencode")

    assert result["status"] == "found"
    assert result["method"] == "startapps"
    assert result["app_id"] == "ai.opencode.desktop"


# --- the rstrip(".exe") regression ----------------------------------------- #

def test_exe_suffix_is_stripped_as_a_suffix_not_a_charset():
    # Old bug: "opencode".rstrip(".exe") stripped the CHARACTER SET {.,e,x},
    # producing "opencod" -> Program Files scan looked for opencod.exe.
    assert windows._strip_exe_suffix("opencode") == "opencode"
    assert windows._strip_exe_suffix("opencode.exe") == "opencode"
    assert windows._strip_exe_suffix("OpenCode.EXE") == "OpenCode"
    assert windows._strip_exe_suffix("edge") == "edge"  # rstrip -> "edg"


def test_program_files_scan_finds_opencode_exe(monkeypatch, tmp_path):
    install_dir = tmp_path / "OpenCode"
    install_dir.mkdir()
    (install_dir / "OpenCode.exe").write_bytes(b"MZ")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "missing-x86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "missing-local"))

    # Lowercase query AND the display-name casing must both find the file.
    for variation in (["opencode"], ["OpenCode", "OpenCode.exe"]):
        found = windows._scan_program_dirs(variation)
        assert found, variation
        assert found.lower().endswith("opencode.exe")


# --- fail-soft + cache ----------------------------------------------------- #

def test_powershell_failure_fails_soft(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("powershell is missing")

    monkeypatch.setattr(windows.subprocess, "run", _boom)
    monkeypatch.setattr(windows, "_uninstall_display_names", lambda: [])

    assert windows.find_installed_app("opencode") == []

    monkeypatch.setattr(windows, "_find_app_executable_detail",
                        lambda query, timeout=8: (None, None))
    result = windows.resolve_app("opencode")
    assert result["status"] == "not_found"
    assert result.get("reason")


def test_startapps_is_queried_once_and_cached(monkeypatch):
    calls = []

    def _fake_run(*args, **kwargs):
        calls.append(kwargs.get("timeout"))

        class _Result:
            stdout = "OpenCode\tai.opencode.desktop"
        return _Result()

    monkeypatch.setattr(windows.subprocess, "run", _fake_run)
    monkeypatch.setattr(windows, "_uninstall_display_names", lambda: [])

    first = windows.find_installed_app("opencode")
    second = windows.find_installed_app("opencode")

    assert first and second
    assert first[0]["app_id"] == "ai.opencode.desktop"
    assert len(calls) == 1, "the second lookup must be served from cache"
    assert calls[0], "the PowerShell query must carry a timeout"


# --- browser tokens: app names fall through to the brain ------------------- #

def test_open_browser_app_falls_through_to_the_brain(monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    for phrase in ("open microsoft edge", "open chrome", "open firefox"):
        assert router.route(phrase, actor="voice") is None, phrase

    assert opened == [], "browser APP names must never be answered with a URL"


def test_open_site_still_opens_the_browser(monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    result = router.route("open youtube", actor="voice")

    assert result is not None
    assert result["action"] == "browser"
    assert opened and "youtube" in opened[0]


def test_launch_and_close_handlers_stay_off_the_route_manifest():
    """AGENTS.md hard rule: app open/close flows through the brain."""
    source = inspect.getsource(router.route)
    assert "_handle_launch_app" not in source
    assert "_handle_close_app" not in source
