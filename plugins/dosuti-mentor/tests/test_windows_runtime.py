"""PowerShell behavior tests; Windows binaries are never executed on the Mac host."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

PWSH = os.environ.get('DOSUTI_TEST_PWSH') or shutil.which('pwsh') or shutil.which('powershell')
PACKAGE = Path(__file__).resolve().parents[3]


@unittest.skipUnless(PWSH, 'PowerShell is optional for Python-only test runs')
class WindowsRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'данные с пробелами'

    def run_ps(self, body):
        def literal(value): return "'" + str(value).replace("'", "''") + "'"
        code = ("$ErrorActionPreference='Stop'; . " + literal(PACKAGE / 'Windows-Runtime.ps1') +
                '; $homePath=' + literal(self.home) + '; ' + body + '; exit 0')
        if os.name != 'nt':
            # Only the executable lookup is replaced; run the real extraction.
            code = code.replace('; $homePath=',
                '; function Get-DoSutiTar { (Get-Command tar -CommandType Application | Select-Object -First 1).Source }; $homePath=', 1)
        encoded = base64.b64encode(code.encode('utf-16-le')).decode()
        result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def probe_command(self, code, milliseconds=5000):
        literal=lambda s:"'"+str(s).replace("'","''")+"'"
        return (f'Test-DoSutiPython {literal(sys.executable)} @({literal("-c")},{literal(code)}) '
                f'-HomePath $homePath -TimeoutMilliseconds {milliseconds}')

    def test_probe_environment_cleanup_errors_and_repeat(self):
        success=self.probe_command('import os,json;from pathlib import Path;print(json.dumps(dict(temporary=os.environ["TEMP"],tmp=os.environ["TMP"],tmpdir=os.environ["TMPDIR"],cwd=str(Path.cwd()))))')
        failure=self.probe_command('from pathlib import Path;Path("partial").write_text("fixture");raise RuntimeError("probe failed")')
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$temporary=Join-Path $homePath 'temp';$keep=Join-Path $temporary 'user-file';[IO.File]::WriteAllText($keep,'keep')
foreach($attempt in 1..2) {
 $result=SUCCESS
 if((Split-Path -Parent $result.temporary) -ne $temporary -or $result.temporary -ne $result.tmp -or $result.tmp -ne $result.tmpdir -or $result.cwd -ne $result.temporary) {throw 'probe escaped managed temp'}
 if(Test-Path -LiteralPath $result.temporary) {throw 'successful probe left temporary files'}
 if(FAILURE) {throw 'failed probe accepted'}
 if(Test-DoSutiPython (Join-Path $homePath 'missing-python.exe') -HomePath $homePath) {throw 'missing Python accepted'}
 if(@(Get-ChildItem -LiteralPath $temporary -Force).Count -ne 1) {throw 'probe left temporary files'}
 if((Get-Content -LiteralPath $keep -Raw) -ne 'keep') {throw 'foreign file deleted'}
}
""".replace('SUCCESS',success).replace('FAILURE',failure))

    def test_probe_start_failure_never_attempts_process_termination(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
function Stop-DoSutiProbe {throw 'An unstarted process must not be terminated'}
if(Test-DoSutiPython (Join-Path $homePath 'missing-python.exe') -HomePath $homePath) {throw 'Missing executable accepted'}
if(@(Get-ChildItem -LiteralPath (Join-Path $homePath 'temp') -Force).Count) {throw 'Start failure left temporary files'}
""")

    def test_probe_cancellation_stops_process_before_cleanup(self):
        stop=''
        if os.name != 'nt':
            stop="function Stop-DoSutiProbe {param($Process) if(-not $Process.HasExited) {$Process.Kill();$Process.WaitForExit()} }"
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
STOP
function Wait-DoSutiProbe {param($Process,$Milliseconds) $script:probeId=$Process.Id;throw [OperationCanceledException]::new('fixture cancelled')}
try { PROBE;throw 'unexpected success' }
catch [OperationCanceledException] {if($_.Exception.Message -notmatch 'fixture cancelled') {throw}}
if(Get-Process -Id $script:probeId -ErrorAction SilentlyContinue) {throw 'cancelled probe process remains'}
if(@(Get-ChildItem -LiteralPath (Join-Path $homePath 'temp') -Force).Count) {throw 'cancelled probe left files'}
""".replace('STOP',stop).replace('PROBE',self.probe_command('import time;time.sleep(60)')))

    def test_probe_cleanup_refuses_link_to_foreign_files(self):
        self.run_ps('Initialize-DoSutiHome $homePath | Out-Null')
        outside=self.base/'outside';outside.mkdir();(outside/'keep').write_text('user data')
        probe=self.home/'temp'/'python-probe-fixture';probe.mkdir()
        try:(probe/'link').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('Directory symlink unavailable for cleanup boundary test')
        self.run_ps("try {Remove-DoSutiProbeFolder (Join-Path $homePath 'temp/python-probe-fixture');throw 'unexpected deletion'} "
                    "catch {if($_.Exception.Message -notmatch 'Links/junctions') {throw}}")
        self.assertEqual((outside/'keep').read_text(),'user data')
        self.assertTrue((probe/'link').is_symlink())

    @unittest.skipUnless(os.name=='nt','Native taskkill process-tree cleanup requires Windows')
    def test_probe_timeout_stops_child_tree_and_removes_only_owned_folder(self):
        pidfile=self.home/'child.pid'
        code=('import sys,subprocess,time;from pathlib import Path;'
              'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
              f'Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(60)')
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
if(PROBE) {throw 'timed-out probe accepted'}
$childId=[int](Get-Content -LiteralPath (Join-Path $homePath 'child.pid') -Raw)
for($i=0;$i -lt 50 -and (Get-Process -Id $childId -ErrorAction SilentlyContinue);$i++) {Start-Sleep -Milliseconds 100}
if(Get-Process -Id $childId -ErrorAction SilentlyContinue) {throw 'probe descendant remains'}
if(@(Get-ChildItem -LiteralPath (Join-Path $homePath 'temp') -Force).Count) {throw 'timed-out probe left files'}
""".replace('PROBE',self.probe_command(code,3000)))

    def test_install_script_parses(self):
        path = PACKAGE / 'Install-DoSuti.ps1'
        self.run_ps("$e=$null; [Management.Automation.Language.Parser]::ParseFile('" +
                    str(path).replace("'", "''") + "',[ref]$null,[ref]$e) | Out-Null; " +
                    "if($e.Count) {throw ($e | Out-String)}")

    def test_existing_python_avoids_download(self):
        output = self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
function Get-Command { param($Name,$CommandType,$ErrorAction) if($Name -eq 'python') { [pscustomobject]@{Source='existing-python'} } }
function Test-DoSutiPython { param($Executable,$LauncherArgs) [pscustomobject]@{executable=$Executable;version='3.12.15'} }
function Install-DoSutiPython { throw 'must not download' }
$r=Resolve-DoSutiPython $homePath '' ([pscustomobject]@{})
if($r.executable -ne 'existing-python' -or $r.owned) {throw 'wrong existing selection'}
""")

    def test_missing_python_uses_private_fallback(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
function Get-Command { param($Name,$CommandType,$ErrorAction) return $null }
function Install-DoSutiPython { param($HomePath,$Spec) [pscustomobject]@{executable=(Join-Path $HomePath 'runtime/python/python.exe');version='3.12.15'} }
$r=Resolve-DoSutiPython $homePath '' ([pscustomobject]@{})
if(-not $r.owned) {throw 'private interpreter must be owned'}
""")

    def test_unsuitable_candidates_are_rejected_before_fallback(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
function Get-Command { param($Name,$CommandType,$ErrorAction) if($Name -eq 'py') { [pscustomobject]@{Source='launcher'} } }
$script:probes=0
function Test-DoSutiPython { param($Executable,$LauncherArgs) $script:probes++; return $null }
function Install-DoSutiPython { param($HomePath,$Spec) [pscustomobject]@{executable='private'} }
$r=Resolve-DoSutiPython $homePath '' ([pscustomobject]@{})
if($script:probes -ne 2 -or -not $r.owned) {throw 'candidate probing failed'}
""")

    def test_offline_refuses_download(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
function Get-Command { param($Name,$CommandType,$ErrorAction) return $null }
function Install-DoSutiPython { throw 'download called' }
try { Resolve-DoSutiPython $homePath '' ([pscustomobject]@{}) -Offline; throw 'unexpected success' }
catch { if($_.Exception.Message -notmatch 'disabled in offline') {throw} }
""")

    def test_unowned_folder_is_not_adopted(self):
        self.home.mkdir(); (self.home / 'keep.txt').write_text('user')
        self.run_ps("try {Initialize-DoSutiHome $homePath; throw 'unexpected success'} "
                    "catch {if($_.Exception.Message -notmatch 'not managed') {throw}}")
        self.assertEqual((self.home / 'keep.txt').read_text(), 'user')
        self.assertFalse((self.home / '.dosuti-home.json').exists())

    def test_home_marker_is_readable_by_python(self):
        self.run_ps('Initialize-DoSutiHome $homePath | Out-Null')
        marker = json.loads((self.home / '.dosuti-home.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(marker['schema'], 'dosuti_home_v1')
        self.assertEqual(len(marker['id']), 32)
        self.assertEqual({path.name for path in self.home.iterdir()},
                         {'.dosuti-home.json', 'runtime', 'cache', 'temp'})

    def test_corrupt_archive_is_rejected_and_partial_removed(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
function Invoke-WebRequest { param($Uri,$OutFile,[switch]$UseBasicParsing,$TimeoutSec) [IO.File]::WriteAllText($OutFile,'corrupt') }
$spec=[pscustomobject]@{url='https://example.invalid/python';sha256=('0'*64)}
try { Install-DoSutiPython $homePath $spec; throw 'unexpected success' }
catch { if($_.Exception.Message -notmatch 'SHA256 mismatch') {throw} }
if(@(Get-ChildItem -LiteralPath (Join-Path $homePath 'cache/python') -Force).Count) {throw 'partial archive remains'}
if(Test-Path -LiteralPath (Join-Path $homePath 'runtime/python')) {throw 'bad interpreter installed'}
""")

    def test_cached_archive_extraction_and_private_reuse(self):
        archive = self.base / 'python.tar.gz'
        payload = b'fixture executable, not runnable'
        with tarfile.open(archive, 'w:gz') as tar:
            item = tarfile.TarInfo('python/python.exe'); item.size = len(payload)
            tar.addfile(item, io.BytesIO(payload))
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        exe_sha = hashlib.sha256(payload).hexdigest()
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$spec=[pscustomobject]@{sha256='ARCHIVE_SHA';python_exe_sha256='EXE_SHA'}
$cache=Join-Path $homePath 'cache/python'; [IO.Directory]::CreateDirectory($cache) | Out-Null
Copy-Item -LiteralPath 'ARCHIVE_PATH' -Destination (Join-Path $cache ($spec.sha256+'.tar.gz'))
function Invoke-WebRequest {throw 'cached archive must not download'}
function tar.exe { throw 'PATH tar must not be used' }
function Test-DoSutiPython {param($Executable,$LauncherArgs) [pscustomobject]@{executable=$Executable;version='3.12.15'} }
$r=Install-DoSutiPython $homePath $spec
if(-not (Test-Path -LiteralPath $r.executable)) {throw 'missing interpreter'}
$again=Resolve-DoSutiPython $homePath '' $spec
if(-not $again.owned -or $again.executable -ne $r.executable) {throw 'private reuse failed'}
""".replace('ARCHIVE_SHA', sha).replace('EXE_SHA', exe_sha).replace('ARCHIVE_PATH', str(archive)))

    def test_extraction_failure_cleans_staging_and_preserves_cached_archive(self):
        archive = self.base / 'invalid.tar.gz'
        archive.write_bytes(b'verified bytes but not a tar archive')
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$spec=[pscustomobject]@{sha256='ARCHIVE_SHA';python_exe_sha256=('0'*64)}
$cache=Join-Path $homePath 'cache/python'; [IO.Directory]::CreateDirectory($cache) | Out-Null
$archive=Join-Path $cache ($spec.sha256+'.tar.gz')
Copy-Item -LiteralPath 'ARCHIVE_PATH' -Destination $archive
function Invoke-WebRequest {throw 'offline cache must not download'}
try { Install-DoSutiPython $homePath $spec; throw 'unexpected success' }
catch { if($_.Exception.Message -notmatch 'Cannot extract Python') {throw} }
if(@(Get-ChildItem -LiteralPath (Join-Path $homePath 'runtime') -Force).Count) {throw 'failed extraction left payload'}
if((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $spec.sha256) {throw 'cached archive changed'}
""".replace('ARCHIVE_SHA', sha).replace('ARCHIVE_PATH', str(archive)))

    def test_executable_checksum_failure_cleans_extraction(self):
        archive = self.base / 'python.tar.gz'
        payload = b'incorrect executable'
        with tarfile.open(archive, 'w:gz') as tar:
            item = tarfile.TarInfo('python/python.exe'); item.size = len(payload)
            tar.addfile(item, io.BytesIO(payload))
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$spec=[pscustomobject]@{sha256='ARCHIVE_SHA';python_exe_sha256=('0'*64)}
$cache=Join-Path $homePath 'cache/python'; [IO.Directory]::CreateDirectory($cache) | Out-Null
Copy-Item -LiteralPath 'ARCHIVE_PATH' -Destination (Join-Path $cache ($spec.sha256+'.tar.gz'))
function Invoke-WebRequest {throw 'offline cache must not download'}
function Test-DoSutiPython {throw 'unverified executable was launched'}
try { Install-DoSutiPython $homePath $spec; throw 'unexpected success' }
catch { if($_.Exception.Message -notmatch 'executable SHA256 mismatch') {throw} }
if(@(Get-ChildItem -LiteralPath (Join-Path $homePath 'runtime') -Force).Count) {throw 'checksum failure left payload'}
""".replace('ARCHIVE_SHA', sha).replace('ARCHIVE_PATH', str(archive)))

    def test_unverified_private_executable_is_not_launched(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$folder=Join-Path $homePath 'runtime/python'; [IO.Directory]::CreateDirectory($folder) | Out-Null
[IO.File]::WriteAllText((Join-Path $folder 'python.exe'),'damaged')
function Get-Command {param($Name,$CommandType,$ErrorAction) return $null}
function Test-DoSutiPython {throw 'unverified executable launched'}
function Repair-DoSutiPython {throw 'unverified original preserved for repair'}
try {Resolve-DoSutiPython $homePath '' ([pscustomobject]@{python_exe_sha256=('0'*64)}); throw 'unexpected success'}
catch {if($_.Exception.Message -notmatch 'preserved for repair') {throw}}
""")

    def test_private_repair_keeps_backup_and_never_executes_old_copy(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$folder=Join-Path $homePath 'runtime/python';[IO.Directory]::CreateDirectory($folder) | Out-Null
[IO.File]::WriteAllText((Join-Path $folder 'python.exe'),'old damaged bytes')
# Byte-range lock interoperability has a separate native Windows test.
function Lock-DoSutiHeavyFile {}
function Unlock-DoSutiHeavyFile {}
function Install-DoSutiPython {param($HomePath,$Spec,$TargetName='python')
 if(-not (Test-Path -LiteralPath (Join-Path $HomePath 'runtime/python/python.exe'))) {throw 'old tree moved before preparation'}
 $target=Join-Path $HomePath ('runtime/'+$TargetName);[IO.Directory]::CreateDirectory($target) | Out-Null
 [IO.File]::WriteAllText((Join-Path $target 'python.exe'),'verified fixture')
 [pscustomobject]@{executable=(Join-Path $target 'python.exe')}
}
function Test-DoSutiPython {param($Executable) [pscustomobject]@{executable=$Executable} }
$sha=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes('verified fixture'))).Replace('-','')
$r=Repair-DoSutiPython $homePath ([pscustomobject]@{python_exe_sha256=$sha})
if((Get-Content -LiteralPath (Join-Path $r.recovery_backup 'python.exe') -Raw) -ne 'old damaged bytes') {throw 'backup lost'}
if((Get-Content -LiteralPath $r.executable -Raw) -ne 'verified fixture') {throw 'replacement missing'}
""")

    def test_private_repair_failure_rolls_back_and_offline_does_not_move_original(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$folder=Join-Path $homePath 'runtime/python';[IO.Directory]::CreateDirectory($folder) | Out-Null
$file=Join-Path $folder 'python.exe';[IO.File]::WriteAllText($file,'preserve')
function Lock-DoSutiHeavyFile {}
function Unlock-DoSutiHeavyFile {}
function Install-DoSutiPython {throw 'fixture install failure'}
try {Repair-DoSutiPython $homePath ([pscustomobject]@{});throw 'unexpected success'}
catch {if($_.Exception.Message -notmatch 'fixture install failure') {throw}}
if((Get-Content -LiteralPath $file -Raw) -ne 'preserve') {throw 'rollback lost original'}
try {Repair-DoSutiPython $homePath ([pscustomobject]@{sha256=('0'*64)}) -Offline;throw 'unexpected success'}
catch {if($_.Exception.Message -notmatch 'verified cached archive') {throw}}
if((Get-Content -LiteralPath $file -Raw) -ne 'preserve') {throw 'offline mutated original'}
""")

    def test_published_verification_interrupt_restores_previous_and_retry_keeps_backup(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$old=Join-Path $homePath 'runtime/python';[IO.Directory]::CreateDirectory($old) | Out-Null
[IO.File]::WriteAllText((Join-Path $old 'python.exe'),'previous bytes')
function Lock-DoSutiHeavyFile {}
function Unlock-DoSutiHeavyFile {}
function Install-DoSutiPython {param($HomePath,$Spec,$TargetName='python')
 if((Get-Content -LiteralPath (Join-Path $HomePath 'runtime/python/python.exe') -Raw) -ne 'previous bytes') {throw 'old tree moved before preparation'}
 $target=Join-Path $HomePath ('runtime/'+$TargetName);[IO.Directory]::CreateDirectory($target) | Out-Null
 [IO.File]::WriteAllText((Join-Path $target 'python.exe'),'new bytes')
 [pscustomobject]@{executable=(Join-Path $target 'python.exe')}
}
$script:interrupt=$true
function Test-DoSutiPython {param($Executable)
 if((Get-Content -LiteralPath $Executable -Raw) -ne 'new bytes') {throw 'previous Python was executed'}
 if($script:interrupt) {throw [OperationCanceledException]::new('fixture interrupted')}
 [pscustomobject]@{executable=$Executable}
}
$sha=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes('new bytes'))).Replace('-','')
$spec=[pscustomobject]@{python_exe_sha256=$sha}
try {Repair-DoSutiPython $homePath $spec;throw 'unexpected success'}
catch {if($_.Exception.Message -notmatch 'fixture interrupted') {throw}}
if((Get-Content -LiteralPath (Join-Path $old 'python.exe') -Raw) -ne 'previous bytes') {throw 'rollback lost original'}
$script:interrupt=$false
$r=Repair-DoSutiPython $homePath $spec
if((Get-Content -LiteralPath (Join-Path $r.recovery_backup 'python.exe') -Raw) -ne 'previous bytes') {throw 'retry lost backup'}
""")

    def test_busy_repair_preserves_original(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
$folder=Join-Path $homePath 'runtime/python';[IO.Directory]::CreateDirectory($folder) | Out-Null
$file=Join-Path $folder 'python.exe';[IO.File]::WriteAllText($file,'preserve')
function Lock-DoSutiHeavyFile {throw 'fixture busy'}
function Install-DoSutiPython {throw 'must not install'}
try {Repair-DoSutiPython $homePath ([pscustomobject]@{});throw 'unexpected success'}
catch {if($_.Exception.Message -notmatch 'worker is active') {throw}}
if((Get-Content -LiteralPath $file -Raw) -ne 'preserve') {throw 'busy repair moved original'}
""")

    @unittest.skipUnless(os.name=='nt','Win32/.NET byte-lock interoperability requires Windows')
    def test_native_repair_blocked_by_python_worker_lock(self):
        import managed_storage
        managed_storage.initialize(self.home)
        target=self.home/'runtime/python';target.mkdir(parents=True)
        (target/'python.exe').write_text('preserve')
        scripts=Path(__file__).resolve().parents[1]/'scripts'
        code=f'import sys;sys.path.insert(0,{str(scripts)!r});from storage_lock import file_lock;import time\nwith file_lock(sys.argv[1]):\n print("held",flush=True);time.sleep(30)'
        worker=subprocess.Popen([sys.executable,'-u','-c',code,str(self.home/'.heavy.lock')],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(worker.stdout.readline().strip(),'held')
            self.run_ps("function Install-DoSutiPython {throw 'must not install'}; "
                "try {Repair-DoSutiPython $homePath ([pscustomobject]@{});throw 'unexpected success'} "
                "catch {if($_.Exception.Message -notmatch 'worker is active') {throw}}")
            self.assertEqual((target/'python.exe').read_text(),'preserve')
        finally:
            worker.terminate();worker.wait(timeout=10);worker.stdout.close()
    def test_pending_cleanup_blocks_bootstrap_without_recreating_folders(self):
        self.run_ps("""
Initialize-DoSutiHome $homePath | Out-Null
@{seal='fixture';owner='fixture'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $homePath '.cleanup-pending.json')
Remove-Item -LiteralPath (Join-Path $homePath 'runtime') -Recurse
try {Initialize-DoSutiHome $homePath;throw 'unexpected success'}
catch {if($_.Exception.Message -notmatch 'legacy cleanup marker') {throw}}
if(Test-Path -LiteralPath (Join-Path $homePath 'runtime')) {throw 'setup recreated files during cleanup'}
""")
