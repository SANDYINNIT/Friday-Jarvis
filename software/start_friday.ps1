# start_friday.ps1 - supervised FRIDAY launcher for the LIVE tree.
#
# Runs main.py and KEEPS RUNNING IT: if FRIDAY dies for any reason, she is
# relaunched automatically. Every death is logged to exit_trace.log with the
# process exit code + uptime, which is the evidence we need to finally find the
# "mystery killer" (taskkill /F => 1, os._exit/intentional => 0, real crash =>
# nonzero with faulthandler trace in the console).
#
# Intentional stop: create the stop flag file while FRIDAY is running, e.g.
#     New-Item "$env:USERPROFILE\.friday\stop_friday.flag" -Force
# The next exit disarms the loop (the flag is removed). The tray Exit menu in
# status_ui writes this flag automatically. No flag + no crash = FRIDAY keeps
# coming back, so the always-on text/voice channel survives.

param(
  [int]$Port = 10101,
  [switch]$OpenApp
)

$ErrorActionPreference = "Stop"
$env:FRIDAY_OPEN_APP = if ($OpenApp) { "1" } else { "0" }

function Show-FridayStartupError {
    param([string]$Message)
    if ($OpenApp) {
        try {
            $popup = New-Object -ComObject WScript.Shell
            $null = $popup.Popup($Message, 15, "FRIDAY could not open", 16)
        } catch { }
    }
}
$audioStartupCheck = [string]$env:FRIDAY_AUDIO_STARTUP_CHECK
if ($audioStartupCheck -cnotin @("", "0", "1")) {
    Write-Error "FRIDAY_AUDIO_STARTUP_CHECK must be unset, 0 or 1"
    exit 2
}
$venvPy  = (Join-Path $PSScriptRoot ".venv/Scripts/python.exe")
$sysPy   = (Join-Path $env:LOCALAPPDATA "Programs/Python/Python311/python.exe")
if (-not (Test-Path -LiteralPath $venvPy)) { $venvPy = $sysPy }
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Error "No python interpreter found (venv + system Python 3.11 both missing)."
    exit 9
}

$fridayDir  = Join-Path $env:USERPROFILE ".friday"
$stopFile   = Join-Path $fridayDir "stop_friday.flag"
$consoleLog = Join-Path $fridayDir "boot_console.log"
$consoleErr = Join-Path $fridayDir "boot_console_err.log"
$exitTrace  = Join-Path $fridayDir "exit_trace.log"
$mainPy     = (Join-Path $PSScriptRoot "main.py")

# Logging must NEVER be able to stop FRIDAY from booting.
#
# This used to be a real, permanent failure: a stale handle on boot_console.log
# (left by a killed supervisor, or by a second supervisor racing the first)
# makes every Add-Content throw "the process cannot access the file because it
# is being used by another process". Because this script runs with
# $ErrorActionPreference = "Stop", that exception killed the supervisor on its
# very first log line - BEFORE it ever reached the launch loop - so FRIDAY could
# not start at all and nothing in the log explained why.
#
# All logging now goes through Write-Log, which swallows write failures (and
# retries once against a fallback file) so a locked log can never wedge boot.
#
# 2026-10-14: Write-Log used to `return $true` / `return $false`. In PowerShell
# a bare `return <value>` writes to the OUTPUT stream, and every call site here
# is a bare statement (`Write-Log -Path ... -Value ...`), so each log line
# printed a bare `True` or `False` to stdout. On a desktop launch the console
# is visible, so the user saw exactly this reported symptom: a CMD window
# opening, printing "False", and no FRIDAY. `False` appeared (not `True`)
# because boot_console.log was locked by an external handle, which is the
# catch branch. Reproduced: running the function with a locked file under
# `powershell -File` emits `False` / `False` on stdout.
#
# A logging helper must NEVER write to the output stream. The outcome now
# goes to $script:LogLastOk, which is what the supervisor actually reads.
$script:LogFallbackWarned = $false
$script:LogLastOk = $true
function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )
    $script:LogLastOk = $false
    try {
        Add-Content -LiteralPath $Path -Value $Value -ErrorAction Stop
        $script:LogLastOk = $true
    } catch {
        if ($Path -eq $consoleLog -and -not $script:LogFallbackWarned) {
            $script:LogFallbackWarned = $true
            try {
                $alt = $Path + ".fallback"
                Add-Content -LiteralPath $alt -Value ("[log-locked] " + $Value) -ErrorAction Stop
                $script:LogLastOk = $true
            } catch {
                # Nothing left to try. Booting matters more than the log.
            }
        }
    }
}

