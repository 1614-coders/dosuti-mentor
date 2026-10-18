"""Real cleanup core via PowerShell; synthetic interpreter discovery on Mac."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import managed_storage as storage
import pilot

PWSH=os.environ.get('DOSUTI_TEST_PWSH') or shutil.which('pwsh') or shutil.which('powershell')
PACKAGE=Path(__file__).resolve().parents[3]

def lit(value):return "'"+str(value).replace("'","''")+"'"

@unittest.skipUnless(PWSH,'PowerShell required')
class Completion(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.root=storage.initialize(self.base/'данные курса')
        pilot.start(self.root/'run')
        (self.root/'setup.json').write_text(json.dumps(dict(schema='pilot_setup_v1',test_only=True,owner='qa-owner',attempts=[])))
        self.out=self.base/'отчёты';self.out.mkdir()
        self.package=self.base/'package';self.package.mkdir()
        shutil.copyfile(PACKAGE/'Complete-WindowsPilot.ps1',self.package/'Complete-WindowsPilot.ps1')
        shutil.copytree(PACKAGE/'plugins',self.package/'plugins',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        self.original=(PACKAGE/'Windows-Runtime.ps1').read_text()
        self.helper('function Resolve-DoSutiPython { [pscustomobject]@{executable='+lit(sys.executable)+'} }')
    def helper(self,suffix):
        (self.package/'Windows-Runtime.ps1').write_text(self.original+'\n'+suffix)
    def call(self,extra='',success=True):
        code="$ErrorActionPreference='Stop'; & "+lit(self.package/'Complete-WindowsPilot.ps1')+' -PilotRoot '+lit(self.root)+' -OutputDirectory '+lit(self.out)+' '+extra
        encoded=base64.b64encode(code.encode('utf-16-le')).decode()
        result=subprocess.run([PWSH,'-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',encoded],capture_output=True,text=True,timeout=40)
        if success:self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        else:self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(list(self.out.glob('.dosuti-cleanup-helper-*')))
        return result
    def plan(self):
        self.call();plans=list(self.out.glob('cleanup-*.json'))
        self.assertEqual(len(plans),1);return plans[0]
    def test_plan_then_apply_preserves_report_and_removes_owned_tree(self):
        plan=self.plan();self.assertTrue(self.root.exists())
        value=json.loads(plan.read_text());self.assertTrue(Path(value['report']).is_file())
        self.call('-Apply -PlanFile '+lit(plan))
        self.assertFalse(self.root.exists());self.assertTrue(Path(value['report']).is_file())
        receipt=json.loads(Path(value['receipt']).read_text())
        self.assertEqual(receipt['status'],'test_data_removed')
        self.assertFalse(receipt['plugin_removed'])
    def test_changed_data_refuses_deletion(self):
        plan=self.plan();(self.root/'keep').write_text('added after plan')
        self.call('-Apply -PlanFile '+lit(plan),success=False)
        self.assertEqual((self.root/'keep').read_text(),'added after plan')
    def test_missing_python_needs_explicit_download_and_leaves_data(self):
        self.helper("function Resolve-DoSutiPython {throw 'no external Python'}\nfunction Install-DoSutiPython {throw 'download must not occur'}")
        result=self.call(success=False)
        self.assertIn('AllowDownload',result.stderr)
        self.assertTrue((self.root/'run/pilot.json').is_file())
    def test_temporary_helper_path_is_removed_after_plan(self):
        record=self.out/'helper.json'
        self.helper("function Resolve-DoSutiPython {throw 'no external Python'}\n"
            'function Install-DoSutiPython {param($HomePath,$Spec)\n'
            '$HomePath | ConvertTo-Json | Set-Content -LiteralPath '+lit(record)+'\n'
            '[IO.File]::WriteAllText((Join-Path $HomePath "helper-only"),"fixture")\n'
            '[pscustomobject]@{executable='+lit(sys.executable)+'}\n}')
        self.call('-AllowDownload')
        path=Path(json.loads(record.read_text(encoding='utf-8-sig')))
        self.assertFalse(path.exists());self.assertTrue(self.root.exists())
    def test_output_inside_test_tree_rejected(self):
        self.out=self.root/'reports'
        self.call(success=False)
        self.assertTrue(self.root.exists())
    def test_early_failed_setup_without_course_uses_early_plan(self):
        shutil.rmtree(self.root/'run')
        (self.root/'reports').mkdir()
        (self.root/'reports/bootstrap.md').write_text('network failed before course')
        plan=self.plan();value=json.loads(plan.read_text())
        self.assertEqual(value['schema'],'setup_cleanup_v1')
        self.assertIn('network failed before course',Path(value['report']).read_text())
        self.call('-Apply -PlanFile '+lit(plan))
        self.assertFalse(self.root.exists());self.assertTrue(Path(value['report']).is_file())

    def test_early_bootstrap_without_setup_journal_can_be_removed(self):
        shutil.rmtree(self.root/'run');(self.root/'setup.json').unlink()
        plan=self.plan();value=json.loads(plan.read_text())
        self.assertEqual(value['schema'],'setup_cleanup_v1')
        self.call('-Apply -PlanFile '+lit(plan))
        self.assertFalse(self.root.exists())
