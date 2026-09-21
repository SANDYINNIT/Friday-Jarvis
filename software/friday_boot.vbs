' FRIDAY silent auto-start: no console window ever appears.
' CRITICAL: python.exe (NOT pythonw) — RealtimeSTT's multiprocessing worker
' cannot spawn under pythonw and the tray boot dies before any log line
' (2026-09-17 boot failure). Window style 0 keeps everything hidden.
' boot_console.log captures stdout+stderr for diagnosing silent crashes
' (shell.Run does NOT parse ">>" itself, so the cmd /c wrapper is required).
Dim shell, bootLog
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "D:\01\software"
bootLog = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.friday\boot_console.log"
shell.Run "cmd /c """"D:\01\software\.venv\Scripts\python.exe"" -u -X faulthandler ""D:\01\software\main.py"" --status-ui --background --client light-python >> """ & bootLog & """ 2>&1""", 0, False
