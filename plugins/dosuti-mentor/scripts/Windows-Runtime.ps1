# Functions only: no downloads or PATH/registry changes when dot-sourced.
function Assert-DoSutiPath([string]$Path) {
    $item = [IO.Path]::GetFullPath($Path)
    while ($item) {
        if (Test-Path -LiteralPath $item) {
            if ((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Links/junctions are not supported for managed storage: $item"
            }
        }
        $item = Split-Path -Parent $item
    }
}

function Initialize-DoSutiHome([string]$HomePath) {
    Assert-DoSutiPath $HomePath
    if (Test-Path -LiteralPath (Join-Path $HomePath '.cleanup-pending.json')) { throw 'A legacy cleanup marker was found. Preserve this folder and choose a new empty DoSuti data folder.' }
    $full = [IO.Path]::GetFullPath($HomePath).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ($full -eq [IO.Path]::GetPathRoot($full).TrimEnd([IO.Path]::DirectorySeparatorChar) -or $full -eq $env:USERPROFILE) {
        throw 'Choose a dedicated DoSuti data folder.'
    }
    $marker = Join-Path $full '.dosuti-home.json'
    Assert-DoSutiPath $marker
    if (Test-Path -LiteralPath $marker) {
        $data = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
        if ($data.schema -ne 'dosuti_home_v1' -or $data.id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid home marker.' }
    } else {
        if ((Test-Path -LiteralPath $full) -and @(Get-ChildItem -LiteralPath $full -Force).Count) {
            throw 'Existing folder is not managed; choose an empty folder.'
        }
        [IO.Directory]::CreateDirectory($full) | Out-Null
        @{schema='dosuti_home_v1';id=[guid]::NewGuid().ToString('N')} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
    }
    foreach ($child in @('runtime','cache','temp')) {
        $path = Join-Path $full $child
        Assert-DoSutiPath $path
        [IO.Directory]::CreateDirectory($path) | Out-Null
    }
    return $full
}

function ConvertTo-DoSutiArgument([string]$Value) {
    # Windows CommandLineToArgvW quoting, including a trailing backslash.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    return '"' + [regex]::Replace($escaped, '(\\+)$', '$1$1') + '"'
}

function Wait-DoSutiProbe($Process, [int]$Milliseconds) { return $Process.WaitForExit($Milliseconds) }

function Stop-DoSutiProbe($Process) {
    try { $processId = $Process.Id }
    catch { return } # Start failed: no process exists.
    if ($Process.HasExited) { return }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path ([Environment]::SystemDirectory) 'taskkill.exe'
    $info.Arguments = '/PID ' + $processId + ' /T /F'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $killer = New-Object Diagnostics.Process
    $killer.StartInfo = $info
    try {
        $killer.Start() | Out-Null
        $null = $killer.StandardOutput.ReadToEndAsync()
        $null = $killer.StandardError.ReadToEndAsync()
        if (-not $killer.WaitForExit(10000)) {
            $killer.Kill(); $killer.WaitForExit()
            throw 'Python probe tree termination timed out; preserve its managed temporary folder.'
        }
        if ($killer.ExitCode -ne 0 -or -not $Process.WaitForExit(5000)) {
            throw 'Cannot confirm Python probe tree termination; preserve its managed temporary folder.'
        }
    } finally {
        try {
            try { $killerId = $killer.Id } catch { $killerId = $null }
            if ($null -ne $killerId -and -not $killer.HasExited) { $killer.Kill(); $null = $killer.WaitForExit(5000) }
        } finally { $killer.Dispose() }
    }
}

function Remove-DoSutiProbeFolder([string]$Path) {
    Assert-DoSutiPath $Path
    # Inspect each child before descending: PS 5.1 recursion can follow junctions.
    foreach ($item in @(Get-ChildItem -LiteralPath $Path -Force)) {
        Assert-DoSutiPath $item.FullName
        if ($item.PSIsContainer) { Remove-DoSutiProbeFolder $item.FullName }
        else { Remove-Item -LiteralPath $item.FullName -Force }
    }
    Remove-Item -LiteralPath $Path -Force
}

function Test-DoSutiPython([string]$Executable, [string[]]$LauncherArgs = @(),
                         [string]$HomePath, [ValidateRange(1,60000)][int]$TimeoutMilliseconds = 60000) {
    if ($Executable -match '[\\/]WindowsApps[\\/]python') { return $null }
    if (-not $HomePath) { throw 'Python probing requires a managed DoSuti home.' }
    $dataHome = Initialize-DoSutiHome $HomePath
    $temporary = Join-Path $dataHome ('temp/python-probe-' + [guid]::NewGuid().ToString('N'))
    Assert-DoSutiPath $temporary
    if (Test-Path -LiteralPath $temporary) { throw 'Probe folder already exists; preserve it.' }
    [IO.Directory]::CreateDirectory($temporary) | Out-Null
    $process = $null
    $started = $false
    $code = @'
import sys, struct, platform, json, ssl, venv, ensurepip, subprocess
assert sys.platform == 'win32' and sys.implementation.name == 'cpython'
assert sys.version_info[:2] in ((3, 11), (3, 12)) and struct.calcsize('P') == 8
assert platform.machine().lower() in ('amd64', 'x86_64')
assert 'windowsapps' not in (sys.executable + sys.base_prefix).lower()
folder = sys.argv[1] + '/venv'
venv.EnvBuilder(with_pip=True).create(folder)
subprocess.run([folder + '/Scripts/python.exe', '-I', '-B', '-m', 'pip', '--version'], check=True, capture_output=True, timeout=45)
print(json.dumps(dict(executable=sys.executable, version=platform.python_version(), architecture=platform.machine())))
'@
    try {
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $Executable
        $info.Arguments = ((@($LauncherArgs) + @('-I','-B','-c',$code,$temporary)) | ForEach-Object { ConvertTo-DoSutiArgument $_ }) -join ' '
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.WorkingDirectory = $temporary
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        foreach ($key in @('TEMP','TMP','TMPDIR','PIP_CACHE_DIR')) { $info.EnvironmentVariables[$key] = $temporary }
        $info.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'
        $info.EnvironmentVariables['PIP_DISABLE_PIP_VERSION_CHECK'] = '1'
        foreach ($key in @('PYLAUNCHER_ALLOW_INSTALL','PYLAUNCHER_ALWAYS_INSTALL','PYLAUNCHER_DRYRUN')) {
            $info.EnvironmentVariables.Remove($key)
        }
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $info
        $started = $process.Start()
        if (-not $started) { return $null }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not (Wait-DoSutiProbe $process $TimeoutMilliseconds)) { return $null }
        if ($process.ExitCode -ne 0) { return $null }
        if (-not $stdout.Wait(5000) -or -not $stderr.Wait(5000)) { return $null }
        return ($stdout.Result | ConvertFrom-Json)
    } catch [OperationCanceledException] { throw }
      catch [System.Management.Automation.PipelineStoppedException] { throw }
      catch { return $null }
    finally {
        $stopped = $false
        try {
            if ($started) {
                try { Stop-DoSutiProbe $process }
                catch { throw "Python probe cleanup failed; preserve $temporary. $_" }
            }
            $stopped = $true
        } finally {
            if ($null -ne $process) { $process.Dispose() }
            if ($stopped) { Remove-DoSutiProbeFolder $temporary }
        }
    }
}

function Get-DoSutiTar {
    # Do not select Git/MSYS tar or a PATH override on Windows.
    return Join-Path ([Environment]::SystemDirectory) 'tar.exe'
}

function Install-DoSutiPython([string]$HomePath, $Spec, [string]$TargetName = 'python') {
    if ($TargetName -notmatch '^python(?:-candidate-[a-f0-9]{32})?$') { throw 'Invalid private Python target.' }
    $runtime = Join-Path $HomePath 'runtime'
    $target = Join-Path $runtime $TargetName
    $python = Join-Path $target 'python.exe'
    if (Test-Path -LiteralPath $target) { throw 'Private Python is damaged or unverified; preserve runtime/python before repair.' }
    $cache = Join-Path $HomePath 'cache/python'
    Assert-DoSutiPath $cache
    [IO.Directory]::CreateDirectory($cache) | Out-Null
    $archive = Join-Path $cache ($Spec.sha256 + '.tar.gz')
    Assert-DoSutiPath $archive
    if ((Test-Path -LiteralPath $archive) -and (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $Spec.sha256) {
        Remove-Item -LiteralPath $archive
    }
    if (-not (Test-Path -LiteralPath $archive)) {
        $part = $archive + '.part'
        Assert-DoSutiPath $part
        $previousTLS = [Net.ServicePointManager]::SecurityProtocol
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $Spec.url -OutFile $part -UseBasicParsing -TimeoutSec 300
            if ((Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash -ne $Spec.sha256) { throw 'Python archive SHA256 mismatch.' }
            Move-Item -LiteralPath $part -Destination $archive
        } finally {
            [Net.ServicePointManager]::SecurityProtocol = $previousTLS
            if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part }
        }
    }
    $staging = Join-Path $runtime ('.python-staging-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($staging) | Out-Null
    try {
        # Windows bsdtar can lose non-ANSI characters in argv, even with PS 7.
        # Unicode stays in CreateProcess's working directory; argv is relative ASCII.
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = Get-DoSutiTar
        $info.UseShellExecute = $false
        $info.WorkingDirectory = $HomePath
        $info.Arguments = '-xzf ' + (ConvertTo-DoSutiArgument ('cache/python/' + [IO.Path]::GetFileName($archive))) +
            ' -C ' + (ConvertTo-DoSutiArgument ('runtime/' + [IO.Path]::GetFileName($staging)))
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $info
        try {
            $process.Start() | Out-Null
            $process.WaitForExit()
            if ($process.ExitCode -ne 0) { throw 'Cannot extract Python; Windows tar.exe failed.' }
        } finally { $process.Dispose() }
        $candidate = Join-Path $staging 'python/python.exe'
        if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -ne $Spec.python_exe_sha256) { throw 'Python executable SHA256 mismatch.' }
        Move-Item -LiteralPath (Join-Path $staging 'python') -Destination $target
        $checked = Test-DoSutiPython $python -HomePath $HomePath
        if (-not $checked) { throw 'Private Python cannot create a usable environment.' }
        return $checked
    } catch {
        # This target was created by this attempt; an existing target was refused above.
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
        throw
    } finally { if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force } }
}

