' Explicit Start-menu/desktop Open. Sign-in continues to use friday_boot.vbs.
' No settings, stores or stop flags are changed by this wrapper itself.
Option Explicit
Dim shell, fso, folder, launcher
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(folder, "start_friday.ps1")
shell.CurrentDirectory = folder
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & launcher & """ -OpenApp", 0, False
