"""Bounded offline store/bridge selection; no production runtime or personal state."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
area = ROOT / ".venv" / "field-notes-ui-check" / "personality"
area.mkdir(parents=True, exist_ok=True)
run = Path(tempfile.mkdtemp(prefix="run-", dir=area))
for key in ("HOME", "USERPROFILE", "TEMP", "TMP"):
    os.environ[key] = str(run)
tempfile.tempdir = str(run)
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
for key, filename in {
    "FRIDAY_COMPANION_SETTINGS":"preferences.json", "FRIDAY_TASK_DB":"tasks.db",
    "FRIDAY_AUDIT_DIR":"audit", "FRIDAY_LOG_DIR":"logs",
    "FRIDAY_EXHAUSTION_LEDGER":"exhaustion.json", "FRIDAY_MEMORY_DB":"memory.db",
    "FRIDAY_LIGHT_ARCHIVE_DB":"archive.db", "FRIDAY_KNOWLEDGE_DB":"knowledge.db",
}.items():
    os.environ[key] = str(run / filename)
os.environ.update(FRIDAY_IDENTITY_INTEGRATION="0", FRIDAY_IDENTITY_READER="0")
record = Path(__file__).with_name("personality-results.json")


def audit(event, args):
    if event in ("subprocess.Popen", "socket.connect", "socket.bind", "socket.getaddrinfo") or event.startswith("winreg."):
        raise RuntimeError("Personality verification forbids processes, network and real registry access")
    if event == "open" and isinstance(args[0], (str, bytes)):
        mode, flags = args[1], args[2]
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing:
            path = Path(os.fsdecode(args[0])).resolve()
            if path != record.resolve() and not path.is_relative_to(run.resolve()):
                raise RuntimeError("Verification write outside its isolated fixture")


sys.addaudithook(audit)
# Allows the already-installed pure Python pytest to run under an existing host
# runtime when the old venv executable is missing. Does not install/upgrade it or
# make incompatible CPython native extensions usable.
sys.path.append(str(ROOT / ".venv" / "Lib" / "site-packages"))
import pytest


class Results:
    def __init__(self):
        self.rows = []

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.failed:
            self.rows.append({"test": report.nodeid, "phase": report.when, "outcome": report.outcome})


results = Results()
code = pytest.main(["-q", "test_personality.py", "test_settings_bridge.py", "--tb=short",
                    "--override-ini=addopts=", "--log-file=" + str(run / "pytest.log"),
                    "-p", "no:cacheprovider"], plugins=[results])
record.write_text(json.dumps({"exit_code": int(code), "python": sys.version.split()[0],
    "scope": "real preference store/bridge; synthetic channels/events/providers; no full Light acceptance",
    "guards": "no subprocess/network/real registry; writes limited to scratch and this evidence file",
    "results": results.rows}, indent=2), encoding="utf-8")
raise SystemExit(code)
