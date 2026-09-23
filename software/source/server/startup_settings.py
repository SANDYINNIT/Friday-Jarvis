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
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "FRIDAY01"


def _startup_conhost():
    return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "conhost.exe")


def _startup_python():
    """Interpreter used to spawn FRIDAY: the one already running this
    process — works with or without a virtualenv."""
    return sys.executable


def _startup_pythonw():
    """Windowless spawn variant: pythonw.exe next to the running interpreter
    when it exists, otherwise the interpreter itself."""
    exe = Path(sys.executable)
    pythonw = exe.with_name(f"pythonw{exe.suffix}")
    return str(pythonw) if pythonw.exists() else str(exe)


def _startup_main():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "main.py"))


def set_startup(enabled: bool) -> bool:
    """Register/unregister the auto-start entry via wmic-equivalent reg.exe."""
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
    except OSError as error:
        print(f"[startup toggle] registry open failed: {error}", flush=True)
        return False
    try:
        if enabled:
            # Hidden-boot command: POINTS AT friday_boot.vbs. python.exe
            # flashes a console window at boot (and conhost --headless does
            # not hide when Windows Terminal is the default console); pythonw
            # hangs (RealtimeSTT's multiprocessing worker cannot spawn under
            # pythonw). The .vbs runs everything at window style 0 with the
            # correct working directory — zero windows, every time.
            command = f'wscript.exe "{Path(Path(__file__).resolve().parents[2])}\\friday_boot.vbs"'
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, _RUN_VALUE)
            except FileNotFoundError:
                pass
    except OSError as error:
        print(f"[startup toggle] failed: {error}", flush=True)
        return False
    finally:
        key.Close()
    return True


def startup_enabled() -> bool:
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


def start_tray(on_open_app=None, on_close_app=None, on_exit=None):
    """Run the FRIDAY system-tray icon in a daemon thread. Returns the
    running icon controller or None when pystray is unavailable."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print("[tray] pystray unavailable â€” install pystray to enable the tray", flush=True)
        return None

    import PIL

    # Draw a simple 'F' badge so the tray icon reads as FRIDAY.
    image = PIL.Image.new("RGBA", (64, 64), (10, 20, 34, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([2, 2, 62, 62], outline=(66, 214, 255, 255), width=3)
    draw.text((22, 14), "F", fill=(82, 242, 177, 255))

    def _open_app(icon, item):
        if on_open_app is not None:
            on_open_app()

    def _close_app(icon, item):
        if on_close_app is not None:
            on_close_app()

    def _exit(icon, item):
        if on_exit is not None:
            on_exit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open App", _open_app, default=True),
        pystray.MenuItem("Close App (keep FRIDAY running)", _close_app),
        pystray.MenuItem("Exit FRIDAY", _exit),
    )
    icon = pystray.Icon("FRIDAY01", image, "FRIDAY â€” voice assistant", menu)
    controller = {}

    def run_tray():
        icon.run()

    thread = threading.Thread(target=run_tray, daemon=True, name="friday-tray")
    thread.start()
    controller = {"hide_app": lambda: None, "thread": thread, "icon": icon}
    return controller

