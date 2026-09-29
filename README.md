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
- **Git for Windows** — so you can clone and later pull updates with the bundled
  updater (<https://git-scm.com/download/win>).
- **Python 3.11** — a virtualenv is *optional*; FRIDAY runs against any plain Python 3.11 install.
- **pip** for installing dependencies (Poetry also works via `pyproject.toml`).
- **Ollama** running locally with at least `qwen3:8b`:
  ```powershell
  ollama pull qwen3:8b
  ```
- **Optional — cloud API keys** (Groq, OpenRouter, Gemini, Deepgram, Telegram bot) for cloud STT/brain/vision/TTS and phone control. Without them FRIDAY runs fully local.
- **Optional — Microsoft C++ build tools** only if a fully clean `pip install` tries to compile a source-only package (e.g. `webrtcvad`); FRIDAY's core does not require it.

---

## Installation

> **Read this first if something goes wrong:** the steps below are ordered and
> each one depends on the last. The three that people most often get wrong are
> (1) installing with `--no-deps`, (2) copying the **5** patched files (there are
> five, not three), and (3) running from the `software\` folder. See
> [Troubleshooting](#troubleshooting) for exact fixes.

FRIDAY is a **Windows** app. Everything below is PowerShell.

### Step 0 — Get the code

```powershell
git clone https://github.com/SANDYINNIT/Friday-Jarvis.git
cd Friday-Jarvis\software
```

Keep this as a **git clone** (not a ZIP download) — that is what lets you pull
future updates with the one-line updater below.

### Step 1 — Prerequisites

- **Python 3.11** (check with `python --version`). A virtualenv is optional.
- **Ollama** running, with the brain model pulled:
  ```powershell
  ollama pull qwen3:8b
  ```
- Optional: [LiveKit server](https://github.com/livekit/livekit/releases) only
  if you want the experimental `--server livekit` voice (skip it otherwise).

### Step 2 — Install the dependencies

Run from inside `software\`:

```powershell
pip install --no-deps -r requirements_freeze.txt
```

- `--no-deps` is **required**. The freeze is an exact, tested closure; letting
  pip resolve dependencies itself breaks it (e.g. litellm needs
  `openai>=2.20,<3.0` while `livekit-plugins-openai` pins `openai~=1.35`).
- This step also installs the patched Open Interpreter wheel from the relative
  `tmp-oi\` path, so it must be run with `software\` as the working directory.
- If pip tries to **compile** a package (you'll see "Building wheel for …"), it
  needs Microsoft's C++ build tools first — install "Visual Studio Build Tools"
  with the C++ workload from https://visualstudio.microsoft.com/visual-cpp-build-tools/
  and re-run. 21 of the frozen pins are source-only (`webrtcvad`, `PyAutoGUI`,
  `PyGetWindow`, `pytweening`, `docopt`, `html2text`, `wget`, `PyRect`,
  `proxy_tools`, `MouseInfo`, `PyMsgBox`, `pyperclip`, `PyScreeze`, `encodec`,
  `gruut*`). `webrtcvad` has no prebuilt wheel anywhere. FRIDAY's core does not
  need these, but a fully clean install will.

### Step 3 — Re-apply the 5 patched Open Interpreter files

The patched wheel already contains these, but re-applying them guarantees the
exact files this repo ships are what you run (and undoes any later pip
overwrite). This Python snippet prints the right `site-packages` folder for you:

```powershell
$sp = & python -c "import sys; print(next(p for p in sys.path if p.endswith('site-packages')))"
copy .\site-packages-patches\interpreter\core\computer\display\display.py      "$sp\interpreter\core\computer\display\"
copy .\site-packages-patches\interpreter\core\computer\display\friday_locate.py "$sp\interpreter\core\computer\display\"
copy .\site-packages-patches\interpreter\core\computer\vision\vision.py         "$sp\interpreter\core\computer\vision\"
copy .\site-packages-patches\interpreter\core\respond.py                        "$sp\interpreter\core\"
copy .\site-packages-patches\interpreter\core\llm\utils\merge_deltas.py         "$sp\interpreter\core\llm\utils\"
```

Those patches replace Open Interpreter's broken remote `/point/` display API with
FRIDAY's own local vision chain (`friday_locate.py`), so her "look at the screen
/ find X" commands work without a third-party service.

### Step 4 — (Optional) API keys

FRIDAY runs fully local with no keys. To enable cloud STT/brain/vision/TTS and
Telegram, see [API credentials](#api-credentials) below.

### Step 5 — Run it

From inside `software\`:

```powershell
python main.py --status-ui
```

Startup takes 30–60 s the first time (the brain loads). When she's ready she
answers `http://localhost:10101/ping` with `pong`. **If that shows `pong`, your
install is good.**

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
python main.py --status-ui

# Same thing via the convenience script
# (uses .venv\Scripts\python.exe when present, otherwise the plain `python`)
powershell -ExecutionPolicy Bypass -File .\start_friday.ps1
```

**Hidden / auto-start (tray):**

```powershell
python main.py --status-ui --background
```

> Use **the same interpreter you installed dependencies into** everywhere above — `python` when you skipped the venv, or `activate` your venv first if you made one.

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

## Keeping it updated

This project is kept up to date on GitHub. When a new version is committed
upstream, pull it into your install with the bundled updater — it does the
whole job (code + dependencies + re-applies the patched files), so you never
have to repeat the install steps by hand.

Run it from the **repository root** (the folder containing `README.md`):

```powershell
# Update to the latest commit (fetch → pull → pip install → re-apply patches)
powershell -ExecutionPolicy Bypass -File .\update_friday.ps1
```

Useful flags:

| Flag | What it does |
|---|---|
| *(none)* | Update code, install deps, re-apply the 5 patched files, then tell you to restart |
| `-Check` | Just say whether a newer commit exists; change nothing |
| `-Force` | Re-run the install/patch steps even if you're already up to date |
| `-NoPip` | Update the source only (skip `pip install` and the patch re-copy) |

After it finishes, restart FRIDAY to load the new code. Your keys and data
(`dot_friday\api_credentials.json`, `%USERPROFILE%\.friday\`, `pc_memory.md`,
your `*.db`) are **never** touched by the updater.

> The updater uses `git`, so keep FRIDAY as a **git clone** (Step 0). If you
> ever re-install as a plain ZIP, the updater will tell you to re-clone instead.
> Prefer not to hand-edit files inside the checkout — local edits can cause the
> `git pull` to stop; commit them on a branch or use `-NoPip` and copy them in.

---

## Troubleshooting

**`/ping` never answers, or the window doesn't appear**
- Make sure you're running from the `software\` folder and with the same Python
  you installed into. Confirm the brain is loaded: `curl http://localhost:10101/ping`.
- First boot loads the brain and can take up to a minute; wait and retry.

**`ModuleNotFoundError: No module named 'open_interpreter'` / imports fail**
- The `pip install` (Step 2) didn't finish. Re-run it from `software\` with
  `--no-deps`. If it complained about building a wheel, install the C++ build
  tools first (see Step 2).

**Her "look at the screen" / find-on-screen commands fail, or you see `/point/`**
- The 5 patched files (Step 3) aren't applied. Re-run that step (or the
  updater, which does it for you).

