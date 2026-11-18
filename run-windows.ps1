[CmdletBinding()]
param([switch]$Setup,[switch]$Check,[switch]$EnableDevices,[switch]$Configure,[switch]$N8N,[Parameter(ValueFromRemainingArguments=$true)][string[]]$ConfigArgs)
$ErrorActionPreference='Stop'
# Use this PowerShell's bundled modules even when invoked from another PowerShell edition.
foreach ($moduleName in @('Microsoft.PowerShell.Management','Microsoft.PowerShell.Utility','Microsoft.PowerShell.Security')) {
 $moduleFile=[IO.Path]::Combine($PSHOME,('Modules/'+$moduleName+'/'+$moduleName+'.psd1'))
 Import-Module -Name $moduleFile -ErrorAction Stop
}
if ($env:OS -ne 'Windows_NT') { throw 'Native Windows launcher only. Linux uses run-linux.sh.' }
$hash=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'requirements-windows.lock') -Algorithm SHA256).Hash.Substring(0,20).ToLowerInvariant()
$venv=Join-Path $PSScriptRoot ('.venv-windows/py311-'+$hash)
$python=Join-Path $venv 'Scripts/python.exe';$marker=Join-Path $venv '.installed'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker).Trim() -ne $hash) { throw 'Run .\install-windows.ps1 first; no matching verified native environment exists.' }
if ((@($Setup,$Check,$Configure,$N8N,$EnableDevices) | Where-Object { [bool]$_ }).Count -gt 1) { throw 'Choose one setup/check/configure/native launch mode.' }
$private=Join-Path $env:LOCALAPPDATA 'FRIDAY'
if (!(Test-Path -LiteralPath $private) -and ($Check -or (($Configure -or $N8N) -and $ConfigArgs.Count -gt 0 -and $ConfigArgs[0] -in @('show','check','test')))) { throw 'No saved profile. Run -Setup; check did not create a directory or change permissions.' }
if (!(Test-Path -LiteralPath $private)) {
 New-Item -ItemType Directory -Path $private | Out-Null
 $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
 & icacls.exe $private /inheritance:r /grant:r "*$($sid):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
 if ($LASTEXITCODE -ne 0) { throw 'Private profile ACL could not be set. Do not save keys until permissions are repaired.' }
}
if ((Get-Item -LiteralPath $private).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Private profile cannot be redirected through a junction/symlink.' }
# Verify the exact profile subtree has no extra principals before accepting secrets.
$acl=Get-Acl -LiteralPath $private
$owner=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed=@($owner,'S-1-5-18','S-1-5-32-544')
if (!$acl.AreAccessRulesProtected) { throw 'Profile permissions are inherited; review/protect only the FRIDAY profile, never its parent.' }
foreach ($rule in $acl.Access) {
 $sid=$rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
 if ($sid -notin $allowed) { throw 'Profile permissions include another principal; review only this profile before saving credentials.' }
}
$configFiles=@('routes.json','api_credentials.json','n8n.json')
foreach ($variable in @('FRIDAY_USER_ROUTES','FRIDAY_CREDENTIALS_FILE','FRIDAY_N8N_CONFIG')) {
 $value=[Environment]::GetEnvironmentVariable($variable,'Process')
 if ($value) {
  $resolved=[IO.Path]::GetFullPath($value)
  if ([IO.Path]::GetDirectoryName($resolved) -ne [IO.Path]::GetFullPath($private)) { throw 'Windows configuration overrides must remain directly inside the protected FRIDAY profile; another installation was not opened.' }
  $configFiles += [IO.Path]::GetFileName($resolved)
 }
}
foreach ($name in $configFiles) {
 $file=Join-Path $private $name
 if (Test-Path -LiteralPath $file) {
  if ((Get-Item -LiteralPath $file).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Private configuration cannot be a redirected file.' }
  foreach ($rule in (Get-Acl -LiteralPath $file).Access) {
   $sid=$rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
   if ($sid -notin $allowed) { throw 'A private configuration file grants another principal access; review only that file.' }
  }
 }
}
$entry=Join-Path $PSScriptRoot 'software/public_entry.py'
if ($Setup) { & $python -I -B $entry setup }
elseif ($Check) { & $python -I -B $entry check }
elseif ($Configure) { & $python -I -B $entry configure @ConfigArgs }
elseif ($N8N) { & $python -I -B $entry n8n @ConfigArgs }
else {
 if ($EnableDevices) { & $python -I -B $entry windows --enable-devices }
 else { & $python -I -B $entry windows }
}
exit $LASTEXITCODE