if (-not (Test-Path -LiteralPath $fridayDir)) { New-Item -ItemType Directory -Path $fridayDir | Out-Null }

# Supervisor singleton.
#
# 2026-10-14: there was NO mutual exclusion between supervisors. Windows runs
# the HKCU Run value (friday_boot.vbs -> this script) at logon, and the desktop
# shortcut runs THIS SAME SCRIPT again on a double-click. Two supervisors then
# each run their own `Start-Process cmd.exe start_friday.cmd` loop, both bind
# port 10101, and one loses the bind race and dies immediately - the supervisor
# then "restarts in 8s" into the same losing race. exit_trace.log shows exactly
# this: three consecutive `exit=0 uptime_s=1` records on 2026-10-02, 11 s apart.
#
# A named mutex makes the second supervisor exit immediately and quietly, so
# the already-running FRIDAY is simply raised instead of being fought over.
$mutexName = "Global\FRIDAYSupervisor_" + ($env:USERNAME -replace '\\', '_')
$script:SupervisorMutex = $null
$acquiredMutex = $false
try {
    $script:SupervisorMutex = New-Object System.Threading.Mutex($false, $mutexName)
    $acquiredMutex = $script:SupervisorMutex.WaitOne(0)
} catch {
    # If the mutex itself cannot be created we must NOT block the boot; fall
    # through unsupervised rather than refusing to start FRIDAY at all.
    $acquiredMutex = $true
}
if (-not $acquiredMutex) {
    # Another supervisor already owns FRIDAY. Tell it to show the HUD, then
    # leave without touching the port. Exit 0 so nothing treats this as a crash.
    if ($OpenApp) {
        $shown = $false
        # A supervisor can own the mutex while its worker is still loading.
        # A second Open must not create another worker or disappear silently.
        for ($attempt = 0; $attempt -lt 6; $attempt++) {
            try {
                $reply = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/open_app" -TimeoutSec 2
                if ($reply -eq "FRIDAY window raised") { $shown = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 500
        }
        if (-not $shown) { Show-FridayStartupError "FRIDAY is already starting or stopping, but its window could not be restored. Wait a moment and try Open again. No second assistant was started." }
    }
    exit 0
}

# An explicit Open acknowledges a completed intentional Exit. Automatic
# sign-in still consumes the marker and remains stopped. Do this before any
# provider preflight so an intentional stop cannot start Ollama.
if (Test-Path -LiteralPath $stopFile) {
    Remove-Item -LiteralPath $stopFile -Force
    if (-not $OpenApp) {
        Write-Log -Path $exitTrace -Value ("[stop] supervisor honouring pending stop flag, exiting at " + (Get-Date -Format o))
        exit 0
    }
    Write-Log -Path $exitTrace -Value "[open] explicit user launch acknowledged the previous intentional stop"
}

# Pre-flight: FRIDAY's primary brain (qwen3:8b) runs through a local Ollama.
# A dead Ollama wedges FRIDAY's boot silently for minutes, so make sure it is
# answering before we start her. Non-fatal: FRIDAY can still fail over to the
# cloud pools, but only after a long boot wait we'd rather avoid.
if ($audioStartupCheck -ne "1") {
    $ollamaUp = $false
    try {
        $resp = curl.exe -s --max-time 2 "http://localhost:11434/api/tags"
        if ($LASTEXITCODE -eq 0 -and $resp) { $ollamaUp = $true }
    } catch { }
    if (-not $ollamaUp) {
        Write-Log -Path $consoleLog -Value ("[supervisor] Ollama not responding - starting it")
        try {
            Start-Process (Join-Path $env:LOCALAPPDATA "Programs/Ollama/ollama.exe") -ArgumentList @("serve") -WindowStyle Hidden
            for ($i = 0; $i -lt 15; $i++) {
                Start-Sleep -Seconds 2
                if (curl.exe -s --max-time 2 "http://localhost:11434/api/tags" | Out-String) { $ollamaUp = $true; break }
            }
        } catch {
            Write-Log -Path $consoleLog -Value ("[supervisor] Ollama start failed: " + $_.Exception.Message)
        }
        Write-Log -Path $consoleLog -Value ("[supervisor] Ollama ready = " + $ollamaUp)
    }
}

$launcherCmd = Join-Path $PSScriptRoot "start_friday.cmd"
$quick = 0
$listenerRecoveries = 0
while ($true) {
    $stamp  = Get-Date -Format "s"
    $start  = Get-Date
    Write-Log -Path $consoleLog -Value ("`r`n===== Starting FRIDAY ($stamp) port=$Port =====")

    $p = $null
    try {
        $before = Get-Date
        $p = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$launcherCmd`" $Port") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
        # Start-Process -Wait follows descendants too. FRIDAY may spawn the
        # separate edge-TTS service, so wait for the launcher process itself;
        # otherwise an exited FRIDAY can leave this supervisor blocked forever.
        $p.WaitForExit()
        $p.Refresh()
    } catch {
        Write-Log -Path $consoleLog -Value ("[supervisor] launch ERROR: " + $_.Exception.Message)
        Write-Log -Path $exitTrace -Value ("[launch-error] " + $_.Exception.Message + " at " + (Get-Date -Format o))
        Start-Sleep -Seconds 15
        continue
    }

    $uptime = [int](Get-Date).Subtract($before).TotalSeconds
    $code   = $p.ExitCode

    Write-Log -Path $consoleLog -Value ("[supervisor] FRIDAY exited code=$code after ${uptime}s at " + (Get-Date -Format "s"))
    Write-Log -Path $exitTrace -Value ((Get-Date -Format o) + " pid=" + $p.Id + " exit=" + $code + " uptime_s=" + $uptime)

    # Intentional stop requested while running?
    if (Test-Path -LiteralPath $stopFile) {
        Remove-Item -LiteralPath $stopFile -Force
        Write-Log -Path $exitTrace -Value ("[stop] flag seen, supervisor exiting at " + (Get-Date -Format o))
        exit 0
    }

    if ($code -eq 78) {
        Write-Log -Path $exitTrace -Value "[stop] catalog configuration rejected; review the FRIDAY startup error before relaunching"
        if ($OpenApp) { Show-FridayStartupError "FRIDAY could not start because its saved catalog or Town configuration failed validation. Your data was not replaced. Review the local FRIDAY startup diagnostics before retrying." }
        exit 78
    }

    # The owned normal Light/HUD monitor uses 75 for startup, listener
    # and terminal HUD failure. Bound even long-lived repeat failures.
    if ($code -eq 75) {
        $listenerRecoveries++
        if ($listenerRecoveries -ge 3) {
            Write-Log -Path $exitTrace -Value "[stop] Light lifecycle recovery limit reached"
            exit 75
        }
    }

    # Exit code 0 with almost-zero uptime could be main.py's single-instance
    # guard deciding FRIDAY already runs elsewhere. Verify that claim with a
    # real /ping BEFORE giving up supervising -- a spurious boot-time signal
    # now exits cleanly (code 0), and we must NOT mistake that for another
    # instance being alive.
    $alreadyUp = $false
    try {
        $probe = curl.exe -s --max-time 3 -o NUL -w "%{http_code}" "http://127.0.0.1:$Port/ping"
        if ($LASTEXITCODE -eq 0 -and $probe -eq "200") { $alreadyUp = $true }
    } catch { }
    if ($code -eq 0 -and $uptime -lt 20 -and $alreadyUp) {
        Write-Log -Path $exitTrace -Value ("[stop] exit 0 in ${uptime}s and /ping answered (single-instance guard), supervisor exiting " + (Get-Date -Format o))
        exit 0
    }

    # Rapid-fail backoff so a boot-looping FRIDAY doesn't hammer the machine.
    if ($uptime -lt 45) { $quick++ } else { $quick = 0 }
    $delay = if ($quick -ge 3) { 120 } else { 8 }
    Write-Log -Path $consoleLog -Value ("[supervisor] restarting in ${delay}s (quick-fail count=$quick)")
    Start-Sleep -Seconds $delay
}
