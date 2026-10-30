"""PDF import uses existing resources, revisions, rollback and unit sources."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pdf_fixtures as fixture
import course
import material_sources as ms
import test_study_units as fixtures

class PDFImport(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.StudyUnits();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.path=self.f.f.directory/'книга с пробелом.pdf';self.path.write_bytes(fixture.textbook())
    def attach(self,kind):
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(self.f.args(kind=kind,source=str(self.path),none=False,ref=None,resource_type='file'))
        return course.read(self.f.f.lesson/'lesson.json')[kind]
    def resource(self,action,identity,**kwargs):
        return ms.resource(self.f.args(action=action,resource_id=identity,expected_revision=None,notes=None,**kwargs))
    def files(self):
        return {str(p):p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file() and p.suffix!='.lock'}
    def test_homework_pdf_is_extracted_and_original_preserved(self):
        before=self.path.read_bytes();item=self.attach('homework')
        text=Path(item['file']).read_text(encoding='utf-8')
        self.assertIn('PDF page 2',text);self.assertEqual(item['origin']['extraction'],'pdf_text_layer_v1')
        self.assertEqual(Path(item['origin']['raw_file']).read_bytes(),before)
        self.assertEqual(self.path.read_bytes(),before)
    def test_homework_pdf_without_extension_is_detected_by_signature(self):
        renamed=self.path.with_suffix('');self.path.rename(renamed);self.path=renamed
        item=self.attach('homework')
        self.assertEqual(item['origin']['extraction'],'pdf_text_layer_v1')
    def test_resource_extract_review_unit_and_cached_read(self):
        item=self.attach('resources')['items'][0]
        state=self.resource('extract',item['id']);item=state['items'][0]
        self.assertEqual(item['status'],'extracted_not_reviewed');self.assertEqual(item['extraction'],'pdf_text_layer_v1')
        ms.resource(self.f.args(action='review',resource_id=item['id'],expected_revision=item['revision'],notes='Read pages 1 and 2; layout checked'))
        source=dict(kind='resource',role='external',notes='PDF pages 1-2, spatial layout reviewed',lesson='lesson-01',resource_id=item['id'],start_line=1,end_line=len(Path(item['extracted_file']).read_text(encoding='utf-8').splitlines()))
        self.f.save(self.f.card(sources=[source]));self.assertFalse(self.f.get()['stale'])
        process=subprocess.run([sys.executable,'-B',str(Path(course.__file__)),
            'learn-unit','--course',str(self.f.f.path),'--unit-id','maps','--profile','default',
            '--session','new-chat','--compact'],capture_output=True,encoding='utf-8',timeout=20,
            env=dict(os.environ,PYTHONUTF8='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertIn('PDF page 2',json.loads(process.stdout)['unit']['source_contexts'][0]['text'])
        before=self.files()
        with patch('pdf_content.extract',side_effect=AssertionError('Do not reprocess existing PDF extraction')):
            self.assertTrue(self.resource('extract',item['id'])['items'][0]['review_valid'])
        # The repeated write may update lesson revision; extracted bytes and review stay pinned.
        self.assertEqual(Path(item['extracted_file']).read_bytes(),before[item['extracted_file']])
    def test_failed_import_or_registration_leaves_no_orphan_files(self):
        before=self.files();self.path.write_bytes(b'%PDF-1.4 broken')
        with self.assertRaises(ValueError):self.attach('homework')
        self.assertEqual(self.files(),before)
        self.path.write_bytes(fixture.textbook())
        with patch('course.attach_local',side_effect=OSError('simulated registration error')):
            with self.assertRaises(OSError):ms.attach_material(self.f.args(kind='homework',source=str(self.path)))
        self.assertEqual(self.files(),before);self.assertTrue(self.path.exists())
    def test_refused_encrypted_and_compressed_pdf_leave_no_orphans(self):
        before=self.files()
        for data in (fixture.encrypted_empty(),fixture.document([' '*(17*1024*1024)],compressed=True)):
            with self.subTest(bytes=len(data)):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):self.attach('homework')
                self.assertEqual(self.files(),before)
                self.assertEqual(self.path.read_bytes(),data)

    def test_pdf_cannot_be_smuggled_as_remote_code(self):
        with self.assertRaisesRegex(ValueError,'not a code file'):
            ms.attach_material(self.f.args(kind='code',source=str(self.path)))
    def test_source_or_text_tampering_invalidates_review(self):
        item=self.attach('resources')['items'][0];item=self.resource('extract',item['id'])['items'][0]
        ms.resource(self.f.args(action='review',resource_id=item['id'],expected_revision=item['revision'],notes='Read source'))
        raw=Path(item['origin']['raw_file']);raw.chmod(0o600);raw.write_bytes(fixture.code())
        self.assertFalse(self.resource('list',None)['items'][0]['review_valid'])
    def test_corrupt_registered_pdf_stays_registered_without_partial_text(self):
        self.path.write_bytes(b'%PDF-1.4 broken');item=self.attach('resources')['items'][0]
        before=self.files()
        with self.assertRaises(ValueError):self.resource('extract',item['id'])
        self.assertEqual(self.files(),before)
    def test_concurrent_extraction_keeps_one_registered_text(self):
        item=self.attach('resources')['items'][0]
        def run(_):
            try:self.resource('extract',item['id']);return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        self.assertIn(True,results)
        current=self.resource('list',None)['items'][0]
        files=list(self.f.f.lesson.rglob('text-*.txt'))
        self.assertEqual(files,[Path(current['extracted_file'])])

if __name__=='__main__':unittest.main()
