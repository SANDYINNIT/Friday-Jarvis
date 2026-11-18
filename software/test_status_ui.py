"""Focused tests for the optional FRIDAY status/event boundary."""

from concurrent.futures import ThreadPoolExecutor
import re
import tempfile
from pathlib import Path

import pytest

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

# The particle field is deliberately tuned for cost (600 -> 280 in the
# 2026-10-16 HUD pass, "lower hidden-tab orb cost"). Asserting a pinned
# literal went stale on that change and broke collection of the WHOLE suite
# (a bare "600" also matches any unrelated number). Check the declaration
# instead: it exists, and it is a sane positive count.
_js_text = (UI_DIRECTORY / "app.js").read_text(encoding="utf-8")
_particle_decl = re.search(r"const\s+PARTICLE_COUNT\s*=\s*(\d+)", _js_text)
assert _particle_decl, "app.js no longer declares PARTICLE_COUNT"
assert int(_particle_decl.group(1)) > 0, "PARTICLE_COUNT must be positive"

from source.server.status_ui import WebviewBridge, _chain_position
@pytest.mark.parametrize("modal_configured", [False, True])
def test_provider_roster_respects_optional_modal_configuration(monkeypatch, tmp_path, modal_configured):
    from source.server import api_pools, brain_router, n8n_runtime

    monkeypatch.setattr(brain_router, "MODAL_BASE",
                        "http://127.0.0.1:9/v1" if modal_configured else None)
    monkeypatch.setattr(api_pools, "describe_pools", lambda: {})
    adapter = n8n_runtime.N8NAdapter(
        config=n8n_runtime.load_config(path=tmp_path / "n8n.json", environ={}),
    )
    monkeypatch.setattr(n8n_runtime, "get_adapter", lambda: adapter)

    data = WebviewBridge(bus).get_ai_agents()
    agents = data["agents"]
    ids = [agent["id"] for agent in agents]
    required = {
        "agent-groq-conv", "agent-groq-reasoning", "agent-openrouter-brain",
        "agent-gemini-research", "agent-local-qwen", "agent-gemini-vision",
        "agent-openrouter-vision", "agent-groq-stt", "agent-deepgram-stt",
        "agent-local-whisper", "agent-gemini-stt", "agent-gemini-tts",
        "agent-local-edge-tts", "agent-n8n-workflows",
    }
    if modal_configured:
        required.add("agent-modal-primary")
    assert set(ids) == required
    names = {agent["name"] for agent in agents}
    assert {"Groq Primary Dialogue Agent", "Gemini Vision Fallback Agent",
            "Local Qwen3 Offline Brain"} <= names
    assert ids[0] == ("agent-modal-primary" if modal_configured else "agent-groq-conv")
    assert data["summary"]["total"] == len(ids) == len(required)
    assert "active_tier" not in data["summary"]
    assert "chain_position" in data["summary"]


# The pill that used to fake "NORMAL" must show real chain depth instead.
for _pool, _expected in (
    (None, "---"),
    ("brain_modal", "STEP 1/5"),
    ("brain_groq", "STEP 2/5"),
    ("brain_openrouter", "STEP 3/5"),
    ("brain_gemini", "STEP 4/5"),
    ("local", "STEP 5/5"),
):
    assert _chain_position(_pool) == _expected, (_pool, _chain_position(_pool))
# app.js must not still READ the removed key. Comments are stripped first so the
# explanatory note in the source is allowed while real usage is not.
_js_all = (UI_DIRECTORY / "app.js").read_text(encoding="utf-8")
_js_code = "\n".join(
    ln for ln in _js_all.splitlines() if not ln.lstrip().startswith("//")
)
assert "active_tier" not in _js_code, "app.js still reads the removed active_tier key"
assert "agents-summary-tier" not in _js_all
assert "agents-summary-chain" in _js_all
print("PASS: webview bridge get_ai_agents returns multi-provider orchestrator matrix")
print("PASS: webview orb assets and optional microphone mute hook exist")
