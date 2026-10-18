"""Early-failure export, ownership, frozen recovery and reuse of the deletion core."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import setup_cleanup as early
import managed_storage as storage
import pilot,pilot_cleanup as cleanup,pilot_setup
from storage_lock import file_lock

class EarlyCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.root=storage.initialize(self.base/'failed setup')
        (self.root/'reports').mkdir();(self.root/'reports/bootstrap.md').write_text('Python download failed')
        (self.root/'cache').mkdir();(self.root/'cache/cached.bin').write_bytes(b'partial download')
        self.path=self.base/'cleanup.json'
    def plan(self):return early.plan(self.root,self.path)
    def apply(self):return cleanup.apply(self.root,self.path,setup_only=True)
    def test_marker_only_bootstrap_exported_and_removed(self):
        result=self.plan();report=Path(result['report'])
        self.assertIn('Python download failed',report.read_text())
        self.assertIn('setup.json отсутствует',report.read_text())
        self.apply();self.assertFalse(self.root.exists());self.assertTrue(report.is_file())
        self.assertEqual(self.apply()['status'],'already_removed')
    def test_failed_setup_journal_and_logs_preserved_outside(self):
        pilot_setup.save(self.root,dict(schema=pilot_setup.SCHEMA,test_only=True,owner='qa',attempts=[dict(number=1,status='failed',environment={'system':'QA'},events=[])]))
        (self.root/'attempt-01-dependencies.log').write_text('pip download failed')
        report=Path(self.plan()['report']);self.apply()
        text=report.read_text();self.assertIn('pip download failed',text);self.assertIn('"status": "failed"',text)
    def test_qa_or_unknown_user_data_refuses_early_mode(self):
        pilot.start(self.root/'run')
        with self.assertRaisesRegex(ValueError,'QA run exists'):self.plan()
        self.assertTrue((self.root/'run/pilot.json').exists())
    def test_unknown_folder_is_preserved(self):
        (self.root/'my-course').mkdir();(self.root/'my-course/course.json').write_text('user data')
        with self.assertRaisesRegex(ValueError,'Unknown installation entry'):self.plan()
        self.assertTrue((self.root/'my-course/course.json').exists())
    def test_active_bootstrap_and_worker_block_plan(self):
        for name in ['.bootstrap.lock','.heavy.lock']:
            with file_lock(self.root/name):
                with self.assertRaises((TimeoutError,OSError)):self.plan()
        self.assertTrue(self.root.exists());self.assertFalse(self.path.exists())
    def test_changed_file_and_changed_report_block_apply(self):
        result=self.plan();(self.root/'cache/cached.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed after'):self.apply()
        self.assertTrue(self.root.exists())
        Path(result['report']).write_text('modified report')
        with self.assertRaisesRegex(ValueError,'report missing or changed'):self.apply()
    def test_partial_apply_freezes_setup_and_recovers_original_plan(self):
        self.plan();unlink=Path.unlink
        def interrupted(path,*a,**kw):
            if path.name=='cached.bin':raise OSError('fixture interrupted')
            return unlink(path,*a,**kw)
        with patch.object(Path,'unlink',interrupted):
            with self.assertRaises(OSError):self.apply()
        with self.assertRaisesRegex(ValueError,'cleanup is pending'):storage.validate(self.root)
        with self.assertRaisesRegex(ValueError,'cleanup is pending'):
            pilot_setup.prepare(type('Args',(),dict(root=self.root))())
        self.apply();self.assertFalse(self.root.exists())
    def test_incomplete_diagnostic_export_refuses_plan(self):
        (self.root/'reports/invalid.log').write_bytes(b'\xff\xfe\xff')
        with self.assertRaisesRegex(ValueError,'Incomplete setup report'):self.plan()
        self.assertTrue(self.path.with_name('cleanup-report.md').exists())
        self.assertFalse(self.path.exists());self.assertTrue(self.root.exists())
    def test_external_symlink_target_preserved_and_schema_not_interchangeable(self):
        outside=self.base/'outside';outside.write_text('keep')
        (self.root/'cache/link').symlink_to(outside)
        self.plan()
        with self.assertRaisesRegex(ValueError,'legacy plan'):cleanup.apply(self.root,self.path)
        self.apply();self.assertEqual(outside.read_text(),'keep')
    def test_missing_ownership_and_inside_output_refused(self):
        with self.assertRaisesRegex(ValueError,'outside'):early.plan(self.root,self.root/'plan.json')
        (self.root/'.dosuti-home.json').unlink()
        with self.assertRaises((ValueError,OSError)):self.plan()
        self.assertTrue((self.root/'cache/cached.bin').exists())

if __name__=='__main__':unittest.main()
