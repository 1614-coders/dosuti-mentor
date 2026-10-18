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
        encoded = base64.b64encode(code.encode('utf-16-le')).decode()
        result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

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
function tar.exe { & tar @args }
function Test-DoSutiPython {param($Executable,$LauncherArgs) [pscustomobject]@{executable=$Executable;version='3.12.15'} }
$r=Install-DoSutiPython $homePath $spec
if(-not (Test-Path -LiteralPath $r.executable)) {throw 'missing interpreter'}
$again=Resolve-DoSutiPython $homePath '' $spec
if(-not $again.owned -or $again.executable -ne $r.executable) {throw 'private reuse failed'}
""".replace('ARCHIVE_SHA', sha).replace('EXE_SHA', exe_sha).replace('ARCHIVE_PATH', str(archive)))

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
function Install-DoSutiPython {param($HomePath,$Spec)
 $target=Join-Path $HomePath 'runtime/python';[IO.Directory]::CreateDirectory($target) | Out-Null
 [IO.File]::WriteAllText((Join-Path $target 'python.exe'),'verified fixture')
 [pscustomobject]@{executable=(Join-Path $target 'python.exe')}
}
$r=Repair-DoSutiPython $homePath ([pscustomobject]@{})
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
