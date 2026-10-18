"""User-facing preparation spans download, worker, publication and a truthful handoff."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import course
from preparation_status import tracked,phase,describe
from preparation_status import record
from processing_status import Reporter
from storage_lock import file_lock


class PreparationStatus(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.lesson=self.root/'course/lessons/one'
        self.args=NS(course=self.root/'course',lesson='one',title='One',video='https://example.invalid/video',
                     order=1,start=0,duration=60)
        with contextlib.redirect_stdout(io.StringIO()):
            course.init(self.args);course.add_lesson(self.args)

    def test_legacy_status_is_unknown_without_creating_files(self):
        before=set(self.lesson.rglob('*'))
        self.assertEqual(describe(self.lesson),{'status':'unavailable'})
        self.assertEqual(set(self.lesson.rglob('*')),before)
        result=subprocess.run([sys.executable,str(SCRIPTS/'course.py'),'preparation-status',
            '--course',str(self.args.course),'--lesson','one'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'status':'unavailable'})

    def test_download_error_then_retry_never_claims_teaching_ready(self):
        seen=[]
        @tracked
        def fail(args):
            seen.append(describe(self.lesson))
            raise OSError('Network unavailable')
        with self.assertRaisesRegex(OSError,'Network unavailable'):fail(self.args)
        result=describe(self.lesson)
        self.assertEqual((seen[0]['stage'],seen[0]['status']),('download','running'))
        self.assertEqual(result['status'],'failed')
        self.assertIn('Network unavailable',result['message'])
        @tracked
        def success(args):phase('publish')
        success(self.args)
        result=describe(self.lesson)
        self.assertEqual((result['stage'],result['status']),('review','complete'))
        self.assertFalse(result['teaching_ready'])

        self.assertIsNone(result['percent'])
        self.assertNotIn('error',result)

    def test_worker_overlay_and_actual_stop_reason(self):
        @tracked
        def run(args):
            working=self.lesson/'.jobs/example';working.mkdir(parents=True)
            phase('worker',worker_key='example')
            with file_lock(working/'.processing.lock'):
                worker=Reporter(working);worker.set('asr')
                result=describe(self.lesson)
                self.assertEqual((result['stage'],result['status']),('asr','running'))
                worker.set('asr','failed',RuntimeError('Actual ASR cause'))
            raise RuntimeError('Child failed')
        with self.assertRaises(RuntimeError):run(self.args)
        result=describe(self.lesson)
        self.assertIn('Actual ASR cause',result['message'])
        self.assertEqual(result['stage'],'asr')

    def test_previous_attempt_cannot_overwrite_newer_attempt(self):
        started=threading.Event();release=threading.Event()
        @tracked
        def slow(args):
            started.set();self.assertTrue(release.wait(5));phase('publish')
        @tracked
        def fast(args):phase('model_install')
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(slow,self.args)
            try:
                self.assertTrue(started.wait(5));fast(self.args)
                before=(self.lesson/'.preparation/current.json').read_bytes()
            finally:release.set()
            future.result(timeout=5)
        self.assertEqual(before,(self.lesson/'.preparation/current.json').read_bytes())
        self.assertEqual(describe(self.lesson)['stage'],'review')

    def test_source_change_and_invalid_index_are_unknown(self):
        @tracked
        def run(args):pass
        run(self.args)
        data=course.read(self.lesson/'lesson.json');data['video']['source']='different.mp4'
        course.save(self.lesson/'lesson.json',data)
        self.assertEqual(describe(self.lesson)['reason'],'source_changed')
        index=self.lesson/'.preparation/current.json'
        for value in ('{','{"attempt":"../../outside"}','x'*5000):
            index.write_text(value)
            self.assertEqual(describe(self.lesson),{'status':'unavailable'})

    def test_linked_history_cannot_redirect_writes(self):
        outside=self.root/'outside';outside.mkdir()
        try:(self.lesson/'.preparation').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('Symbolic links unavailable')
        @tracked
        def run(args):pass
        with self.assertRaises(ValueError):run(self.args)
        self.assertEqual(list(outside.iterdir()),[])
        self.assertEqual(describe(self.lesson),{'status':'unavailable'})

    def test_killed_parent_is_not_left_running(self):
        script='''import sys,time
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from preparation_status import tracked
@tracked
def run(args):
 Path(sys.argv[3]).write_text('ready')
 time.sleep(30)
run(NS(course=Path(sys.argv[2]),lesson='one',start=0,duration=60))
'''
        ready=self.root/'ready'
        process=subprocess.Popen([sys.executable,'-c',script,str(SCRIPTS),str(self.args.course),str(ready)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not ready.exists() and time.monotonic()<deadline and process.poll() is None:time.sleep(.02)
            self.assertTrue(ready.exists())
            self.assertEqual(describe(self.lesson)['status'],'running')
        finally:
            process.kill();process.communicate(timeout=5)
        result=describe(self.lesson)
        self.assertEqual(result['status'],'interrupted')
        self.assertTrue(result['elapsed_is_last_observed'])
        self.assertFalse(result['teaching_ready'])


class ChatPreparationStatus(unittest.TestCase):
    def setUp(self):
        from test_context import ContextRegression
        self.f=ContextRegression();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.folder=self.f.add('first',0,20,[(1,5,'Example')],[(10,b'frame')])
        @tracked
        def done(args):pass
        done(self.f.args)

    def args(self,step,**changes):
        return NS(**(vars(self.f.args)|dict(step=step,time=0,window=20)|changes))

    def test_cli_records_review_and_ready_as_json(self):
        for step in ('frames_review','ready'):
            result=subprocess.run([sys.executable,str(SCRIPTS/'course.py'),'preparation-step',
                '--course',str(self.f.path),'--lesson',self.f.args.lesson,'--step',step,
                '--time','0','--window','20'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            status=json.loads(result.stdout)
            self.assertEqual(status['teaching_ready'],step=='ready')
            self.assertNotIn('review_inputs',status)

    def test_ready_requires_actual_review_record_and_exact_scope(self):
        before=(self.folder/'tutor_packet.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'actual frames/code review'):record(self.args('ready'))
        self.assertEqual(record(self.args('frames_review'))['status'],'reported')
        with self.assertRaisesRegex(ValueError,'exact scope'):record(self.args('ready',window=10))
        result=record(self.args('ready'))
        self.assertTrue(result['teaching_ready'])
        self.assertEqual(result['review_scope'],[0,20])
        self.assertEqual(result['readiness_basis'],'assistant_report_for_scope_not_automatic_proof')
        self.assertIsNone(result['percent'])
        self.assertEqual((self.folder/'tutor_packet.json').read_bytes(),before)

    def test_missing_coverage_cannot_be_marked_ready(self):
        record(self.args('frames_review',window=30))
        with self.assertRaisesRegex(ValueError,'missing intervals'):
            record(self.args('ready',window=30))
        self.assertFalse(describe(self.f.lesson)['teaching_ready'])

    def test_frame_change_invalidates_ready_without_rehashing_video(self):
        record(self.args('frames_review'));record(self.args('ready'))
        (self.folder/'frames/0.jpg').write_bytes(b'changed frame')
        result=describe(self.f.lesson)
        self.assertFalse(result['teaching_ready'])
        self.assertEqual(result['stage'],'review')

    def test_attached_code_requires_code_review(self):
        source=self.f.directory/'Example.java';source.write_text('class Example {}')
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(NS(**(vars(self.f.args)|dict(kind='code',source=str(source),none=False,ref=None))))
        record(self.args('frames_review'))
        with self.assertRaisesRegex(ValueError,'actual frames/code review'):record(self.args('ready'))
        record(self.args('code_review'))
        self.assertTrue(record(self.args('ready'))['teaching_ready'])