**`pip install` resolution errors (`openai` conflict, etc.)**
- You dropped `--no-deps`. Use the exact command: `pip install --no-deps -r requirements_freeze.txt`.

**Port 10101 already in use**
- Something else (or a second FRIDAY) is on the port. Either stop it, or start
  FRIDAY on a different one: `python main.py --status-ui --server-port 10102`.

**`--server livekit` exits with a message about `livekit-server`**
- That binary isn't installed by pip. Download it from
  <https://github.com/livekit/livekit/releases> (Windows:
  `livekit_1.13.7_windows_amd64.zip`), put it on your PATH, and re-run. The
  default `--server light` needs none of this.

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
Friday-Jarvis\
├── README.md / AGENTS.md    → these docs
├── update_friday.ps1        → one-command updater (git pull + install + re-apply patches)
└── software\
    ├── main.py                 → entry point (typer CLI: --status-ui / --background / --profile …)
    ├── pyproject.toml          → dependencies; pins the patched Open Interpreter wheel (tmp-oi\)
    ├── requirements_freeze.txt → pip freeze snapshot for pip-based installs
    ├── start_friday.ps1        → desktop launcher
    ├── friday_boot.vbs         → hidden boot wrapper used by the registry auto-start
    ├── memory\project_notes.md → FRIDAY's working notes
    ├── skills\                 → skills mount point
    ├── tmp-oi\*.whl            → the patched open-interpreter wheel (required by the build)
    ├── site-packages-patches\  → the 5 hand-patched interpreter files (re-apply after install)
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
        ├── display_patch.py / → local vision chain for OI
        ├── screen_understanding.py / windows_control.py / ui_automation.py / web_scrape.py
        ├── focus_tracker.py / windows_context.py / desktop_ear.py / intent_judge.py
        ├── social_guard.py / speech_filters.py / audit.py / redaction.py
        ├── startup_settings.py / file_logger.py / persona_flair.py / status_ui.py
        ├── delegation.py / agent_coordinator.py / mcp_runtime.py / home_assistant.py (opt-in, off by default)
        ├── profiles\         → default.py (active) · fast.py · local.py · jarvis_persona.md
        ├── ui\               → HUD (index.html · app.js · style.css)
        ├── livekit\          → experimental LiveKit voice server
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

**AGPL-3.0** — see `LICENSE`. (The project is a fork of the open-source **01** voice interface by Open Interpreter, itself AGPL-3.0. The vendored Open Interpreter fork ships under its own license — see `software/LICENSE`.)