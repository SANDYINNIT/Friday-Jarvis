# FRIDAY — a personal JARVIS-style voice assistant for Windows

**FRIDAY** turns a Windows PC into a talking assistant you can command by voice or by phone (Telegram). She listens for a wake word, understands what you say, works your computer for you (opening apps, clicking your screen, managing windows, fetching the web), and speaks back in character. She is built on a fork of **Open Interpreter** (`01`) and is one hundred percent open source.

> [!NOTE]
> Everything here describes the copy in this repository. The full working install lives at the original project root; this repo is the portable, cleaned-up version you can clone, configure, and run yourself.

---

## What she can do

- **Talk to you** — full voice loop: wake word → speech-to-text → brain → tool use → text-to-speech, with a JARVIS/FRIDAY-style personality (`KAREN` warmth mixed in).
- **Work your computer** — open/close apps, focus windows, type, click, read your screen (screenshot → OCR/vision), control media, manage Wi-Fi tasks, run PowerShell/Python.
- **Use models smartly** — a default local brain (`qwen3:8b` via Ollama) with automatic failover to cloud providers (Groq, OpenRouter, Gemini, Deepgram) when the local model can't handle it or gets rate-limited. Cloud quota pools keep each API key locked to one job.
- **Remember things** — SQLite memory, a lessons file injected live into her system prompt every turn, note-taking, calendar scheduling, and a self-improvement loop.
- **Be reachable from your phone** — an allowlisted Telegram adapter lets you message her and receive answers, screenshots, and confirmations wherever you are.
- **Mind good manners** — she stays silent during phone calls, rejects hallucinated speech, guards against runaway tools, strips secrets from her own logs, and pauses politely on rate limits instead of crashing.

## How it works

```
Wake word  →  Speech-to-text (local RealtimeSTT / Groq whisper → Deepgram fallback)
                ↓
         Brain routing: weakest-first tier (local qwen3 → cloud Groq / OpenRouter / Gemini)
                ↓
         Open Interpreter runs her code (Python, tools, files, mouse/keyboard)
                ↓
         Result → spoken reply  (Gemini TTS → local edge-tts fallback)  +  HUD update
                ↓
         Memory: facts → SQLite · lessons → lessons.md · notes → project_notes.md
```

The flow is deliberately **forward-only failover**: when a provider errors or rate-limits, she silently tries the next account/model, never loses the user's request, and never speaks internal reasoning or tool JSON.

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 (Windows 10/11) |
| Server | FastAPI/uvicorn (`software/source/server/server.py`) |
| Assistant core | Open Interpreter (patched fork, wheel in `software/tmp-oi/`) |
| Local brain | Ollama `qwen3:8b` |
| Cloud brain/vision | Groq, OpenRouter, Gemini (optional keys) |
| STT | local RealtimeSTT (faster-whisper); cloud: Groq whisper → Deepgram |
| TTS | cloud-first Gemini → local edge-tts server (`:5050`) |
| UI | pywebview HUD (Chat + AI Core + Calendar + Memory + …) |

---

## Requirements

- **OS:** Windows 10/11 (Windows-only features are used throughout).
- **Python 3.11** in a virtualenv (`software\.venv`).
- **Poetry** for installing dependencies.
- **Ollama** running locally with at least `qwen3:8b`:
  ```powershell
  ollama pull qwen3:8b
  ```
- **Optional — cloud API keys** (Groq, OpenRouter, Gemini, Deepgram, Telegram bot) for cloud STT/brain/vision/TTS and phone control. Without them FRIDAY runs fully local.

---

## Installation

```powershell
# 1. Clone and enter the project
git clone <your-repo-url>
cd <repo>\software

# 2. Create the environment (the patched Open Interpreter wheel is pinned in pyproject.toml)
python -m venv .venv
.venv\Scripts\activate
poetry install          # or: pip install -r requirements_freeze.txt

# 3. Re-apply the 3 hand-patched Open Interpreter files from this repo
#    (copy the matching files from site-packages-patches\ back into the venv):
copy .\site-packages-patches\interpreter\core\computer\display\display.py      .venv\Lib\site-packages\interpreter\core\computer\display\
copy .\site-packages-patches\interpreter\core\computer\display\friday_locate.py .venv\Lib\site-packages\interpreter\core\computer\display\
copy .\site-packages-patches\interpreter\core\computer\vision\vision.py         .venv\Lib\site-packages\interpreter\core\computer\vision\
```

