"""Native two-process safety reproducers; installer functions remain unmodified.

These safety assertions may FAIL on the unfixed installer. A skip is not evidence
that a race is absent. Archives contain inert bytes, never an executable Python.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from powershell_test_support import literal, powershell_command, powershell_executable

PWSH = powershell_executable()
LIBRARY = Path(__file__).resolve().parents[1] / 'scripts/Windows-Runtime.ps1'
PAYLOAD = b'Inert Python fixture. Do not execute.\n'

WORKER = r'''
$ErrorActionPreference='Stop'
. @@LIBRARY@@
$homePath=@@HOME@@; $control=@@CONTROL@@; $fixture=@@ARCHIVE@@
$mode=@@MODE@@; $role=@@ROLE@@
$spec=Get-Content -LiteralPath @@SPEC@@ -Raw | ConvertFrom-Json
function Signal([string]$Name) { [IO.File]::WriteAllText((Join-Path $control $Name),[DateTime]::UtcNow.ToString('o')) }
function Wait-Signal([string]$Name,[switch]$Optional) {
 $clock=[Diagnostics.Stopwatch]::StartNew()
 while(-not (Test-Path -LiteralPath (Join-Path $control $Name))) {
  if($clock.Elapsed.TotalSeconds -gt 12) {
   if($Optional) {return}
   throw ('Fixture barrier timed out: '+$Name)
  }
  Start-Sleep -Milliseconds 20
 }
}
function Get-FileHash {
 param([string]$LiteralPath,[string]$Algorithm)
 if($mode -in @('winner_failure','winner_success')) {
  if([IO.Path]::GetFullPath($LiteralPath) -eq [IO.Path]::GetFullPath((Join-Path $homePath ('cache/python/'+$spec.sha256+'.tar.gz')))) {
   # Both workers have passed the installer's target-absence check.
   Signal ('ready-'+$role)
   $other=if($role -eq 'a') {'b'} else {'a'}
   Wait-Signal ('ready-'+$other) -Optional
  }
  if($role -eq 'b' -and $LiteralPath -match '[\\/]\.python-staging-[^\\/]+[\\/]python[\\/]python\.exe$') {
   Signal 'candidate-b'
   Wait-Signal 'done-a'
   if($mode -eq 'winner_failure') {throw 'Fixture B failed before publishing its candidate'}
  }
 }
 if($mode -eq 'interrupted' -and $LiteralPath -match '[\\/]\.python-staging-[^\\/]+[\\/]python[\\/]python\.exe$') {
  Signal 'interrupt-ready'
  Wait-Signal 'never-release'
 }
 Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $LiteralPath -Algorithm $Algorithm
}
function Invoke-WebRequest {
 param($Uri,$OutFile,[switch]$UseBasicParsing,$TimeoutSec)
 # No network is used. Keep the real installer's download/hash/move/finally flow.
 if($mode -eq 'download_failure' -and $role -eq 'b') {Wait-Signal 'part-a'}
 [IO.File]::WriteAllBytes($OutFile,[IO.File]::ReadAllBytes($fixture))
 if($mode -eq 'download_failure') {
  Signal ('part-'+$role)
  if($role -eq 'a') {Wait-Signal 'part-b' -Optional;throw 'Fixture A download failed'}
  Wait-Signal 'done-a'
 }
}
function Test-DoSutiPython {
 param([string]$Executable,[string[]]$LauncherArgs=@(),[string]$HomePath)
 if(-not $Executable.StartsWith((Join-Path $homePath 'runtime'),[StringComparison]::OrdinalIgnoreCase)) {return $null}
 if($mode -eq 'repair_failure' -and $Executable -match 'python-candidate-') {throw 'Fixture candidate probe failed'}
 if(-not (Test-Path -LiteralPath $Executable)) {return $null}
 if((Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash -ne $spec.python_exe_sha256) {return $null}
 [pscustomobject]@{executable=$Executable;version='3.12.15'}
}
function Set-Content {
 param([string]$LiteralPath,[string]$Encoding,[Parameter(ValueFromPipeline=$true)]$Value)
 process {
  if($mode -eq 'home') {
   Signal ('home-ready-'+$role)
   $other=if($role -eq 'a') {'b'} else {'a'}
   Wait-Signal ('home-ready-'+$other) -Optional
   if($role -eq 'b') {Wait-Signal 'done-a'}
  }
  Microsoft.PowerShell.Management\Set-Content -LiteralPath $LiteralPath -Encoding $Encoding -Value $Value
 }
}
$result=@{ok=$false;role=$role;error=$null;pid=$PID;powershell=$PSVersionTable.PSVersion.ToString()}
try {
 if($mode -in @('home','winner_failure','winner_success','download_failure')) {
  Signal ('start-'+$role)
  $other=if($role -eq 'a') {'b'} else {'a'}
  Wait-Signal ('start-'+$other)
  # Both processes are live, but A enters the critical operation first.
  if($role -eq 'b' -and $mode -in @('winner_failure','winner_success')) {Wait-Signal 'ready-a'}
  if($role -eq 'b' -and $mode -eq 'download_failure') {Wait-Signal 'part-a'}
 }
 if($mode -in @('home','initialize')) {
  $null=Initialize-DoSutiHome $homePath
 } else {
  $selected=Resolve-DoSutiPython $homePath '' $spec
  $result.executable=$selected.executable
  $result.backup=$selected.recovery_backup
  if($role -eq 'a' -and $mode -in @('winner_failure','winner_success')) {
   [IO.File]::WriteAllText((Join-Path $homePath 'runtime/python/winner-a.txt'),'preserve winner')
  }
 }
 $result.home_id=(Get-Content -LiteralPath (Join-Path $homePath '.dosuti-home.json') -Raw | ConvertFrom-Json).id
 $result.ok=$true
} catch {$result.error=$_.Exception.Message}
finally {
 [IO.File]::WriteAllText((Join-Path $control ('result-'+$role+'.json')),($result|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
 Signal ('done-'+$role)
}
'''


class RaceFixture:
    def __init__(self):
        import tarfile
        self.base=Path(tempfile.mkdtemp(prefix='dosuti-install-race-')).resolve()
        try:
            self.home=self.base/'данные с пробелами'
            self.control=self.base/'control';self.control.mkdir()
            self.archive=self.base/'fixture.tar.gz'
            with tarfile.open(self.archive,'w:gz') as archive:
                for name,raw in (('python/python.exe',PAYLOAD),('python/stdlib.txt',b'fixture standard library')):
                    item=tarfile.TarInfo(name);item.size=len(raw);archive.addfile(item,io.BytesIO(raw))
            self.spec=dict(url='https://example.invalid/never-contacted',
                sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                python_exe_sha256=hashlib.sha256(PAYLOAD).hexdigest())
            self.spec_path=self.base/'spec.json';self.spec_path.write_text(json.dumps(self.spec),encoding='utf-8')
            self.processes=[];self.handles=[];self.preserve=False
            self.case='fixture'
        except BaseException:
            shutil.rmtree(self.base)
            raise

    def script(self,mode,role):
        values=dict(LIBRARY=LIBRARY,HOME=self.home,CONTROL=self.control,ARCHIVE=self.archive,
                    SPEC=self.spec_path,MODE=mode,ROLE=role)
        code=WORKER
        for key,value in values.items():code=code.replace('@@'+key+'@@',literal(value))
        return code

    def close(self):
        failures=[]
        for process in self.processes:
            try:
                if process.poll() is None:
                    killer=Path(os.environ['SystemRoot'])/'System32/taskkill.exe'
                    subprocess.run([str(killer),'/PID',str(process.pid),'/T','/F'],
                                   capture_output=True,timeout=10)
                    process.wait(timeout=5)
            except BaseException as error:
                failures.append(error)
        for stream in self.handles:
            try:stream.close()
            except BaseException as error:failures.append(error)
        if failures:self.preserve=True
        if not self.preserve:shutil.rmtree(self.base)
        if failures:
            raise RuntimeError('Could not complete worker/output cleanup; fixture preserved at '+str(self.base)) from failures[0]

    def run(self,mode,*roles):
        jobs=[]
        for role in roles:
            output=(self.control/(role+'.stdout')).open('wb');self.handles.append(output)
            errors=(self.control/(role+'.stderr')).open('wb');self.handles.append(errors)
            process=subprocess.Popen(powershell_command(PWSH,self.script(mode,role)),
                                     cwd=self.base,stdout=output,stderr=errors)
            self.processes.append(process);jobs.append((role,process))
        deadline=time.monotonic()+50
        results={}
        for role,process in jobs:
            process.wait(timeout=max(0.1,deadline-time.monotonic()))
            path=self.control/('result-'+role+'.json')
            if process.returncode or not path.is_file():
                raise AssertionError('PowerShell worker failed: '+(self.control/(role+'.stderr')).read_text(encoding='utf-8',errors='replace'))
            results[role]=json.loads(path.read_text(encoding='utf-8-sig'))
        print('RACE_EVIDENCE '+json.dumps(dict(case=self.case,mode=mode,results=results,
            signals={p.name:p.read_text(encoding='utf-8-sig') for p in self.control.iterdir()
                     if p.is_file() and not p.suffix},
            runtime_files=sorted(str(p.relative_to(self.home)) for p in (self.home/'runtime').rglob('*') if p.is_file()),
            cache_files=sorted(p.name for p in (self.home/'cache/python').glob('*'))),ensure_ascii=True),flush=True)
        return results

    def cache(self):
        folder=self.home/'cache/python';folder.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(self.archive,folder/(self.spec['sha256']+'.tar.gz'))


class InstallRaceFixtures(unittest.TestCase):
    """Portable checks validate test inputs, not Windows installer behavior."""
    def setUp(self):
        self.f=RaceFixture();self.addCleanup(self.f.close)

    def test_archive_has_only_inert_payload_with_matching_hashes(self):
        import tarfile
        with tarfile.open(self.f.archive) as archive:
            self.assertEqual(archive.getnames(),['python/python.exe','python/stdlib.txt'])
            self.assertEqual(archive.extractfile('python/python.exe').read(),PAYLOAD)
        self.assertEqual(hashlib.sha256(self.f.archive.read_bytes()).hexdigest(),self.f.spec['sha256'])
        self.assertEqual(hashlib.sha256(PAYLOAD).hexdigest(),self.f.spec['python_exe_sha256'])

    def test_worker_uses_canonical_install_and_real_publication_operations(self):
        code=self.f.script('winner_failure','a')
        self.assertNotIn('@@',code)
        self.assertIn(str(LIBRARY),code)
        self.assertIn('Resolve-DoSutiPython $homePath',code)
        for name in ('Initialize-DoSutiHome','Install-DoSutiPython','Resolve-DoSutiPython','Repair-DoSutiPython','Move-Item','Remove-Item','Get-DoSutiTar'):
            self.assertNotIn('function '+name,code)


@unittest.skipUnless(os.name=='nt' and PWSH,'Requires native Windows and PowerShell; Mac fixtures cannot confirm this race')
class WindowsInstallRace(unittest.TestCase):
    def setUp(self):
        self.f=RaceFixture();self.f.case=self.id();self.addCleanup(self.f.close)

    def initialize(self,cached=True):
        result=self.f.run('initialize','init')['init'];self.assertTrue(result['ok'],result)
        if cached:self.f.cache()

    def assert_runtime(self):
        path=self.f.home/'runtime/python/python.exe'
        self.assertTrue(path.is_file(),'A failed contender removed the successful installation')
        self.assertEqual(path.read_bytes(),PAYLOAD)
        self.assertEqual((path.parent/'stdlib.txt').read_bytes(),b'fixture standard library')

    def test_first_home_initialization_keeps_one_stable_identity(self):
        results=self.f.run('home','a','b')
        for result in results.values():self.assertTrue(result['ok'],result)
        self.assertEqual(results['a']['home_id'],results['b']['home_id'],
                         'Both initializers succeeded with different managed-home identities')

    def test_failed_contender_preserves_winner_and_retry_recovers(self):
        self.initialize()
        results=self.f.run('winner_failure','a','b')
        self.assertTrue(results['a']['ok'],results)
        # Preserve the original safety assertions even when the contender reuses the winner.
        with self.subTest(phase='winner must survive'):
            self.assert_runtime()
            self.assertEqual((self.f.home/'runtime/python/winner-a.txt').read_text(),'preserve winner')
        retry=self.f.run('retry','retry')['retry'];self.assertTrue(retry['ok'],retry)
        self.assert_runtime()
        self.assertFalse(list((self.f.home/'runtime').glob('.python-staging-*')))

    def test_two_successful_contenders_do_not_nest_or_remove_the_winner(self):
        self.initialize()
        results=self.f.run('winner_success','a','b')
        self.assertTrue(results['a']['ok'],results)
        self.assertTrue(results['b']['ok'],results)
        self.assert_runtime()
        self.assertEqual((self.f.home/'runtime/python/winner-a.txt').read_text(),'preserve winner')
        self.assertFalse((self.f.home/'runtime/python/python').exists(),'Second checkout was nested inside the first')

    def test_failed_download_cannot_remove_other_workers_partial_and_retry_recovers(self):
        self.initialize(cached=False)
        results=self.f.run('download_failure','a','b')
        self.assertFalse(results['a']['ok'],results)
        with self.subTest(phase='independent download must survive'):
            self.assertTrue(results['b']['ok'],results)
            self.assert_runtime()
        retry=self.f.run('retry','retry')['retry'];self.assertTrue(retry['ok'],retry)
        self.assert_runtime()
        self.assertFalse(list((self.f.home/'cache/python').glob('*.part')))

    def test_first_install_observes_existing_heavy_worker_lock(self):
        self.initialize()
        sys.path.insert(0,str(LIBRARY.parent))
        from storage_lock import file_lock
        with file_lock(self.f.home/'.heavy.lock'):
            result=self.f.run('heavy','worker')['worker']
            self.assertFalse(result['ok'],'First installation ignored the shared heavy-worker lock')
            self.assertFalse((self.f.home/'runtime/python').exists())

    def test_failed_repair_preserves_old_files_then_retry_keeps_backup(self):
        self.initialize()
        first=self.f.run('retry','first')['first'];self.assertTrue(first['ok'],first)
        target=self.f.home/'runtime/python';(target/'python.exe').write_bytes(b'damaged fixture')
        (target/'keep.txt').write_text('old installation data')
        failed=self.f.run('repair_failure','failed')['failed']
        self.assertFalse(failed['ok'],failed)
        self.assertEqual((target/'python.exe').read_bytes(),b'damaged fixture')
        self.assertEqual((target/'keep.txt').read_text(),'old installation data')
        retry=self.f.run('retry','retry')['retry'];self.assertTrue(retry['ok'],retry)
        self.assert_runtime()
        backup=Path(retry['backup'])
        self.assertEqual((backup/'python.exe').read_bytes(),b'damaged fixture')
        self.assertEqual((backup/'keep.txt').read_text(),'old installation data')


if __name__=='__main__':
    if os.name!='nt' or not PWSH:
        raise SystemExit('Native race acceptance requires Windows and PowerShell; skips are not accepted')
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(WindowsInstallRace)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    print('RACE_SUMMARY '+json.dumps(dict(total=result.testsRun,
        failures=[case.id() for case,_ in result.failures],errors=[case.id() for case,_ in result.errors],
        skipped=[case.id() for case,_ in result.skipped])),flush=True)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped and result.testsRun==6 else 1)
