"""Cold-cache diagnostic CLI acceptance with side-effect sentinels."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from source.server.audio_startup import enabled


@pytest.mark.parametrize(("value", "expected"), [(None, False), ("", False), ("0", False), ("1", True)])
def test_exact_diagnostic_flag(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("FRIDAY_AUDIO_STARTUP_CHECK", raising=False)
    else:
        monkeypatch.setenv("FRIDAY_AUDIO_STARTUP_CHECK", value)
    assert enabled() is expected


@pytest.mark.parametrize("value", ["true", " 1", "01"])
def test_ambiguous_diagnostic_flag_refuses_startup(monkeypatch, value):
    monkeypatch.setenv("FRIDAY_AUDIO_STARTUP_CHECK", value)
    with pytest.raises(ValueError, match="must be unset, 0 or 1"):
        enabled()


_GUARDED_CLI = r"""
import importlib.abc
from pathlib import Path
import runpy
import sys
import types

marker, entry, port = sys.argv[1:]

def forbidden(action):
    with Path(marker).open("a", encoding="utf-8") as log:
        log.write(action + "\n")
    raise AssertionError(action)

class BlockInitializers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"RealtimeSTT", "RealtimeTTS", "torch", "interpreter",
                                      "pyautogui", "mss", "livekit", "webview"} or fullname in {
            "source.server.server", "source.server.api_pools", "source.server.remote_telegram",
            "source.server.public_ingress", "source.server.screen_capture_service",
            "source.server.mcp_runtime",
        }:
            forbidden(fullname)

def audit(event, args):
    if event in ("socket.connect", "socket.getaddrinfo"):
        target = args[1] if event == "socket.connect" else args[0]
        host = target[0] if isinstance(target, tuple) else target
        if host not in ("127.0.0.1", "::1", "localhost", None):
            forbidden("nonlocal socket")
    if event == "subprocess.Popen":
        forbidden("subprocess")

sys.meta_path.insert(0, BlockInitializers())
sys.addaudithook(audit)
pyaudio = sys.modules["pyaudio"] = types.ModuleType("pyaudio")
pyaudio.paInt16 = 8
pyaudio.PyAudio = lambda: forbidden("PyAudio device")
sys.argv = [entry, "--status-ui", "--background", "--expose", "--client", "light-python",
            "--server-port", port]
runpy.run_path(entry, run_name="__main__")
"""


def test_real_cli_starts_light_transport_without_models_devices_or_external_calls(tmp_path):
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    marker = tmp_path / "forbidden.txt"
    cache = tmp_path / "empty-torch"
    cache.mkdir()
    env = {**os.environ, "FRIDAY_AUDIO_STARTUP_CHECK": "1", "USERPROFILE": str(tmp_path),
           "HOME": str(tmp_path), "TORCH_HOME": str(cache),
           "FRIDAY_LIGHT_ARCHIVE_DB": str(tmp_path / "must-not-create.db")}
    entry = Path(__file__).resolve().with_name("main.py")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    origin = f"http://127.0.0.1:{port}"
    log = tmp_path / "startup.log"
    with log.open("wb") as output, subprocess.Popen(
        [sys.executable, "-c", _GUARDED_CLI, str(marker), str(entry), str(port)],
        cwd=entry.parent, env=env, stdout=output, stderr=subprocess.STDOUT,
    ) as process:
        try:
            deadline = time.monotonic() + 20
            trace = {}
            while process.poll() is None and time.monotonic() < deadline and not marker.exists():
                try:
                    with opener.open(origin + "/voice/trace", timeout=1) as response:
                        trace = json.load(response)
                    if trace["voice_input"]["state"] == "applied":
                        break
                except urllib.error.URLError:
                    pass
                time.sleep(0.1)
            assert not marker.exists(), marker.read_text() if marker.exists() else ""
            assert trace == {
                "mode": "audio-startup-check", "devices_enabled": False, "speech": {},
                "voice_input": {"state": "applied", "revision": 0, "connected": True,
                                "supported": True, "startup_check": True},
            }, log.read_text(errors="replace")
            with opener.open(origin + "/ping", timeout=1) as response:
                assert response.read() == b"pong"
                assert response.headers["X-Friday-Mode"] == "audio-startup-check"
            with opener.open(origin + "/readiness", timeout=1) as response:
                assert json.load(response) == {"mode": "audio-startup-check",
                    "audio_transport": "v2-connected", "normal_readiness": "not-applicable"}
            for path in ("/chat", "/n8n", "/screen"):
                with pytest.raises(urllib.error.HTTPError) as unavailable:
                    opener.open(origin + path, timeout=1)
                assert unavailable.value.code == 404
            assert not marker.exists()
            assert not list(cache.iterdir())
            assert not (tmp_path / "must-not-create.db").exists()
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