The patches replace Open Interpreter's broken remote `/point/` display API with FRIDAY's own local vision chain (`friday_locate.py`), so her "look at the screen / find X" commands work without a third-party service.

### API credentials

FRIDAY reads her API keys from `%USERPROFILE%\.friday\api_credentials.json`. A ready-to-fill template lives at `dot_friday\api_credentials.example.json` — copy it there and replace every `"(API needed here)"`:

```json
{
  "groq_stt": ["(API needed here)"],
  "groq_brain": ["(API needed here)"],
  "openrouter_brain": ["(API needed here)"],
  "openrouter_vision": ["(API needed here)"],
  "gemini_brain": ["(API needed here)"],
  "gemini_vision": ["(API needed here)"],
  "deepgram": ["(API needed here)"],
  "telegram_bot_token": "(API needed here)",
  "telegram_owner_username": "@your_telegram_username",
  "telegram_allowed_chat_ids": ["YOUR_TELEGRAM_CHAT_ID"]
}
```

> Keys are **lists**: add several keys per job and FRIDAY rotates through them under rate limits. The file is gitignored — never commit real keys.

---

## Running the app

Everything starts from `software\main.py` (a `typer` CLI):

```powershell
# Desktop launch — opens the HUD immediately
cd software
.\.venv\Scripts\python.exe main.py --status-ui

# Same thing via the convenience script
powershell -ExecutionPolicy Bypass -File .\start_friday.ps1
```

**Hidden / auto-start (tray):**

```powershell
.\.venv\Scripts\python.exe main.py --status-ui --background
```

`--background` boots FRIDAY silently into the system tray: the brain and Telegram run, the HUD stays hidden until you pick **Open App** from the tray icon. To auto-start at Windows login, use the **Settings** tab of the HUD (writes the registry `Run` key). `software\friday_boot.vbs` is the boot wrapper the registry entry invokes.

**Useful flags:**

| Flag | What it does |
|---|---|
| `--profile <name>` | Pick a profile from `source\server\profiles\` (`default`, `fast`, `local`) |
| `--server light` (default) | Local FastAPI server on port **10101** |
| `--server livekit` | Experimental LiveKit voice (needs `livekit-server`) |
| `--server-port 10101` | Override the server port |
| `--expose --domain <ngrok>` | Expose the server over the internet (ngrok) |
| `--qr` | Print a QR code with connection info |
| `--debug` | Latency measurements + raw mic recordings |
| `--profiles` | Open the profiles folder |

Startup takes 30–60s on first boot (the voice brain loads first). FRIDAY answers `http://localhost:10101/ping` when ready. A second launch while she's running just raises the app window instead of duplicating the server.

---

## Where to go and what to do

