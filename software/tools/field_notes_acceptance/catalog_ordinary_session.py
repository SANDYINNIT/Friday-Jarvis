"""Temporary observation/containment for ONE actual shipped ordinary launch.

No fake profile, catalog, world, provider, audio or HUD implementation. The
driver injects this via scratch sitecustomize; nothing is installed globally.
"""
import builtins
import errno
import json
import os
from pathlib import Path
import sys
import threading
import time


def install():
    root = Path(__file__).resolve().parents[2]
    scratch = Path(os.environ["FRIDAY_ORDINARY_ACCEPTANCE"]).resolve(strict=True)
    if not scratch.is_relative_to(root / ".venv/field-notes-ui-check"):
        raise RuntimeError("Invalid acceptance location")
    if os.environ.get("FRIDAY_AUDIO_STARTUP_CHECK", "0") != "0":
        raise RuntimeError("Ordinary acceptance cannot use the thin diagnostic runtime")
    events = scratch / f"events-{os.getpid()}.jsonl"
    lock = threading.RLock()

    def mark(event, **fields):
        with lock, events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "event": event,
                                     "at": time.time(), **fields}) + "\n")

    denied = set()

    def audit(event, args):
        if event in ("socket.connect", "socket.getaddrinfo"):
            target = args[1] if event == "socket.connect" else args[0]
            host = target[0] if isinstance(target, tuple) else target
            port = target[1] if isinstance(target, tuple) else None
            if host not in ("127.0.0.1", "::1", "localhost", None) or port == 11434:
                key = "local-provider" if port == 11434 else "outbound"
                if key not in denied:
                    denied.add(key)
                    mark("network-contained", category=key)
                raise OSError(errno.ENETUNREACH, "Acceptance permits local application traffic only")

    sys.addaudithook(audit)
    mark("python-start", python=sys.version.split()[0],
         main_entry=Path(sys.argv[0]).resolve() == root / "main.py")
    if Path(sys.argv[0]).resolve() != root / "main.py":
        return  # STT subprocess inherits containment, not window ownership.

    original_import = builtins.__import__
    patched = set()
    holder = {}

    def watch(interpreter):
        import win32gui
        import win32process
        started = time.monotonic()
        seen = set()
        while time.monotonic() - started < 180:
            if "pause" not in seen:
                pause = getattr(interpreter, "set_session_voice_input", None)
                if callable(pause) and pause(True) is not None:
                    seen.add("pause")
                    mark("session-input-paused", scope="software gate, not hardware mute")
            wrapper = getattr(interpreter, "server", None)
            server = getattr(wrapper, "uvicorn_server", None)
            if server is not None and getattr(server, "started", False) and "socket" not in seen:
                sockets = [sock for listener in getattr(server, "servers", ())
                           if listener.is_serving() for sock in listener.sockets]
                if any(sock.getsockname() == ("127.0.0.1", 10101) for sock in sockets):
                    seen.add("socket")
                    mark("owned-asgi-listener", port=10101)
            if "window" not in seen:
                found = []
                def visit(hwnd, _):
                    if (win32gui.GetWindowText(hwnd) == "FRIDAY"
                            and win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid()
                            and win32gui.IsWindowVisible(hwnd)):
                        found.append(hwnd)
                win32gui.EnumWindows(visit, None)
                if found:
                    seen.add("window")
                    mark("native-hud-visible", hwnd=found[0])
            if (scratch / "request-stop").exists() and callable(holder.get("exit")):
                mark("intentional-exit-callback")
                holder["exit"]()
                return
            time.sleep(.2)
        mark("observer-deadline")
        if callable(holder.get("exit")):
            holder["exit"]()

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        result = original_import(name, globals, locals, fromlist, level)
        ui = sys.modules.get("source.server.status_ui")
        if ui is not None and hasattr(ui, "run_status_window") and "ui" not in patched:
            patched.add("ui")
            original_window = ui.run_status_window
            def observed_window(bus, interpreter=None, start_hidden=False):
                identity = getattr(interpreter, "friday_startup_identity", None)
                from source.server.world.identity_catalog import read_shadow_projection
                catalog = read_shadow_projection(identity.catalog.path) if identity is not None else None
                approved = Path.home() / ".friday/catalog-activation"
                mark("ordinary-runtime-selection", installed=identity is not None,
                     approved_catalog=bool(identity and identity.catalog.path == approved / "identity-catalog.db"),
                     approved_history=bool(identity and identity.shadow._history == approved / "identity-duty-history.db"),
                     revision=catalog["revision"] if catalog else None,
                     bindings=len(catalog["bindings"]) if catalog else None,
                     integration=os.environ.get("FRIDAY_IDENTITY_INTEGRATION"),
                     reader=os.environ.get("FRIDAY_IDENTITY_READER"),
                     operator_off=os.environ.get("FRIDAY_TOWN_INSTALL_OPERATOR", "0") in ("", "0"),
                     start_hidden=start_hidden)
                threading.Thread(target=watch, args=(interpreter,), daemon=True).start()
                return original_window(bus, interpreter, start_hidden=start_hidden)
            ui.run_status_window = observed_window
        settings = sys.modules.get("source.server.startup_settings")
        if settings is not None and hasattr(settings, "start_tray") and "exit" not in patched:
            patched.add("exit")
            original_tray = settings.start_tray
            def observed_tray(*args, **kwargs):
                holder["exit"] = kwargs.get("on_exit")
                mark("owned-shutdown-callback-available")
                return original_tray(*args, **kwargs)
            settings.start_tray = observed_tray
        if len(patched) == 2:
            builtins.__import__ = original_import
        return result
    builtins.__import__ = importing
