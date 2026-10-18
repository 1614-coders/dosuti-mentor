# Windows x64: reuse a working Python, otherwise unpack a private pinned distribution.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Video,
    [string]$PilotRoot = (Join-Path $env:LOCALAPPDATA 'DoSutiMentorPilot'),
    [string]$PythonExecutable,
    [ValidateSet('tiny','base','small','medium','large-v3')][string]$Model = 'small',
    [ValidateRange(1,60)][int]$Seconds = 30,
    [ValidateRange(0,2147483647)][int]$Start = 0,
    [ValidateSet('standard','economy')][string]$CpuMode = 'standard',
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Windows-Runtime.ps1')
$PilotRoot = [IO.Path]::GetFullPath($PilotRoot)
$PilotParent = Split-Path -Parent $PilotRoot
$PilotStamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
# Only an invalid/unwritable home needs an external early-failure report.
$PilotBootstrapReport = Join-Path $PilotParent ("DoSuti-bootstrap-" + $PilotStamp + '.md')
$PilotBootstrapLines = @('# DoSuti Mentor: bootstrap report', '', 'TEST ONLY; review local paths before sharing.', '')
$PilotEnvironment = @{}
$PilotBootstrapLock = $null
$PilotTemp = $null
$PilotSucceeded = $false
try {
    if ($env:OS -ne 'Windows_NT' -or @($env:PROCESSOR_ARCHITECTURE,$env:PROCESSOR_ARCHITEW6432) -notcontains 'AMD64') {
        throw 'Automatic setup currently supports Windows x64 only.'
    }
    $prefix = $PilotRoot.TrimEnd('\') + '\'
    if ($PSScriptRoot.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or $PilotRoot -eq $PSScriptRoot) {
        throw 'Keep the unpacked plugin outside the selected data folder.'
    }
    $PilotRoot = Initialize-DoSutiHome $PilotRoot
    $PilotBootstrapReport = Join-Path $PilotRoot ('reports/bootstrap-' + $PilotStamp + '.md')
    $lockPath = Join-Path $PilotRoot '.bootstrap.lock'
    Assert-DoSutiPath $lockPath
    $PilotBootstrapLock = [IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    if (Test-Path -LiteralPath (Join-Path $PilotRoot '.cleanup-pending.json')) { throw 'Cleanup is pending; resume it before setup.' }
    $PilotTemp = Join-Path $PilotRoot ('temp/job-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($PilotTemp) | Out-Null
    [IO.File]::WriteAllText((Join-Path $PilotTemp '.lease.lock'),'')
    $settings = @{
        DOSUTI_HOME=$PilotRoot; TEMP=$PilotTemp; TMP=$PilotTemp; TMPDIR=$PilotTemp;
        PIP_CACHE_DIR=(Join-Path $PilotRoot 'cache/pip'); HF_HOME=(Join-Path $PilotRoot 'cache/huggingface');
        XDG_CACHE_HOME=(Join-Path $PilotRoot 'cache'); IMAGEIO_USERDIR=(Join-Path $PilotRoot 'cache/imageio');
        PYTHONDONTWRITEBYTECODE='1'; PIP_NO_INPUT='1'; PIP_DISABLE_PIP_VERSION_CHECK='1'
    }
    foreach ($key in $settings.Keys) {
        $PilotEnvironment[$key] = [Environment]::GetEnvironmentVariable($key,'Process')
        [Environment]::SetEnvironmentVariable($key,$settings[$key],'Process')
    }
    $PilotSpecPath = Join-Path $PSScriptRoot 'plugins/dosuti-mentor/references/windows-python.json'
    $PilotSpec = Get-Content -LiteralPath $PilotSpecPath -Raw | ConvertFrom-Json
    $PilotPython = Resolve-DoSutiPython $PilotRoot $PythonExecutable $PilotSpec -Offline:$Offline
    $PilotProviderRecord = Join-Path $PilotRoot 'runtime/python-provider.json'
    Assert-DoSutiPath $PilotProviderRecord
    $PilotPython | ConvertTo-Json | Set-Content -LiteralPath $PilotProviderRecord -Encoding UTF8
    $PilotBootstrapLines += ('Python: ' + ($PilotPython | ConvertTo-Json -Compress))
    $PilotBootstrapLines += 'Python probe passed (version, x64, SSL, venv and pip). Starting preparation.'
    $PilotBootstrapLines | Set-Content -LiteralPath $PilotBootstrapReport -Encoding UTF8
    $PilotSetupScript = Join-Path $PSScriptRoot 'plugins/dosuti-mentor/scripts/pilot_setup.py'
    $PilotArgs = @('-E','-s','-B',$PilotSetupScript,'--root',$PilotRoot,'--video',$Video,'--model',$Model,
                  '--seconds',$Seconds,'--start',$Start,'--cpu-mode',$CpuMode)
    if ($Offline) { $PilotArgs += '--offline' }
    & $PilotPython.executable @PilotArgs
    if ($LASTEXITCODE -ne 0) { throw 'Preparation failed. See SETUP_REPORT.md and the per-step logs inside PilotRoot.' }
    $PilotBootstrapLines += 'Preparation passed. Open PilotRoot/project in Codex and invoke $test-learning.'
    $PilotBootstrapLines | Set-Content -LiteralPath $PilotBootstrapReport -Encoding UTF8
    Write-Host "Bootstrap report: $PilotBootstrapReport"
    $PilotSucceeded = $true
} catch {
    if (Test-Path -LiteralPath (Join-Path $PilotRoot '.cleanup-pending.json')) {
        $PilotBootstrapReport = Join-Path $PilotParent ("DoSuti-bootstrap-" + $PilotStamp + '.md')
    }
    $PilotBootstrapLines += ('Failed: ' + $_.Exception.Message)
    [IO.Directory]::CreateDirectory((Split-Path -Parent $PilotBootstrapReport)) | Out-Null
    $PilotBootstrapLines | Set-Content -LiteralPath $PilotBootstrapReport -Encoding UTF8
    Write-Error "Preparation stopped. Report: $PilotBootstrapReport. $($_.Exception.Message)" -ErrorAction Continue
    exit 1
} finally {
    foreach ($key in $PilotEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key,$PilotEnvironment[$key],'Process') }
    if ($PilotTemp -and (Test-Path -LiteralPath $PilotTemp)) { Remove-Item -LiteralPath $PilotTemp -Recurse -Force }
    if ($PilotBootstrapLock) { $PilotBootstrapLock.Dispose() }
}
if ($PilotSucceeded) {
    $PilotStorageScript = Join-Path $PSScriptRoot 'plugins/dosuti-mentor/scripts/managed_storage.py'
    # Budget only disposable caches, after all bootstrap/processing leases have closed.
    try {
        $PilotCleanupOutput = & $PilotPython.executable -E -s -B $PilotStorageScript --home $PilotRoot --apply --cache-mib 1024 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Cache maintenance is busy or needs review.' }
        $PilotCleanupReport = Join-Path $PilotRoot 'reports/cache-maintenance.json'
        Assert-DoSutiPath $PilotCleanupReport
        $PilotCleanupOutput | Set-Content -LiteralPath $PilotCleanupReport -Encoding UTF8
    } catch { Write-Warning 'Cache cleanup deferred; a worker may be active. Learning data was preserved.' }
}
