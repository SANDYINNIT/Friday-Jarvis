"""Actual Light admission/output paths; uses existing fake-provider fixture."""
import asyncio
import pytest
from test_request_reply_ownership import isolated_server, telegram, drain
from test_mcp_light import produce
from source.server.status_ui import WebviewBridge


@pytest.mark.parametrize("isolated_server", ["mcp"], indirect=True)
def test_light_private_hud_then_telegram_and_stop_joking(isolated_server, monkeypatch, tmp_path):
    runtime, deliveries = isolated_server
    monkeypatch.setenv("FRIDAY_COMPANION_SETTINGS", str(tmp_path / "personality.json"))
    p = runtime.personality
    assert p.save({"private_banter":"spicy"}, p.preferences()["revision"])["ok"]
    bridge = WebviewBridge(runtime.fixture_status, runtime)

    async def scenario():
        await runtime.server.app.router.startup()
        try:
            assert (await asyncio.to_thread(runtime.submit_text, "Explain a Python typo"))["accepted"]
            hud = runtime.server_state["turn_ownership"]
            assert hud.private_text and hud.muted and not hud.is_external
            await asyncio.to_thread(produce, runtime)
            await drain(runtime)
            first = runtime.fixture_completions[-1]["messages"][0]["content"]
            assert "TRUSTED TURN TONE: Private text-only" in first
            assert not runtime.tts.fed  # actual output gate, not just prompt text
            await telegram(runtime, "101", "Explain a Python typo")
            await asyncio.to_thread(produce, runtime)
            await drain(runtime)
            assert "TRUSTED TURN TONE: Neutral" in runtime.fixture_completions[-1]["messages"][0]["content"]
            assert (await asyncio.to_thread(runtime.submit_text, "stop joking"))["accepted"]
            assert p.preferences()["values"]["private_banter"] == "off"
            bridge.stop_turn()
            assert not p.suggest("n8n_decision", "pr-1234abcd", current=True, live=lambda:True)
        finally:
            await runtime.server.app.router.shutdown()
    asyncio.run(scenario())
