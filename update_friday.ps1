<#
.SYNOPSIS
  Updates this FRIDAY checkout to the latest commit from GitHub and re-applies
  the installation steps (patched Open Interpreter files + dependency install).

.DESCRIPTION
  A plain `git pull` is not enough: the Open Interpreter wheel under tmp-oi\ can
  change with an update, and the 5 patched interpreter files under
  site-packages-patches\ must be re-copied into your Python's site-packages
  after every install/upgrade. This script does the whole job in the right order:

    1. Stash nothing, but stop short of touching YOUR data: it never edits
       dot_friday\api_credentials.json, %USERPROFILE%\.friday\, pc_memory.md,
       or any *.db / *.log you created.
    2. `git fetch` + report the incoming commit, then `git pull --ff-only`.
    3. Re-run `pip install --no-deps -r requirements_freeze.txt` (installs the
       patched open-interpreter wheel from the relative tmp-oi\ path).
    4. Re-copy the 5 patched interpreter files into the resolved site-packages.
    5. Print a short summary of what changed and how to restart FRIDAY.

  Run it whenever the upstream repository gets a new commit.

.PARAMETER Check
  Only report whether an update is available; change nothing.

.PARAMETER Force
  Run the update even when the local checkout is already up to date.

.PARAMETER NoPip
  Skip the dependency install and the patch re-copy (code-only sync; use when you
  just want the source updated but are mid-install of packages yourself).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\update_friday.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\update_friday.ps1 -Check
#>
[CmdletBinding()]
param(
  [switch]$Check,
  [switch]$Force,
  [switch]$NoPip
)

$ErrorActionPreference = "Stop"
# This script lives at the repository root (next to README.md); the Python app,
# requirements, and site-packages-patches all live under software\.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$soft = Join-Path $root "software"
Set-Location $soft

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    [ok] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    [!] $msg" -ForegroundColor Yellow }

# --- 0. git availability ----------------------------------------------------
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
  Write-Host "Git is not installed or not on PATH." -ForegroundColor Red
  Write-Host "FRIDAY's updater uses git to fetch the latest commit. Install it from https://git-scm.com/download/win and re-run." -ForegroundColor Yellow
  exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $root ".git"))) {
  Write-Host "This folder is not a git checkout (no .git here)." -ForegroundColor Red
  Write-Host "FRIDAY's updater needs the repository. If you downloaded a ZIP, re-clone it instead:" -ForegroundColor Yellow
  Write-Host "  git clone https://github.com/SANDYINNIT/Friday-Jarvis.git" -ForegroundColor Yellow
  exit 1
}

# --- 1. resolve python (venv if present, else plain python) ------------------
$venvPython = Join-Path $soft ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }
if ($python -eq "python" -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
  Write-Host "No Python found (no .venv here and 'python' is not on PATH). Install Python 3.11+ first." -ForegroundColor Red
  exit 1
}

# --- 2. fetch and report incoming commit -------------------------------------
Write-Step "Checking GitHub for updates (origin/main)..."
& git fetch --quiet origin
if ($LASTEXITCODE -ne 0) { Write-Host "git fetch failed (no network, or repo moved). See -ErrorAction output above." -ForegroundColor Red; exit 1 }

$local  = (& git rev-parse --short HEAD) | Select-Object -First 1
$remote = (& git rev-parse --short "origin/HEAD" 2>$null)
if (-not $remote) { $remote = (& git rev-parse --short "origin/main" 2>$null) }
$behind = (& git rev-list --count "HEAD..origin/HEAD" 2>$null)
if ($null -eq $behind -or $behind -eq "") { $behind = (& git rev-list --count "HEAD..origin/main") }

if ($Check) {
  if ([int]$behind -gt 0) {
    Write-Host "An update IS available: you are $behind commit(s) behind origin." -ForegroundColor Yellow
    Write-Host "Run without -Check to install it." -ForegroundColor Yellow
  } else {
    Write-Host "Already up to date (local $local = origin $remote)." -ForegroundColor Green
  }
  exit 0
}

