# FRIDAY — GitHub Packaging Copy (D:\01 USEFUL ONLY)

This tree is the SHIPPABLE copy of FRIDAY (the personal voice-first computer assistant). It is a heavily customized, Windows-focused fork of Open Interpreter's `01`, with a local Ollama brain and optional free-tier cloud models.

- The live/dev machine tree lives elsewhere (`D:\01`) — do not expect runtime artifacts here.
- **Removals/deletions happen ONLY in this tree.** Bug fixes are also applied to the original so shared code stays identical.

## What it is
- Voice-first personal assistant: wake word → RealtimeSTT → brain (Ollama `qwen3:8b` default, cloud failover) → Open Interpreter tools (screen vision, OCR, app/window control, web) → TTS.
- Server inside `software\` (FastAPI/uvicorn). OI display API patched via `site-packages-patches\` to use FRIDAY's local vision chain (no remote `/point/` service).

## Install & run (no venv required)
1. Install into the target Python (3.11+; a venv also works if you activate one first):
   ```
   cd software
   pip install --no-deps -r requirements_freeze.txt
   ```
   (`--no-deps` is required: the freeze is an exact closure; plain resolution conflicts — litellm needs `openai>=2.20,<3.0` while livekit-plugins-openai pins `openai~=1.35`.)
2. Re-apply the 3 hand-patched Open Interpreter files into that Python's site-packages:
   ```
   $sp = & python -c "import sys; print(next(p for p in sys.path if p.endswith('site-packages')))"
   copy .\site-packages-patches\interpreter\core\computer\display\display.py      "$sp\interpreter\core\computer\display\"
   copy .\site-packages-patches\interpreter\core\computer\display\friday_locate.py "$sp\interpreter\core\computer\display\"
   copy .\site-packages-patches\interpreter\core\computer\vision\vision.py         "$sp\interpreter\core\computer\vision\"
   ```
3. Copy `dot_friday\api_credentials.example.json` → `dot_friday\api_credentials.json` and fill in keys (or skip — FRIDAY runs fully local without any).
4. Run: `python software\main.py` (see `--help`; `--server-port` overrides the default port).

## Repository structure (software\)
- `main.py` — entrypoint (server boot, livekit, auto-start hooks).
- `source\server\` — the assistant: routing, STT/brain/TTS pools (`api_pools.py`), Open Interpreter integration, memory (`self_improve.py`, scratchpad/lessons in `~/.friday\`), profiles (`profiles\default.py`), remote control (`remote_telegram.py`), UI (`ui\`), LiveKit voice (`livekit\`).
- `site-packages-patches\` — the 3 patched OI files this repo ships (also stamped into site-packages at install).
- `tmp-oi\` — patched open-interpreter wheel for the freeze.
- `requirements_freeze.txt` / `pyproject.toml` — exact dependency closures; `poetry.lock` intentionally not shipped (first `poetry install` rebuilds it).

## Health & secrets
- Never commit real keys. `dot_friday\api_credentials.json` and `*.db` are gitignored; after any API testing reset credential files to placeholders.
- Keep the tree free of runtime debris: `__pycache__`, `*.db`, `*.log` do not belong here (they may exist in `.gitignore`).
- Tests: none formal — boot + `GET /ping` → `pong`, then one voice turn (STT → brain → TTS) to smoke-test on a change.

## Docs
- `CONTEXT.md` (design philosophy — note: AGPL-3.0, NOT MIT), `ROADMAP.md`, `USES.md`. Fork freely.