$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Run with the venv interpreter when the project ships/has one, otherwise the
# plain `python` on PATH (works without a virtualenv entirely).
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }

# No local TTS warmup at boot: the voice is cloud-first (Gemini 3.1 Flash TTS).
# The local edge-tts server is started lazily by source/server/gemini_tts.py,
# and only the moment both cloud TTS accounts fail.

& $python "main.py" --status-ui