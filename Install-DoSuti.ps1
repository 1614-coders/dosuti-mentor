[CmdletBinding()]
param(
    [string]$HomePath = (Join-Path $env:LOCALAPPDATA 'DoSutiMentor'),
    [string]$Python = '',
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'plugins/dosuti-mentor/scripts/Install-DoSuti.ps1') @PSBoundParameters
