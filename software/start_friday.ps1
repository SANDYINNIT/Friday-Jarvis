$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root

# No local TTS warmup at boot: the voice is cloud-first (Gemini 3.1 Flash TTS).
# The local edge-tts server is started lazily by source/server/gemini_tts.py,
# and only the moment both cloud TTS accounts fail.

& $python "main.py" --status-ui
