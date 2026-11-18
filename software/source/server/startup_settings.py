"""Startup-on-boot toggle + system-tray integration for FRIDAY.

Settings tab writes HKCU\\...\\CurrentVersion\\Run so Windows auto-starts
FRIDAY at boot. The tray icon (pystray) shows Exit / Open App / Close App:
- Open App: brings up the CMD panel + HUD (main window).
- Exit: kills FRIDAY entirely (voice brain + tray).
- Close App: hides the HUD window; FRIDAY keeps running in the tray.

Telegram keeps announcing 'its on' at every boot, phone-first.
"""

import os
import subprocess
import sys
import threading
import hashlib
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "FRIDAY01"
_SETTINGS_LOCK = threading.RLock()


def _startup_command():
    return f'wscript.exe "{Path(__file__).resolve().parents[2]}\\friday_boot.vbs"'


def _entry(winreg, key):
    try:
        return winreg.QueryValueEx(key, _RUN_VALUE)
    except FileNotFoundError:
        return None


def _revision(entry):
    return hashlib.sha256(repr(entry).encode("utf-8")).hexdigest()


def _registration(entry, winreg):
    if entry is None:
        return False
    return True if entry == (_startup_command(), winreg.REG_SZ) else None


def startup_snapshot():
    if os.environ.get("FRIDAY_USER_ROUTES"):
        return {"available": False, "editable": False, "message": "Sign-in registration is not provided by this local distribution setup; launch manually."}
    """Read one registry value without importing a tray backend or writing defaults."""
    try:
        import winreg
        with _SETTINGS_LOCK, winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            entry = _entry(winreg, key)
            enabled = _registration(entry, winreg)
            return {"available": True, "startup_on": enabled, "revision": _revision(entry),
                    "editable": enabled is not None,
                    "registration": "absent" if entry is None else "unrecognized" if enabled is None else "this-installation",
                    "tray_state": "not reported"}
    except (ImportError, OSError):
        return {"available": False, "editable": False, "message": "Windows startup registration could not be read."}


def set_startup_checked(enabled, expected):
    if os.environ.get("FRIDAY_USER_ROUTES"):
        return {"accepted": False, "message": "Public setup does not register the legacy launcher. No Windows sign-in setting was changed."}
    """Compare the saved value under the shared lock before one registry mutation."""
    if type(enabled) is not bool or not isinstance(expected, str) or len(expected) != 64:
        return {"accepted": False, "message": "A boolean choice and current startup revision are required."}
    try:
        import winreg
        with _SETTINGS_LOCK, winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                          winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            entry = _entry(winreg, key)
            if _revision(entry) != expected:
                return {"accepted": False, "conflict": True, "message": "Startup changed elsewhere. Refresh before saving."}
            if _registration(entry, winreg) is None:
                return {"accepted": False, "message": "An unrecognized startup registration was preserved."}
            if enabled:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, _startup_command())
            elif entry is not None:
                winreg.DeleteValue(key, _RUN_VALUE)
            current = _entry(winreg, key)
            if _registration(current, winreg) is not enabled:
                return {"accepted": False, "message": "Startup could not be confirmed. Refresh its saved state."}
            return {"accepted": True, "enabled": enabled, "revision": _revision(current)}
    except (ImportError, OSError):
        return {"accepted": False, "message": "Startup could not be saved. Check access and refresh before retrying."}


def _startup_conhost():
    return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "conhost.exe")


def _startup_python():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".venv", "Scripts", "python.exe"))


def _startup_pythonw():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".venv", "Scripts", "pythonw.exe"))


def _startup_main():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "main.py"))


def set_startup(enabled: bool) -> bool:
    """Legacy local helper, serialized through the checked writer."""
    if type(enabled) is not bool:
        return False
    with _SETTINGS_LOCK:
        snapshot = startup_snapshot()
        if not snapshot.get("available"):
            return False
        return set_startup_checked(enabled, snapshot["revision"])["accepted"]


def startup_enabled() -> bool:
    if os.environ.get("FRIDAY_USER_ROUTES"):
        return False
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
        winreg.QueryValueEx(key, _RUN_VALUE)
        key.Close()
        return True
    except FileNotFoundError:
        return False


