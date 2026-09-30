"""Focused tests for the optional FRIDAY status/event boundary."""

from concurrent.futures import ThreadPoolExecutor
import tempfile
from pathlib import Path

from source.server.status_ui import (
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_LISTENING,
    STATUS_SPEAKING,
    STATUS_THINKING,
    StatusBus,
    project_log_tail,
    status_payload,
    UI_ENTRYPOINT,
    UI_DIRECTORY,
    ui_settings,
)


bus = StatusBus()
assert bus.snapshot().state == STATUS_IDLE
assert bus.snapshot().sequence == 0

for state in (STATUS_LISTENING, STATUS_THINKING, STATUS_SPEAKING, STATUS_ERROR, STATUS_IDLE):
    bus.publish(state, state)

snapshot = bus.snapshot()
assert snapshot.state == STATUS_IDLE
assert snapshot.label == "STANDBY"
assert snapshot.detail == STATUS_IDLE
assert snapshot.sequence == 5
payload = status_payload(bus)
assert payload["state"] == STATUS_IDLE
assert payload["label"] == "STANDBY"
assert payload["color"] == "#4b7895"
assert payload["sequence"] == 5

try:
    bus.publish("unknown")
except ValueError:
    pass
else:
    raise AssertionError("unknown status was accepted")


def publish_from_worker(index):
    return bus.publish(STATUS_THINKING, str(index)).sequence


with ThreadPoolExecutor(max_workers=4) as executor:
    sequences = list(executor.map(publish_from_worker, range(20)))

assert len(set(sequences)) == 20
assert bus.snapshot().sequence == 25
print("PASS: status bus transitions are validated and thread-safe")
assert ui_settings()["status UI"] == "enabled"
with tempfile.TemporaryDirectory() as directory:
    Path(directory, "friday.log").write_text("safe line\nAPI_KEY=do-not-show\n", encoding="utf-8")
    log_tail = project_log_tail(directory)
    assert "safe line" in log_tail
    assert "do-not-show" not in log_tail
print("PASS: UI state helpers are headless")
assert UI_ENTRYPOINT.is_file()
assert (UI_DIRECTORY / "style.css").is_file()
assert (UI_DIRECTORY / "app.js").is_file()
assert "set_paused" in (UI_DIRECTORY / "app.js").read_text(encoding="utf-8")
assert "get_ai_agents" in (UI_DIRECTORY / "app.js").read_text(encoding="utf-8")
assert "600" in (UI_DIRECTORY / "app.js").read_text(encoding="utf-8")

from source.server.status_ui import WebviewBridge
class _FakeInterpreter:
    server_state = {}
bridge = WebviewBridge(bus, _FakeInterpreter())
agents_data = bridge.get_ai_agents()
# 14 = 13 provider agents + the optional n8n workflow-automation panel.
assert len(agents_data["agents"]) == 14, len(agents_data["agents"])
assert any(a["name"] == "Groq Primary Dialogue Agent" for a in agents_data["agents"])
assert any(a["name"] == "Gemini Visual Inspection Agent" for a in agents_data["agents"])
assert any(a["name"] == "Local Qwen3 Offline Brain" for a in agents_data["agents"])
assert any(a["id"] == "agent-n8n-workflows" for a in agents_data["agents"])
assert agents_data["summary"]["total"] == 14
print("PASS: webview bridge get_ai_agents returns multi-provider orchestrator matrix")
print("PASS: webview orb assets and optional microphone mute hook exist")
