"""Device-free diagnostic entry point for the shipped Light audio transport."""
import os


def enabled():
    value = os.environ.get("FRIDAY_AUDIO_STARTUP_CHECK", "")
    if value not in ("", "0", "1"):
        raise ValueError("FRIDAY_AUDIO_STARTUP_CHECK must be unset, 0 or 1")
    return value == "1"


def run(port, debug=False):
    import asyncio
    import importlib
    import threading

    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    import uvicorn

    from .audio_link import AudioLink, AudioWebSocket
    from .rollout_readiness import runtime_readiness

    class TransportOnly:
        async def input(self, _value):
            pass

        async def output(self):
            # The real transport awaits output, but this mode produces none.
            await asyncio.Future()

    link = AudioLink({"is_paused": True, "audio_startup_check": True})
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.add_middleware(AudioWebSocket, link=link, interpreter=TransportOnly())

    @app.get("/ping")
    async def ping():
        return PlainTextResponse("pong", headers={"X-Friday-Mode": "audio-startup-check"})

    @app.get("/voice/trace")
    async def voice_trace():
        return {"mode": "audio-startup-check", "devices_enabled": False,
                "voice_input": link.voice_snapshot(), "speech": {}}

    @app.get("/readiness")
    async def readiness():
        return runtime_readiness({"audio_link": link}, diagnostic=True)

    # Same local v2 policy as the Light server. Only the diagnostic routes
    # above exist, and the always-paused session can never accept capture.
    os.environ["INTERPRETER_REQUIRE_AUTH"] = "False"
    client = importlib.import_module("source.clients.light-python.client")
    threading.Thread(target=client.run, args=(f"127.0.0.1:{port}", debug),
                     daemon=True, name="friday-audio-check-client").start()
    print("FRIDAY audio transport startup check (devices disabled)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port)