function Resolve-DoSutiPython([string]$HomePath, [string]$Explicit, $Spec, [switch]$Offline) {
    $private = Join-Path $HomePath 'runtime/python/python.exe'
    $candidates = @()
    if ($Explicit) {
        $command = Get-Command $Explicit -CommandType Application -ErrorAction SilentlyContinue
        if ($command) { $candidates += @{exe=$command.Source;args=@()} }
    }
    if (Test-Path -LiteralPath $private) {
        Assert-DoSutiPath $private
        if ((Get-FileHash -LiteralPath $private -Algorithm SHA256).Hash -eq $Spec.python_exe_sha256) {
            $candidates += @{exe=$private;args=@()}
        }
    }
    $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
    if ($launcher) {
        $candidates += @{exe=$launcher.Source;args=@('-3.12')}, @{exe=$launcher.Source;args=@('-3.11')}
    }
    foreach ($name in @('python3.12','python','python3')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($command) { $candidates += @{exe=$command.Source;args=@()} }
    }
    foreach ($candidate in $candidates) {
        $selected = Test-DoSutiPython $candidate.exe $candidate.args -HomePath $HomePath
        if ($selected) {
            $selected | Add-Member -NotePropertyName owned -NotePropertyValue ($selected.executable -eq $private)
            return $selected
        }
    }
    $privateFolder = Join-Path $HomePath 'runtime/python'
    if (Test-Path -LiteralPath $privateFolder) {
        $selected = Repair-DoSutiPython $HomePath $Spec -Offline:$Offline
        $selected | Add-Member -NotePropertyName owned -NotePropertyValue $true
        return $selected
    }
    if ($Offline) { throw 'No suitable CPython 3.11/3.12 x64 found; automatic download is disabled in offline mode.' }
    Write-Host 'No suitable Python found. Downloading a private CPython into runtime/python.'
    $selected = Install-DoSutiPython $HomePath $Spec
    $selected | Add-Member -NotePropertyName owned -NotePropertyValue $true
    return $selected
}

