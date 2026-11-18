[CmdletBinding()]
param([string]$Python="py")
$ErrorActionPreference='Stop'
# Use this PowerShell's bundled modules even when invoked from another PowerShell edition.
foreach ($moduleName in @('Microsoft.PowerShell.Management','Microsoft.PowerShell.Utility','Microsoft.PowerShell.Security')) {
 $moduleFile=[IO.Path]::Combine($PSHOME,('Modules/'+$moduleName+'/'+$moduleName+'.psd1'))
 Import-Module -Name $moduleFile -ErrorAction Stop
}
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows10/11 x64.' }
$root=$PSScriptRoot
[string[]]$pythonArgs = if ($Python -eq 'py') { @('-3.11') } else { @() }
& $Python @pythonArgs -I -B -c "import sys,struct;assert sys.version_info[:2]==(3,11) and struct.calcsize('P')==8, 'Install CPython3.11 x64 from python.org'"
if ($LASTEXITCODE -ne 0) { throw 'A working CPython3.11 x64 interpreter is required.' }
$lockHash=(Get-FileHash -LiteralPath (Join-Path $root 'requirements-windows.lock') -Algorithm SHA256).Hash.Substring(0,20).ToLowerInvariant()
$venv=Join-Path $root ('.venv-windows/py311-'+$lockHash)
if ((Test-Path -LiteralPath $venv) -and !(Test-Path -LiteralPath (Join-Path $venv '.friday-owned'))) { throw 'Refusing an unrecognized environment.' }
if (!(Test-Path -LiteralPath $venv)) {
 New-Item -ItemType Directory -Path $venv | Out-Null
 Set-Content -LiteralPath (Join-Path $venv '.friday-owned') -Value 'FRIDAY native environment'
 & $Python @pythonArgs -m venv $venv
 if ($LASTEXITCODE -ne 0) { throw 'venv creation failed; repair this owned environment before retrying.' }
}
$interpreter=Join-Path $venv 'Scripts/python.exe'
if (!(Test-Path -LiteralPath $interpreter)) { throw 'Incomplete owned environment; remove only that environment directory after checking the path, then rerun.' }
$marker=Join-Path $venv '.installed'
if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
# Child-only installation environment. No global Python, key file or OS registration changes.
$prior=@{}
foreach ($name in @('PIP_CONFIG_FILE','PIP_INDEX_URL','PIP_EXTRA_INDEX_URL','PIP_FIND_LINKS','PIP_TRUSTED_HOST','PIP_TARGET','PIP_PREFIX','PIP_USER','UV_SYSTEM_CERTS','UV_INDEX','UV_INDEX_URL','UV_DEFAULT_INDEX','UV_EXTRA_INDEX_URL','UV_FIND_LINKS','UV_CONFIG_FILE','UV_INSECURE_HOST','UV_SYSTEM_PYTHON')) { $prior[$name]=[Environment]::GetEnvironmentVariable($name,'Process'); Remove-Item -LiteralPath ('Env:'+$name) -ErrorAction SilentlyContinue }
$env:PIP_CONFIG_FILE='NUL'
Push-Location $root
try {
 $pipVersion = & $interpreter -I -B -c "import importlib.metadata;print(importlib.metadata.version('pip'))"
 [string[]]$trustArgs = if ([version]$pipVersion -lt [version]'24.2') { @('--use-feature=truststore') } else { @() }
 & $interpreter -m pip install @trustArgs --no-cache-dir --disable-pip-version-check --retries 1 --timeout 30 --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements-bootstrap.lock
 if ($LASTEXITCODE -ne 0) { throw 'Isolated build-tool installation failed.' }
 $uv=Join-Path $venv 'Scripts/uv.exe'
 if (!(Test-Path -LiteralPath $uv)) { throw 'Pinned installer helper missing.' }
 $env:UV_SYSTEM_CERTS='true'
 & $uv --no-config pip install --python $interpreter --require-hashes --no-build-isolation --index-url https://pypi.org/simple -r requirements-windows.lock
 if ($LASTEXITCODE -ne 0) { throw 'Native dependencies failed. See troubleshooting; no successful-install marker was saved.' }
 & $interpreter -m pip check
 if ($LASTEXITCODE -ne 0) { throw 'Installed dependency constraints are inconsistent.' }
 & $interpreter -I -B packaging/tokenizer_cache.py install
 if ($LASTEXITCODE -ne 0) { throw 'Tokenizer setup failed; no model weights were downloaded.' }
 Set-Content -LiteralPath $marker -Value $lockHash
 Write-Output 'Installed native dependencies. Next: .\run-windows.ps1 -Setup'
} finally {
 Pop-Location
 foreach ($name in $prior.Keys) { if ($null -eq $prior[$name]) { Remove-Item -LiteralPath ('Env:'+$name) -ErrorAction SilentlyContinue } else { [Environment]::SetEnvironmentVariable($name,$prior[$name],'Process') } }
}
