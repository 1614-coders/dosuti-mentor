param(
    [string]$HomePath = (Join-Path $env:LOCALAPPDATA 'DoSutiMentor'),
    [string]$Python = '',
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $root 'Windows-Runtime.ps1')
if ($env:PROCESSOR_ARCHITECTURE -notmatch 'AMD64|x86_64') { throw 'Windows setup currently supports x64 only.' }

$home = Initialize-DoSutiHome $HomePath
$spec = Get-Content -LiteralPath (Join-Path $root 'plugins/dosuti-mentor/references/windows-python.json') -Raw | ConvertFrom-Json
$selected = Resolve-DoSutiPython $home $Python $spec -Offline:$Offline
& $selected.executable -B (Join-Path $root 'plugins/dosuti-mentor/scripts/bootstrap.py') --home $home
if ($LASTEXITCODE -ne 0) { throw "DoSuti setup failed with exit code $LASTEXITCODE." }
