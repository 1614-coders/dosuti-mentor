[CmdletBinding()]
param(
    [string]$HomePath = (Join-Path $env:LOCALAPPDATA 'DoSutiMentor'),
    [string]$Python = '',
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $root 'Windows-Runtime.ps1')
if ($env:PROCESSOR_ARCHITECTURE -notmatch 'AMD64|x86_64') { throw 'Windows setup currently supports x64 only.' }

$dataHome = Initialize-DoSutiHome $HomePath
$spec = Get-Content -LiteralPath (Join-Path $root '../references/windows-python.json') -Raw | ConvertFrom-Json
$selected = Resolve-DoSutiPython $dataHome $Python $spec -Offline:$Offline
& $selected.executable -B (Join-Path $root 'bootstrap.py') --home $dataHome
if ($LASTEXITCODE -ne 0) { throw "DoSuti setup failed with exit code $LASTEXITCODE." }
