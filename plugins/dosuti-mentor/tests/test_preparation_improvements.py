"""Cancel a real owned tree, fail before transfers, and report bounded facts."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import download_space
import http_resume
from preparation_control import REQUEST, PreparationCancelled, run
from preparation_status import tracked, phase, describe, cancel
from preparation_summary import summarize
import test_preparation_status as fixtures


class Cancellation(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PreparationStatus()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.folder = self.fixture.lesson
        self.args = self.fixture.args

    def test_cancel_live_tree_and_retry_preserves_checkpoint(self):
        # Both the child and its grandchild continuously write a heartbeat.
        root = self.fixture.root
        child_script = root / 'child.py'
        child_script.write_text('''import pathlib,subprocess,sys,time
p=pathlib.Path(sys.argv[1]); p.write_text('ready')
if len(sys.argv)>2: subprocess.Popen([sys.executable,__file__,sys.argv[2]])
while True:
 p.write_text(str(time.time()))
 time.sleep(.05)
''')
        parent_script = root / 'parent.py'
        parent_script.write_text('''import sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from preparation_status import tracked
from preparation_control import run,PreparationCancelled
@tracked
def work(args):
 Path(sys.argv[3]).write_text('finished audio checkpoint')
 run([sys.executable,sys.argv[4],sys.argv[5],sys.argv[6]],check=True)
try: work(NS(course=Path(sys.argv[2]),lesson='one',start=0,duration=60))
except PreparationCancelled: sys.exit(130)
''')
        checkpoint = root / 'audio.checkpoint'
        beats = [root / 'beat-child', root / 'beat-grandchild']
        process = subprocess.Popen([sys.executable, str(parent_script), str(fixtures.SCRIPTS), str(self.args.course),
            str(checkpoint), str(child_script), *map(str, beats)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            deadline = time.monotonic() + 5
            while not all(p.exists() for p in beats) and time.monotonic() < deadline: time.sleep(.02)
            self.assertTrue(all(p.exists() for p in beats))
            view = describe(self.folder)
            # Use the real CLI during active work, not a thread-only mock.
            result = subprocess.run([sys.executable, str(fixtures.SCRIPTS/'course.py'), 'preparation-cancel',
                '--course', str(self.args.course), '--lesson', 'one', '--attempt', view['attempt_id']],
                capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['requested'])
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130)
            observed = [p.read_bytes() for p in beats]
            time.sleep(.2)
            self.assertEqual(observed, [p.read_bytes() for p in beats])
            self.assertIsNone(unrelated.poll())
            self.assertEqual(checkpoint.read_text(), 'finished audio checkpoint')
            stopped = describe(self.folder)
            self.assertEqual(stopped['status'], 'interrupted')
            self.assertIn('отменена пользователем', stopped['error'])
            @tracked
            def retry(args):
                self.assertEqual(checkpoint.read_text(), 'finished audio checkpoint')
                phase('publish')
            retry(self.args)
            self.assertFalse(describe(self.folder)['cancellation_requested'])
            with self.assertRaisesRegex(ValueError, 'attempt changed'):
                cancel(NS(**vars(self.args), attempt=view['attempt_id']))
        finally:
            if process.poll() is None: process.kill()
            process.communicate(timeout=5)
            unrelated.kill(); unrelated.wait(timeout=5)

    def test_completed_and_old_attempt_requests_cannot_stop_new_work(self):
        @tracked
        def done(args): pass
        done(self.args)
        view = describe(self.folder)
        self.assertFalse(cancel(NS(**vars(self.args), attempt=view['attempt_id']))['requested'])
        with self.assertRaises(ValueError): cancel(NS(**vars(self.args), attempt='../escape'))
        self.assertFalse((self.folder/'.preparation'/view['attempt_id']/'cancel.json').exists())

    def test_cancelled_worker_keeps_its_last_real_stage_and_reason(self):
        from processing_status import Reporter
        from storage_lock import file_lock
        @tracked
        def prepare(args):
            worker = self.folder/'.jobs/one'; worker.mkdir(parents=True)
            phase('worker', worker_key='one')
            with file_lock(worker/'.processing.lock'):
                Reporter(worker).set('asr')
            raise PreparationCancelled()
        with self.assertRaises(PreparationCancelled): prepare(self.args)
        view = describe(self.folder)
        self.assertEqual((view['stage'], view['status']), ('asr', 'interrupted'))
        self.assertIn('отменена пользователем', view['error'])

    def test_cancelled_prepare_job_does_not_stay_pending(self):
        args = NS(**(vars(self.args) | dict(import_prepared=None, frame_every=10, model='small',
            model_path=self.fixture.root/'model', language='ru', prompt='', rebuild=False)))
        with patch.object(course, 'checked_video', return_value=(self.fixture.root/'video', 'a'*64)), patch('model_readiness.checked_model', return_value=dict(fingerprint='test')), patch.object(course, 'preparation_run', side_effect=PreparationCancelled):
            with self.assertRaises(PreparationCancelled): course.prepare(args)
        data = course.read(self.folder/'lesson.json')
        self.assertEqual(next(iter(data['processing_jobs'].values()))['status'], 'interrupted')
        self.assertEqual(data['prepared_fragments'], [])

    def test_subprocess_timeout_keeps_timeout_semantics(self):
        token = REQUEST.set(self.fixture.root/'cancel.json')
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                run([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=.1, capture_output=True)
            result = run([sys.executable, '-c', 'print("ok")'], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, 'ok\n')
        finally: REQUEST.reset(token)

    def test_qa_cancel_does_not_wait_for_use_lock(self):
        import pilot
        from storage_lock import file_lock
        qa = self.fixture.root / 'qa'
        pilot.start(qa)
        args = NS(**(vars(self.args) | dict(course=qa/'course')))
        course.add_lesson(args)
        @tracked
        def prepare(args):
            with file_lock(qa/'.pilot-use.lock'):
                view = describe(qa/'course/lessons/one')
                result = subprocess.run([sys.executable, str(fixtures.SCRIPTS/'course.py'), 'preparation-cancel',
                    '--course', str(args.course), '--lesson', 'one', '--attempt', view['attempt_id']],
                    capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)['requested'])
            phase('publish')
        with self.assertRaises(PreparationCancelled): prepare(args)

    def test_cancel_cannot_recreate_deleted_attempt(self):
        import preparation_status
        # Recreate the journal for the decorator's final report only after checking cancel.
        # The cancellation command itself must not recreate missing parents.
        @tracked
        def outer(args):
            view = describe(self.folder)
            folder = self.folder/'.preparation'/view['attempt_id']
            original = folder.rename(folder.with_name(folder.name+'-moved'))
            try:
                with patch.object(preparation_status, 'describe', return_value=view):
                    with self.assertRaises(OSError): cancel(NS(**vars(args), attempt=view['attempt_id']))
                self.assertFalse(folder.exists())
            finally: original.rename(folder)
        outer(self.args)


class SpaceChecks(unittest.TestCase):
    def test_known_and_unknown_sizes_and_unknown_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(download_space.shutil, 'disk_usage', return_value=NS(free=100)):
                with self.assertRaisesRegex(ValueError, 'перед скачиванием'): download_space.ensure(tmp, 200)
            with patch.object(download_space.shutil, 'disk_usage', return_value=NS(free=download_space.RESERVE+1000)):
                self.assertIsNone(download_space.ensure(tmp)['estimated_download_bytes'])
                self.assertTrue(download_space.ensure(tmp)['warnings'])
            with patch.object(download_space.shutil, 'disk_usage', side_effect=OSError('unknown')):
                self.assertIsNone(download_space.ensure(tmp, 50)['disk_free_bytes'])

    def test_direct_head_size_blocks_before_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'video.mp4'
            def metadata(command, **kwargs):
                self.assertIn('--head', command)  # A body transfer would fail this assertion.
                Path(command[command.index('--dump-header')+1]).write_text('HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n')
                return NS(returncode=0)
            with patch.object(http_resume, 'preparation_run', side_effect=metadata), patch.object(download_space.shutil, 'disk_usage', return_value=NS(free=10)):
                with self.assertRaisesRegex(ValueError, 'перед скачиванием'): http_resume.download('https://example.invalid/v.mp4', target)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_known_yandex_size_does_not_make_an_extra_head_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'video.mp4'
            def transfer(command, **kwargs):
                self.assertNotIn('--head', command)
                Path(command[command.index('--dump-header')+1]).write_text('HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n')
                Path(command[command.index('--output')+1]).write_bytes(b'abc')
                return NS(returncode=0)
            with patch.object(http_resume, 'preparation_run', side_effect=transfer) as transport:
                http_resume.download('https://example.invalid/signed', target, known_size=3)
                self.assertEqual(transport.call_count, 1)
            self.assertEqual(target.read_bytes(), b'abc')

    def test_yandex_known_size_blocks_before_video_download(self):
        import video_sources
        with tempfile.TemporaryDirectory() as tmp:
            def metadata(url, target, duration):
                target.write_text(json.dumps(dict(type='file', file='https://example.invalid/video', sha256='a'*64, size=1000)))
            with patch.object(video_sources, 'curl', side_effect=metadata) as request, patch.object(download_space.shutil, 'disk_usage', return_value=NS(free=10)):
                with self.assertRaisesRegex(ValueError, 'перед скачиванием'): video_sources.fetch_yandex('https://disk.yandex.ru/i/test', Path(tmp))
                self.assertEqual(request.call_count, 1)

    def test_site_size_and_merge_copy_block_before_download(self):
        import video_sources
        with tempfile.TemporaryDirectory() as tmp:
            metadata = NS(stdout=json.dumps(dict(requested_formats=[dict(filesize=1000), dict(filesize=200)])))
            with patch.object(video_sources, 'preparation_run', return_value=metadata), patch.object(video_sources, 'run_site_download') as transfer, patch.object(download_space.shutil, 'disk_usage', return_value=NS(free=download_space.RESERVE+1500)):
                with self.assertRaisesRegex(ValueError, 'перед скачиванием'): video_sources.fetch_site('https://example.invalid/watch', Path(tmp))
                transfer.assert_not_called()


class Summary(unittest.TestCase):
    def test_checked_quality_timing_and_readonly_storage(self):
        fixture = fixtures.ChatPreparationStatus(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture_args = fixture.f.args
        from preparation_status import record
        record(fixture.args('frames_review')); record(fixture.args('ready'))
        folder = fixture.f.lesson
        before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        value = summarize(folder)
        self.assertTrue(value['preparation']['teaching_ready'])
        self.assertTrue(value['material_quality']['coverage_complete'])
        self.assertFalse(value['material_quality']['meaning_verified'])
        self.assertEqual(value['storage']['logical_bytes'], sum(map(len, before.values())))
        self.assertTrue(value['storage']['scan_complete'])
        self.assertIsNone(value['cleanup']['safe_to_delete_bytes'])
        self.assertEqual(before, {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()})
        result = subprocess.run([sys.executable, str(fixtures.SCRIPTS/'course.py'), 'preparation-summary',
            '--course', str(fixture_args.course), '--lesson', fixture_args.lesson], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['schema'], 'preparation_summary_v1')

    def test_stage_times_accumulate_and_legacy_remains_unknown(self):
        from processing_status import Reporter
        with tempfile.TemporaryDirectory() as tmp:
            with patch('processing_status.time.monotonic', side_effect=[10, 10, 12, 15, 16]):
                report = Reporter(tmp); report.set('download'); report.set('worker'); report.set('download'); report.set('review', 'complete')
            self.assertEqual(report.data['stage_seconds'], dict(download=3, worker=3))
        fixture = fixtures.PreparationStatus(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        before = set(fixture.lesson.rglob('*'))
        value = summarize(fixture.lesson)
        self.assertIsNone(value['automatic_stage_seconds'])
        self.assertIsNone(value['material_quality'])
        self.assertEqual(before, set(fixture.lesson.rglob('*')))