def tray_status() -> dict:
    return {"startup_windows": startup_enabled(), "tray_available": _pystray_available()}


def _pystray_available():
    try:
        import pystray  # noqa: F401

        return True
    except ImportError:
        return False


_TRAY_LOCK = threading.RLock()
_TRAY_CONTROLLER = None


def _request_tray_stop(controller):
    """Release only this tray owner; never call the assistant exit callback."""
    with _TRAY_LOCK:
        if _TRAY_CONTROLLER is not controller or controller["state"] in ("stopping", "stopped"):
            return False
        running = controller["state"] == "running"
        controller["state"] = "stopping"
        icon = controller["icon"]
    if running:
        try:
            icon.visible = False
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            print("[tray] Backend stop could not be confirmed.", flush=True)
    # If stopped before readiness, the setup callback performs stop once ready.
    return True


def start_tray(on_open_app=None, on_close_app=None, on_exit=None):
    """One process-local tray owner; no work happens until this explicit call."""
    global _TRAY_CONTROLLER
    with _TRAY_LOCK:
        if _TRAY_CONTROLLER is not None:
            return _TRAY_CONTROLLER
        try:
            import pystray
            from .tray_assets import TOOLTIP, load_tray_image
            image = load_tray_image()
        except Exception:
            print("[tray] Tray support unavailable; FRIDAY can continue without it.", flush=True)
            return None
        controller = {"state": "starting", "icon": None, "thread": None,
                      "hide_app": lambda: None}

        def dispatch(callback):
            with _TRAY_LOCK:
                if _TRAY_CONTROLLER is not controller or controller["state"] != "running":
                    return
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        print("[tray] Menu action could not be completed.", flush=True)

        def open_app(_icon, _item):
            dispatch(on_open_app)

        def close_app(_icon, _item):
            dispatch(on_close_app)

        def exit_app(_icon, _item):
            with _TRAY_LOCK:
                if _TRAY_CONTROLLER is not controller or controller["state"] != "running":
                    return
            if not _request_tray_stop(controller):
                return
            # Hide/stop BEFORE the existing hard-exit handler, which may never return.
            if on_exit is not None:
                try:
                    on_exit()
                except Exception:
                    print("[tray] Exit action could not be completed.", flush=True)

        def ready(icon):
            try:
                with _TRAY_LOCK:
                    if _TRAY_CONTROLLER is controller and controller["state"] == "starting":
                        controller["state"] = "running"
                        icon.visible = True
                        return
                icon.stop()
            except Exception:
                _request_tray_stop(controller)

        def run_tray():
            global _TRAY_CONTROLLER
            try:
                icon.run(setup=ready)
            except Exception:
                print("[tray] Tray backend stopped unexpectedly; assistant startup is independent.", flush=True)
            finally:
                _request_tray_stop(controller)
                with _TRAY_LOCK:
                    controller["state"] = "stopped"
                    controller["icon"] = None
                    if _TRAY_CONTROLLER is controller:
                        _TRAY_CONTROLLER = None
                image.close()

        try:
            menu = pystray.Menu(
                pystray.MenuItem("Open App", open_app, default=True),
                pystray.MenuItem("Close App (keep FRIDAY running)", close_app),
                pystray.MenuItem("Exit FRIDAY", exit_app),
            )
            icon = pystray.Icon("FRIDAY01", image, TOOLTIP, menu)
            controller["icon"] = icon
            controller["stop"] = lambda: _request_tray_stop(controller)
            thread = threading.Thread(target=run_tray, daemon=True, name="friday-tray")
            controller["thread"] = thread
            _TRAY_CONTROLLER = controller
            thread.start()
            return controller
        except Exception:
            if _TRAY_CONTROLLER is controller:
                _TRAY_CONTROLLER = None
            controller["state"] = "stopped"
            controller["icon"] = None
            controller["thread"] = None
            image.close()
            print("[tray] Tray initialization unavailable; FRIDAY can continue without it.", flush=True)
            return None
