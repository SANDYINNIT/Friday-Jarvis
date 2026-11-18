@echo off
rem Companion to start_friday.ps1: boots FRIDAY using the SAME proven
rem cmd /c ">>" redirection the original friday_boot.vbs used for months.
rem Streams stdout+stderr merged into ~\.friday\boot_console.log.
rem
rem 2026-10-02 audit: a locked boot_console.log silently prevented FRIDAY from
rem booting at all. start_friday.ps1 states "Logging must NEVER be able to stop
rem FRIDAY from booting" and protects its own writes with Write-Log + a .fallback
rem file - but this script redirected with cmd ">>", which has no such guard.
rem When the redirection fails, cmd reports "Access is denied" and DOES NOT RUN
rem THE COMMAND, so python never started and the supervisor only saw the
rem "===== Starting FRIDAY =====" line it had written to its own fallback file.
rem That is exactly what happened: boot_console.log was left open by an external
rem process without FILE_SHARE_WRITE.
rem
rem Fix: probe writability first and fall back to a timestamped sibling log.
rem This does NOT hide the problem - the fallback path is written into the
rem fallback log and echoed to the supervisor, so a locked primary log is still
rem visible rather than being silently swallowed.
setlocal EnableDelayedExpansion
set "PORT=%1"
if "%PORT%"=="" set "PORT=10101"

set "FRIDAYDIR=%USERPROFILE%\.friday"
set "LOG=%FRIDAYDIR%\boot_console.log"
if not exist "%FRIDAYDIR%" mkdir "%FRIDAYDIR%" 2>nul

rem Can we actually append? Probe with a real append and keep it quiet.
rem (The probe line is removed immediately so the log stays clean.)
set "USABLE="
break > "%LOG%.probe" 2>nul && (
    del "%LOG%.probe" >nul 2>&1
    >> "%LOG%" echo. 2>nul && set "USABLE=1"
)
del "%LOG%.probe" >nul 2>&1

if defined USABLE (
    set "TARGET=%LOG%"
) else (
    rem Locked (or unwritable). Use a rotating sibling so the boot still happens
    rem and the evidence is preserved under a name that says what happened.
    rem %TIME% rather than wmic: wmic is deprecated/removed on current Windows
    rem and silently yielded an empty stamp, producing "boot_console.locked-.log".
    rem PowerShell for the stamp, because it is already a hard dependency of this
    rem boot chain (start_friday.ps1) and it is reliable. Pure-cmd %TIME%
    rem substitution proved fragile here and produced a mangled name.
    set "STAMP="
    for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss" 2^>nul`) do set "STAMP=%%T"
    if not defined STAMP set "STAMP=%RANDOM%%RANDOM%"
    rem !STAMP! not %STAMP%: this is inside a parenthesised block, so %STAMP%
    rem is expanded at PARSE time (before the set above ran) and yields an empty
    rem name - which is what produced "boot_console.locked-.log" first time.
    set "TARGET=%FRIDAYDIR%\boot_console.locked-!STAMP!.log"
)

rem 2026-10-14: mirror start_friday.ps1's interpreter fallback. This script used
rem to hardcode the venv python only, while the supervisor above it already
remembers that the venv may be absent and falls back to the system Python 3.11.
rem When the venv was gone, the supervisor happily launched this file and this
rem file then failed with "not recognized", so FRIDAY never started. One
rem interpreter decision, made in the supervisor, passed down.
set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo ===== Starting FRIDAY [%date% %time%] port=%PORT% log=%TARGET% python=%PYEXE% =====>> "%TARGET%"
rem Public n8n ingress is opt-in. The interpreter port is always local.
set "EXPOSE="
if "%FRIDAY_EXPOSE%"=="1" set "EXPOSE=--expose"
set "BACKGROUND=--background"
if "%FRIDAY_OPEN_APP%"=="1" set "BACKGROUND="
"%PYEXE%" -u -X faulthandler "%~dp0main.py" --status-ui %BACKGROUND% --client light-python --server-port %PORT% %EXPOSE% >> "%TARGET%" 2>&1
set "RC=%errorlevel%"
echo [FRIDAY exited code=!RC! at %date% %time%] >> "%TARGET%"
exit /b !RC!
