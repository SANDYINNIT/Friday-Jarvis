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
2. Re-apply the 5 hand-patched Open Interpreter files into that Python's site-packages:
   ```
   $sp = & python -c "import sys; print(next(p for p in sys.path if p.endswith('site-packages')))"
   copy .\site-packages-patches\interpreter\core\computer\display\display.py      "$sp\interpreter\core\computer\display\"
   copy .\site-packages-patches\interpreter\core\computer\display\friday_locate.py "$sp\interpreter\core\computer\display\"
   copy .\site-packages-patches\interpreter\core\computer\vision\vision.py         "$sp\interpreter\core\computer\vision\"
   copy .\site-packages-patches\interpreter\core\respond.py                        "$sp\interpreter\core\"
   copy .\site-packages-patches\interpreter\core\llm\utils\merge_deltas.py         "$sp\interpreter\core\llm\utils\"
   ```
3. Copy `dot_friday\api_credentials.example.json` → `dot_friday\api_credentials.json` and fill in keys (or skip — FRIDAY runs fully local without any).
4. Run: `python software\main.py` (see `--help`; `--server-port` overrides the default port).

LiveKit voice (optional, `--server livekit`): pip does NOT ship the LiveKit server binary. Download `livekit-server` from https://github.com/livekit/livekit/releases (Windows: `livekit_1.13.7_windows_amd64.zip`; also on Homebrew/Linux) and put it on PATH. `main.py` refuses to boot in livekit mode with a clear message if the binary is missing.

Windows build toolchain (optional): 21 of the frozen pins are sdist-only (`webrtcvad`, `docopt`, `html2text`, `wget`, `PyAutoGUI`, `PyGetWindow`, `PyRect`, `pytweening`, `proxy_tools`, `MouseInfo`, `PyMsgBox`, `pyperclip`, `PyScreeze`, `encodec`, `gruut*`, ...). If pip tries to build one, install MSVC Build Tools (Visual Studio Build Tools with the C++ workload) first; `webrtcvad` has no prebuilt wheel anywhere. FRIDAY's core does not require it.

## Repository structure (software\)
- `main.py` — entrypoint (server boot, livekit, auto-start hooks).
- `test_*.py` — FRIDAY's test suite (341 tests, no network/mic/API keys needed). Run `pytest` from `software\`; `pyproject.toml` sets `testpaths`/`norecursedirs` so it never walks `.venv`.
- `source\server\` — the assistant: routing, STT/brain/TTS pools (`api_pools.py`), Open Interpreter integration, memory (`self_improve.py`, scratchpad/lessons in `~/.friday\`), self-awareness (`self_awareness.py`), health (`doctor.py`, `system_diagnostics.py`), tasks (`task_store.py`), optional n8n (`n8n_runtime.py`), profiles (`profiles\default.py`), remote control (`remote_telegram.py`), UI (`ui\`), LiveKit voice (`livekit\`).
- `site-packages-patches\` — the 5 patched OI files this repo ships (also stamped into site-packages at install; the `tmp-oi\` wheel already bakes them in too).
- `tmp-oi\` — patched open-interpreter wheel for the freeze.
- `requirements_freeze.txt` / `pyproject.toml` — exact dependency closures; `poetry.lock` intentionally not shipped (first `poetry install` rebuilds it).

## Health & secrets
- Never commit real keys. `dot_friday\api_credentials.json` and `*.db` are gitignored; after any API testing reset credential files to placeholders.
- No personal data in shipped code: no absolute developer paths, no usernames/machine names, no real handles. `tts_bootstrap.py` discovers the Python 3.12 interpreter from `%LOCALAPPDATA%`/`py` rather than a hard-coded path.
- Keep the tree free of runtime debris: `__pycache__`, `*.db`, `*.log`, `software\screenshots\` do not belong here (all gitignored — delete them if a test run creates them).
- Tests: `pytest` from `software\` is the primary check; boot + `GET /ping` → `pong` on a spare port (e.g. `--server-port 10102`) is the smoke test. Never run two instances at once — they share `~/.friday\` and will fight over the Telegram bot token (`409 Conflict`).

## Docs
- `CONTEXT.md` (design philosophy — note: AGPL-3.0, NOT MIT), `ROADMAP.md`, `USES.md`. Fork freely.