function Lock-DoSutiHeavyFile($Stream) { $Stream.Lock(0,1) }
function Unlock-DoSutiHeavyFile($Stream) { $Stream.Unlock(0,1) }

function Repair-DoSutiPython([string]$HomePath, $Spec, [switch]$Offline) {
    $marker=Join-Path $HomePath '.dosuti-home.json'
    Assert-DoSutiPath $marker
    $identity=Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($identity.schema -ne 'dosuti_home_v1' -or $identity.id -notmatch '^[a-f0-9]{32}$') { throw 'Python repair requires a managed home.' }
    $target = Join-Path $HomePath 'runtime/python'
    Assert-DoSutiPath $target
    if ($Offline) {
        $archive = Join-Path $HomePath ('cache/python/' + $Spec.sha256 + '.tar.gz')
        Assert-DoSutiPath $archive
        if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $Spec.sha256) {
            throw 'Offline Python repair needs the verified cached archive; original Python preserved.'
        }
    }
    $lockPath = Join-Path $HomePath '.heavy.lock'
    Assert-DoSutiPath $lockPath
    $lock = [IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)
    $locked = $false
    $backup = Join-Path $HomePath ('runtime/python-previous-' + [guid]::NewGuid().ToString('N'))
    $candidateName = 'python-candidate-' + [guid]::NewGuid().ToString('N')
    $candidate = Join-Path $HomePath ('runtime/' + $candidateName)
    $prepared = $false
    $moved = $false
    try {
        if ($lock.Length -eq 0) { $lock.SetLength(1) }
        try { Lock-DoSutiHeavyFile $lock; $locked=$true }
        catch { throw 'Another DoSuti worker is active; retry Python repair after processing finishes.' }
        # Download, hash and probe the replacement while the previous tree stays put.
        $selected = Install-DoSutiPython $HomePath $Spec -TargetName $candidateName
        $prepared = $true
        # Preserve the old tree, including unknown files, rather than deleting it.
        Move-Item -LiteralPath $target -Destination $backup
        $moved = $true
        try {
            Move-Item -LiteralPath $candidate -Destination $target
            $python = Join-Path $target 'python.exe'
            if ((Get-FileHash -LiteralPath $python -Algorithm SHA256).Hash -ne $Spec.python_exe_sha256) { throw 'Python executable SHA256 mismatch.' }
            $selected = Test-DoSutiPython $python -HomePath $HomePath
            if (-not $selected) { throw 'Published Python failed verification.' }
        }
        catch {
            if (Test-Path -LiteralPath $target) {
                if (Test-Path -LiteralPath $candidate) { throw "Repair interrupted; preserve $target and $backup for recovery. $_" }
                Move-Item -LiteralPath $target -Destination $candidate
            }
            Move-Item -LiteralPath $backup -Destination $target
            $moved = $false
            throw
        }
        $selected | Add-Member -NotePropertyName recovery_backup -NotePropertyValue $backup
        return $selected
    } finally {
        try {
            if ($prepared -and (Test-Path -LiteralPath $candidate)) { Remove-Item -LiteralPath $candidate -Recurse -Force }
        } finally {
            try { if ($locked) { Unlock-DoSutiHeavyFile $lock } }
            finally { $lock.Dispose() }
        }
        if ($moved) { Write-Host "Previous Python preserved at $backup" }
    }
}
