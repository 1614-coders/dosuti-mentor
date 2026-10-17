# One Windows entry point; does not install system Python or change execution policy.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Video,
    [string]$PilotRoot = (Join-Path $env:LOCALAPPDATA 'DoSutiMentorPilot'),
    [string]$PythonExecutable,
    [ValidateSet('tiny','base','small','medium','large-v3')][string]$Model = 'small',
    [ValidateRange(1,60)][int]$Seconds = 30,
    [ValidateRange(0,2147483647)][int]$Start = 0
)
$ErrorActionPreference = 'Stop'
$PilotRoot = [System.IO.Path]::GetFullPath($PilotRoot)
$PilotParent = Split-Path -Parent $PilotRoot
# Bootstrap report is a sibling: a failed Python check must not make root look
# like an unrelated existing directory to the resumable Python setup.
[System.IO.Directory]::CreateDirectory($PilotParent) | Out-Null
$PilotStamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$PilotBootstrapReport = Join-Path $PilotParent ("DoSuti-bootstrap-" + $PilotStamp + '.md')
$PilotBootstrapLines = @('# DoSuti Mentor: bootstrap report', '', 'TEST ONLY; review local paths before sharing.', '')
try {
    $PilotLauncherArgs = @()
    if ($PythonExecutable) {
        $PilotPythonExe = (Get-Command $PythonExecutable -ErrorAction Stop).Source
    } else {
        $PilotPythonExe = (Get-Command py -ErrorAction Stop).Source
        $PilotLauncherArgs = @('-3.12')
    }
    $PilotVersion = & $PilotPythonExe @PilotLauncherArgs -X utf8 -c 'import sys; print(sys.version); sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Python unavailable/unsupported. Install Python 3.12 with launcher, or pass -PythonExecutable.' }
    $PilotBootstrapLines += ('Python: ' + ($PilotVersion -join ' '))
    $PilotSetupScript = Join-Path $PSScriptRoot 'plugins\dosuti-mentor\scripts\pilot_setup.py'
    if (-not (Test-Path -LiteralPath $PilotSetupScript -PathType Leaf)) { throw 'Keep this script beside README.md in the unpacked DoSuti Mentor package.' }
    $PilotBootstrapLines += 'Python check passed; starting runtime/model/CPU preparation.'
    $PilotBootstrapLines | Set-Content -LiteralPath $PilotBootstrapReport -Encoding UTF8
    & $PilotPythonExe @PilotLauncherArgs -X utf8 -B $PilotSetupScript --root $PilotRoot --video $Video --model $Model --seconds $Seconds --start $Start
    if ($LASTEXITCODE -ne 0) { throw 'Preparation failed. See SETUP_REPORT.md inside PilotRoot and the per-step log.' }
    $PilotBootstrapLines += 'Preparation passed. Open PilotRoot/project in Codex and invoke $test-learning.'
    $PilotBootstrapLines | Set-Content -LiteralPath $PilotBootstrapReport -Encoding UTF8
    Write-Host "Bootstrap report: $PilotBootstrapReport"
} catch {
    $PilotBootstrapLines += ('Failed: ' + $_.Exception.Message)
    $PilotBootstrapLines | Set-Content -LiteralPath $PilotBootstrapReport -Encoding UTF8
    Write-Error "Preparation stopped. Report: $PilotBootstrapReport. $($_.Exception.Message)" -ErrorAction Continue
    exit 1
}