if ([int]$behind -eq 0 -and -not $Force) {
  Write-Host "Already up to date (local $local = origin $remote). Nothing to do." -ForegroundColor Green
  Write-Host "Use -Force to re-run the install/patch steps anyway." -ForegroundColor Gray
  exit 0
}

# --- 3. pull ----------------------------------------------------------------
Write-Step "Updating source (git pull --ff-only)..."
$beforeHash = & git rev-parse HEAD
& git pull --ff-only --quiet
if ($LASTEXITCODE -ne 0) {
  Write-Host "git pull failed. You may have local edits; resolve them (git status) and re-run, or use -NoPip for a code-only sync." -ForegroundColor Red
  exit 1
}
$afterHash = & git rev-parse HEAD
if ($beforeHash -ne $afterHash) {
  $subject = (& git log -1 --pretty=%s) | Select-Object -First 1
  Write-Ok "source updated to $afterHash ($subject)"
} else {
  Write-Ok "source already at $afterHash (no file changes)"
}

# --- 4. dependencies + patched files ----------------------------------------
if ($NoPip) {
  Write-Step "Skipping dependency install and patch re-copy (-NoPip)."
  Write-Host "    If the update changed tmp-oi\ or site-packages-patches\, re-run WITHOUT -NoPip." -ForegroundColor Gray
} else {
  Write-Step "Installing dependencies (pip install --no-deps -r requirements_freeze.txt)..."
  Write-Host "    (This installs the patched open-interpreter wheel from tmp-oi\.)" -ForegroundColor Gray
  & $python -m pip install --no-deps -r requirements_freeze.txt --disable-pip-version-check
  if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "pip install reported errors. This is often a build-toolchain issue (21 pins are source-only, e.g. webrtcvad needs MSVC Build Tools)."
    Write-Warn2 "FRIDAY may still run; continue to the patch step and then try starting her."
  } else {
    Write-Ok "dependencies installed"
  }

  Write-Step "Re-applying the 5 patched Open Interpreter files into site-packages..."
  $sp = (& $python -c "import sys; print(next(p for p in sys.path if p.endswith('site-packages')))") | Select-Object -First 1
  if (-not $sp) { $sp = (& $python -c "import site; print(site.getsitepackages()[0])") | Select-Object -First 1 }
  if (-not $sp) { Write-Warn2 "Could not resolve site-packages; copy the 5 files by hand (see README)."; $sp = "" }
  if ($sp) {
    $patchFiles = @(
      "interpreter\core\computer\display\display.py",
      "interpreter\core\computer\display\friday_locate.py",
      "interpreter\core\computer\vision\vision.py",
      "interpreter\core\respond.py",
      "interpreter\core\llm\utils\merge_deltas.py"
    )
    $patchDir = Join-Path $soft "site-packages-patches"
    $okPatches = 0
    foreach ($rel in $patchFiles) {
      $src = Join-Path $patchDir $rel
      $dst = Join-Path $sp $rel
      if (-not (Test-Path -LiteralPath $src)) { Write-Warn2 "missing patch file: $rel"; continue }
      $dstDir = Split-Path -Parent $dst
      if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
      Copy-Item -LiteralPath $src -Destination $dst -Force
      $okPatches++
    }
    Write-Ok "re-applied $okPatches/5 patched interpreter files into $sp"
  }
}

# --- 5. done ----------------------------------------------------------------
Write-Step "Update complete."
Write-Host "    Restart FRIDAY to load the new code:" -ForegroundColor Gray
Write-Host "      python software\main.py --status-ui     (or .\software\start_friday.ps1)" -ForegroundColor Gray
Write-Host "    Your keys, memory, and lessons (%USERPROFILE%\.friday, dot_friday\api_credentials.json) were NOT touched." -ForegroundColor Gray
