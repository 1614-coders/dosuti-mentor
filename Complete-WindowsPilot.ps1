# Export/review a cleanup plan, then apply it from outside the tree being deleted.
[CmdletBinding()]
param(
    [string]$PilotRoot = (Join-Path $env:LOCALAPPDATA 'DoSutiMentorPilot'),
    [string]$OutputDirectory = (Join-Path $env:USERPROFILE 'DoSutiReports'),
    [string]$PlanFile,
    [switch]$Apply,
    [switch]$AllowDownload
)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Windows-Runtime.ps1')
$root=[IO.Path]::GetFullPath($PilotRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$out=[IO.Path]::GetFullPath($OutputDirectory)
Assert-DoSutiPath $root
Assert-DoSutiPath $out
$prefix=$root+[IO.Path]::DirectorySeparatorChar
if ($out -eq $root -or $out.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or
    $PSScriptRoot -eq $root -or $PSScriptRoot.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {
    throw 'Keep the unpacked plugin and reports outside PilotRoot.'
}
$pilot=Join-Path $root 'run'
$early=-not (Test-Path -LiteralPath $pilot)
if ($Apply) {
    if (-not $PlanFile) { throw 'Apply requires the external PlanFile created by the first run.' }
    $PlanFile=[IO.Path]::GetFullPath($PlanFile)
    Assert-DoSutiPath $PlanFile
    if ($PlanFile.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Plan must be outside PilotRoot.' }
    if ((Get-Item -LiteralPath $PlanFile -Force).Length -gt 12000000) { throw 'Cleanup plan is oversized.' }
    $plan=Get-Content -LiteralPath $PlanFile -Raw | ConvertFrom-Json
    if ($plan.schema -eq 'setup_cleanup_v1') { $early=$true; $expected=$root }
    elseif ($plan.schema -eq 'pilot_cleanup_v2') { $early=$false; $expected=$pilot }
    else { throw 'Unknown cleanup plan schema.' }
    if ($plan.root -ne $root -or $plan.pilot -ne $expected) { throw 'Plan belongs to a different pilot.' }
} else {
    $marker=Join-Path $root '.dosuti-home.json';Assert-DoSutiPath $marker
    if ((Get-Item -LiteralPath $marker -Force).Length -gt 4096) { throw 'Home marker is oversized.' }
    $identity=Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($identity.schema -ne 'dosuti_home_v1' -or $identity.id -notmatch '^[a-f0-9]{32}$') { throw 'Managed home marker missing or invalid.' }
}
[IO.Directory]::CreateDirectory($out) | Out-Null
$scratch=Join-Path $out ('.dosuti-cleanup-helper-'+[guid]::NewGuid().ToString('N'))
$oldEnv=@{};$oldLocation=Get-Location
try {
    Initialize-DoSutiHome $scratch | Out-Null
    $temp=Join-Path $scratch 'temp'
    foreach($key in @('TEMP','TMP','TMPDIR')) {
        $oldEnv[$key]=[Environment]::GetEnvironmentVariable($key,'Process')
        [Environment]::SetEnvironmentVariable($key,$temp,'Process')
    }
    $spec=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'plugins/dosuti-mentor/references/windows-python.json') -Raw | ConvertFrom-Json
    $selected=$null
    try { $selected=Resolve-DoSutiPython $scratch '' $spec -Offline }
    catch { Write-Host 'No external Python found; preparing a temporary cleanup helper.' }
    if ($selected -and ($selected.executable -eq $root -or $selected.executable.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase))) {
        $selected=$null
    }
    if (-not $selected) {
        $source=Join-Path $root ('cache/python/'+$spec.sha256+'.tar.gz')
        if (Test-Path -LiteralPath $source) {
            Assert-DoSutiPath $source
            if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -eq $spec.sha256) {
                $cache=Join-Path $scratch 'cache/python';[IO.Directory]::CreateDirectory($cache) | Out-Null
                Copy-Item -LiteralPath $source -Destination (Join-Path $cache ($spec.sha256+'.tar.gz'))
            }
        }
        $cached=Join-Path $scratch ('cache/python/'+$spec.sha256+'.tar.gz')
        if (-not (Test-Path -LiteralPath $cached) -and -not $AllowDownload) {
            throw 'No external Python or verified cached archive. Original data preserved. Retry with -AllowDownload to obtain the temporary helper.'
        }
        $selected=Install-DoSutiPython $scratch $spec
    }
    Set-Location -LiteralPath $scratch
    if (-not $Apply) { $PlanFile=Join-Path $out ('cleanup-'+[guid]::NewGuid().ToString('N')+'.json') }
    if ($early) {
        $scriptPath=Join-Path $PSScriptRoot 'plugins/dosuti-mentor/scripts/setup_cleanup.py'
        if ($Apply) { & $selected.executable -E -s -B $scriptPath apply --root $root --plan-file $PlanFile }
        else { & $selected.executable -E -s -B $scriptPath plan --root $root --output $PlanFile }
    } else {
        $scriptPath=Join-Path $PSScriptRoot 'plugins/dosuti-mentor/scripts/pilot.py'
        if ($Apply) { & $selected.executable -E -s -B $scriptPath cleanup-apply --pilot $pilot --plan-file $PlanFile }
        else { & $selected.executable -E -s -B $scriptPath cleanup-plan --pilot $pilot --output $PlanFile }
    }
    if ($LASTEXITCODE -ne 0) { throw 'Cleanup stopped; preserve the plan/report and inspect the Python error above.' }
    Write-Host "External cleanup plan: $PlanFile"
    if (-not $Apply) { Write-Host 'Review the exported report and plan, then rerun with -Apply -PlanFile pointing to this file. Data has not been deleted.' }
    else { Write-Host 'Test data removed. Remove the plugin, test source, downloaded package and test chats separately in Codex/Explorer.' }
} finally {
    if (Test-Path -LiteralPath $oldLocation.Path) { Set-Location -LiteralPath $oldLocation.Path }
    else { Set-Location -LiteralPath $out }
    foreach($key in $oldEnv.Keys) { [Environment]::SetEnvironmentVariable($key,$oldEnv[$key],'Process') }
    if (Test-Path -LiteralPath $scratch) {
        try { Remove-Item -LiteralPath $scratch -Recurse -Force }
        catch { Write-Warning "Temporary cleanup helper remains at $scratch; close its processes and remove this folder. $_" }
    }
}
