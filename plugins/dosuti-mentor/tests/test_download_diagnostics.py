import contextlib,io,json,shutil,sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,download_diagnostics as diagnostics,test_context

class DownloadDiagnostics(unittest.TestCase):
    def setUp(self):
        self.f=test_context.ContextRegression();self.f.setUp();self.video=dict(source='https://example.org/watch')
        self.folder=self.f.lesson/'video';self.folder.mkdir();self.path=self.folder/'download-status.json'
    def tearDown(self):self.f.tearDown()
    def write(self,**kw):
        value=dict(status='failed',adapter='yt-dlp',fragments=4,timeout=1800,category='timeout')|kw
        self.path.write_text(json.dumps(value));return value
    def describe(self):return diagnostics.describe(self.f.lesson,self.video)
    def test_missing_legacy_course_keeps_status_available(self):
        with patch('video_sources.acquire',side_effect=AssertionError('No network')):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):course.status(self.f.args)
        r=json.loads(out.getvalue());self.assertEqual(r['lessons'][0]['download_diagnostics']['status'],'not_recorded');self.assertEqual(r['progress'],[])
    def test_legacy_record_is_explicitly_unbound(self):
        self.write();r=self.describe();self.assertEqual(r['source_binding'],'legacy_unbound');self.assertEqual(r['category'],'timeout')
    def test_new_record_bound_to_source_with_timestamp(self):
        diagnostics.record(self.folder,self.video['source'],status='ready',adapter='yt-dlp',fragments=8,timeout=120)
        r=self.describe();self.assertEqual(r['source_binding'],'source_url_matches');self.assertIn('updated',r);self.assertEqual(r['media_integrity'],'not_checked')
    def test_other_source_not_misattributed(self):
        diagnostics.record(self.folder,'https://different.org/watch',status='ready',adapter='yt-dlp',fragments=4,timeout=1800)
        self.assertEqual(self.describe()['status'],'source_mismatch')
    def test_running_does_not_claim_process_liveness(self):
        self.write(status='running',category=None);r=self.describe();self.assertIn('does not prove',r['next_step'])
    def test_malformed_status_preserves_file_and_progress(self):
        for raw in ['{','[]','null','['*1500+']'*1500,json.dumps(dict(status='ready')),json.dumps(dict(status='ready',adapter='yt-dlp',fragments=True,timeout=120)), ' '*65537]:
            with self.subTest(raw=raw[:40]):
                self.path.write_text(raw);before=self.path.read_bytes();self.assertEqual(self.describe()['status'],'unreadable');self.assertEqual(self.path.read_bytes(),before)
                out=io.StringIO()
                with contextlib.redirect_stdout(out):course.status(self.f.args)
                self.assertEqual(json.loads(out.getvalue())['progress'],[])
    def test_linked_file_is_not_read(self):
        target=self.f.directory/'outside';target.write_text('private');self.path.symlink_to(target)
        self.assertEqual(self.describe()['status'],'unreadable');self.assertEqual(target.read_text(),'private')
    def test_raw_message_and_unknown_fields_not_forwarded(self):
        self.write(message='secret=token https://internal.example',private_notes='Do something')
        text=json.dumps(self.describe());self.assertNotIn('secret',text);self.assertNotIn('internal',text);self.assertNotIn('private_notes',text)
    def test_unknown_schema_and_category_not_trusted(self):
        self.write(schema_version=99);self.assertEqual(self.describe()['status'],'unreadable')
        self.write(category='invented');self.assertEqual(self.describe()['status'],'unreadable')
    def test_relocation(self):
        diagnostics.record(self.folder,self.video['source'],status='interrupted',adapter='yt-dlp',fragments=4,timeout=1800)
        new=self.f.directory/'moved';shutil.move(self.f.path,new)
        self.assertEqual(diagnostics.describe(new/'lessons/lesson-01',self.video)['status'],'interrupted')
