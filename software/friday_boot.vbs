' FRIDAY silent auto-start: no console window ever appears.
' CRITICAL: python.exe (NOT pythonw) — RealtimeSTT's multiprocessing worker
' cannot spawn under pythonw and the tray boot dies before any log line
' (2026-09-17 boot failure). Window style 0 keeps everything hidden.
' boot_console.log captures stdout+stderr for diagnosing silent crashes
' (shell.Run does NOT parse ">>" itself, so the cmd /c wrapper is required).
' Interpreter: the plain `python` on PATH — no virtualenv required. It must be
' the same Python 3.11 the dependencies were installed into.
Dim shell, fso, proj, bootLog
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
proj = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = proj
bootLog = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.friday\boot_console.log"
cmdLine = "cmd /c ""python"" -u -X faulthandler """ & proj & "\main.py"" --status-ui --background --client light-python >> """ & bootLog & """ 2>&1"""
shell.Run cmdLine, 0, False