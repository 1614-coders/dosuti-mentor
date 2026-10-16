import contextlib,io,json,sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,test_context,download_planning as plan,maintenance

class Maintenance(unittest.TestCase):
    def setUp(self):self.f=test_context.ContextRegression();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return NS(**(vars(self.f.args)|kw))
    def test_quality_ceiling_no_high_fallback(self):
        self.assertEqual(plan.quality_format('720'),'bv*[height<=720]+ba/b[height<=720]')
        for q in (720,'2000',None):
            with self.assertRaises(ValueError):plan.quality_format(q)
    def test_estimates_multiple_streams_and_unknown(self):
        self.assertEqual(plan.estimate(dict(requested_formats=[dict(filesize=100),dict(filesize=20)])),(120,'exact_metadata'))
        self.assertEqual(plan.estimate(dict(duration=10,tbr=8)),(10000,'estimate'))
        self.assertEqual(plan.estimate(dict(duration=10)),(None,'unknown'))
        self.assertEqual(plan.estimate(dict(filesize=float('nan'))),(None,'unknown'))
    def test_local_plan_no_network_or_changes(self):
        before=(self.f.lesson/'lesson.json').read_bytes()
        with patch.object(plan.subprocess,'run',side_effect=AssertionError('No network')):r=plan.download_plan(self.args(video_quality='480'))
        self.assertFalse(r['quality_applies']);self.assertFalse(r['metadata_network']);self.assertEqual(before,(self.f.lesson/'lesson.json').read_bytes())
    def remote(self):
        p=self.f.lesson/'lesson.json';d=course.read(p);d['video']=dict(source='https://example.org/watch');course.save(p,d)
    def test_remote_plan_allowlist_and_format(self):
        self.remote()
        with patch.object(plan.subprocess,'run',return_value=NS(stdout=json.dumps(dict(filesize=100,height=720,url='secret',http_headers={'Cookie':'secret'})))) as run:
            r=plan.download_plan(self.args(video_quality='720'))
        self.assertNotIn('secret',json.dumps(r));self.assertEqual(r['estimated_download_bytes'],100);self.assertIn(plan.quality_format('720'),run.call_args.args[0])
    def test_playlist_and_live_rejected(self):
        self.remote()
        for info in [dict(_type='playlist'),dict(is_live=True),dict(live_status='is_upcoming')]:
            with patch.object(plan.subprocess,'run',return_value=NS(stdout=json.dumps(info))),self.assertRaises(ValueError):plan.download_plan(self.args())
    def test_direct_size_not_invented(self):
        p=self.f.lesson/'lesson.json';d=course.read(p);d['video']=dict(source='https://example.org/movie.mp4');course.save(p,d)
        with patch.object(plan.subprocess,'run',side_effect=AssertionError('No network')):r=plan.download_plan(self.args())
        self.assertIsNone(r['estimated_download_bytes']);self.assertFalse(r['quality_applies'])
    def test_doctor_does_not_install(self):
        r=maintenance.doctor(self.args(model_path=self.f.directory/'missing-model'));self.assertEqual(r['model']['status'],'not_ready');self.assertFalse(r['network_used']);self.assertTrue(r['read_only'])
    def test_cleanup_preserves_referenced_job_and_progress(self):
        job=self.f.lesson/'.jobs/example';job.mkdir(parents=True);(job/'checkpoint.json').write_text('{"status":"complete"}');(job/'data').write_bytes(b'keep')
        p=self.f.lesson/'lesson.json';d=course.read(p);d['future_reference']=str(job/'data');course.save(p,d)
        before={str(p):p.read_bytes() for p in self.f.path.rglob('*') if p.is_file()}
        r=maintenance.cleanup_plan(self.args());self.assertTrue(r['areas'][0]['metadata_reference_detected']);self.assertEqual(r['areas'][0]['decision'],'preserve');self.assertFalse(r['deletion_supported']);self.assertEqual(r['proposed_deletions'],[])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.f.path.rglob('*') if p.is_file()})
    def test_incomplete_or_unreferenced_job_not_assumed_safe(self):
        job=self.f.lesson/'.jobs/example';job.mkdir(parents=True);(job/'checkpoint.json').write_text('{')
        r=maintenance.cleanup_plan(self.args());self.assertEqual(r['areas'][0]['decision'],'preserve');self.assertEqual(r['areas'][0]['recorded_state'],'unknown')

    def test_prepare_quality_reaches_acquire(self):
        with patch('video_sources.acquire',return_value=self.f.video) as acquire:
            course.download_video(self.f.lesson,course.read(self.f.lesson/'lesson.json'),2,240,'480')
        self.assertEqual(acquire.call_args.args[-1],'480')
    def test_doctor_model_check_creates_no_lock(self):
        missing=self.f.directory/'missing';before=set(self.f.directory.iterdir())
        maintenance.doctor(self.args(model_path=missing))
        self.assertEqual(before,set(self.f.directory.iterdir()))