| Place | What you can do there |
|---|---|
| **HUD** (`--status-ui`) | Chat, AI Core (model/router status), Tasks, **Calendar** (voice-scheduled events), **Memory** (facts learned), Knowledge Base, Tools, Workflows, **Settings** (auto-start toggle, tray) |
| **Telegram** | Message your bot from your phone: text FRIDAY, ask for a screenshot (`"take a screenshot"` / `"show me your screen"`), get confirmations. Only allowlisted chat IDs get replies — everyone else is silent. |
| `source\server\profiles\` | Your assistant's brain settings: model, TTS, skills folder, system prompt. `default.py` is the active one; `jarvis_persona.md` carries her personality. |
| `software\skills\` | Skills folder FRIDAY's tools can be added to (empty mount point in this repo). |
| `%USERPROFILE%\.friday\` | Runtime data: `api_credentials.json` (keys), `lessons.md` (learned lessons, injected into each turn), `scratchpad.md`, `exhaustion_ledger.json` (rate-limit cooldowns), `logs\friday.log`, `audit\` |
| `memory\project_notes.md` | FRIDAY's note-taking — she appends summaries here after working sessions. |
| `pc_memory.md` | Verified system memory (your username, app paths, learned hotkeys/commands). |

## Directory structure

```
software\
├── main.py                 → entry point (typer CLI: --status-ui / --background / --profile …)
├── pyproject.toml          → dependencies; pins the patched Open Interpreter wheel (tmp-oi\)
├── requirements_freeze.txt → pip freeze snapshot for pip-based installs
├── start_friday.ps1        → desktop launcher
├── friday_boot.vbs         → hidden boot wrapper used by the registry auto-start
├── memory\project_notes.md → FRIDAY's working notes
├── skills\                 → skills mount point
├── tmp-oi\*.whl            → the patched open-interpreter wheel (required by the build)
├── site-packages-patches\  → the 3 hand-patched interpreter files (re-apply after install)
└── source\
    ├── clients\light-python\client.py  → the desktop voice client
    └── server\
        ├── server.py         → core loop: wake word, STT, brain routing, tools, TTS
        ├── brain_router.py   → weakest-first model routing + per-provider failover
        ├── cloud_stt.py      → cloud STT with key rotation (Groq → Deepgram)
        ├── gemini_tts.py     → cloud-first TTS chain (Gemini → local edge-tts)
        ├── api_pools.py      → sealed quota pools (one job per API key)
        ├── command_router.py → deterministic, risk-gated PC-control phrases
        ├── memory.py         → SQLite long-term memory
        ├── self_improve.py   → lessons ledger injected live into each turn
        ├── reminders.py / schedule_store.py / note_taker.py / digest.py / proactive.py
        ├── remote_telegram.py → phone control (allowlisted)
        ├── display_patch.py / kernel_display_patch.py → local vision chain for OI
        ├── screen_understanding.py / windows_control.py / ui_automation.py / web_scrape.py
        ├── focus_tracker.py / windows_context.py / desktop_ear.py / intent_judge.py
        ├── social_guard.py / speech_filters.py / loop_guard.py / audit.py / redaction.py
        ├── startup_settings.py / file_logger.py / persona_flair.py / status_ui.py
        ├── delegation.py / agent_coordinator.py / mcp_runtime.py / home_assistant.py (opt-in, off by default)
        ├── profiles\         → default.py (active) · fast.py · local.py · jarvis_persona.md
        ├── ui\               → HUD (index.html · app.js · style.css)
        ├── livekit\          → experimental LiveKit voice server
        └── utils\            → small helpers (get_system_info)
```

Supporting docs: `CONTEXT.md` (design philosophy), `ROADMAP.md` (what's planned), `USES.md` (use cases).

---

## Configuration knobs

FRIDAY is highly configurable through environment variables (**`FRIDAY_*`**). Important ones:

- `FRIDAY_CREDENTIALS_FILE` — path to the credentials JSON (default `~/.friday/api_credentials.json`)
- `FRIDAY_MEMORY_DB` · `FRIDAY_SCHEDULE_FILE` · `FRIDAY_REMINDERS_DB` — storage locations
- `FRIDAY_SCRATCH_DIR` — where screenshots/scratchpad captures are saved
- `FRIDAY_LOG_DIR` · `FRIDAY_AUDIT_DIR` — logging/audit locations
- `FRIDAY_TELEGRAM_BOT_TOKEN` · `FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS` · `FRIDAY_TELEGRAM_OWNER_USERNAME` — override the sealed Telegram config
- `FRIDAY_GROQ_BRAIN_MODEL` · `FRIDAY_OPENROUTER_MODEL_CHAIN` · `FRIDAY_GEMINI_BRAIN_MODEL` · `FRIDAY_VISION_MODEL` — model routing
- `FRIDAY_LOCAL_BRAIN_MODEL` (default `qwen3:8b`) · `FRIDAY_FAST_MODEL`
- `FRIDAY_STT_TIMEOUT` · `FRIDAY_VISION_TIMEOUT` · `FRIDAY_TOOL_CAP` — safety/timeout tuning
- Feature toggles: `FRIDAY_DESKTOP_EAR`, `FRIDAY_DIGEST`, `FRIDAY_SOCIAL_GUARD`, `FRIDAY_WELCOME_BACK`, `FRIDAY_HOME_ASSISTANT`, `FRIDAY_MCP_CONFIG`, `FRIDAY_FOCUS_TRACKER`, …

---

## License

MIT — see `LICENSE`. (Third-party components keep their own licenses; see `pyproject.toml`.)