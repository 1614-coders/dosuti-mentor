"""Request coordination must avoid duplicated work without weakening evidence checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import screen_ocr as ocr
import test_screen_ocr as fixture
import course


class Requests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.OCR();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def candidate_files(self):return list((self.f.f.lesson/'evidence/ocr').glob('*.json'))
    def test_four_simultaneous_requests_recognize_once(self):
        barrier=threading.Barrier(4);original=ocr.file_lock
        @contextmanager
        def coordinated(path,timeout=30):
            if Path(path).name.startswith('.ocr-'):barrier.wait(timeout=10)
            with original(path,timeout=timeout):yield
        with patch.object(ocr,'file_lock',coordinated), patch.object(ocr,'recognize',return_value=[copy.deepcopy(fixture.LINE)]) as backend:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(lambda _:ocr.view(self.f.args(run=True)),range(4)))
        self.assertEqual(backend.call_count,1)
        self.assertTrue(all(r==results[0] for r in results))
        self.assertEqual(len(self.candidate_files()),1)
        self.assertEqual(self.f.before,(self.f.f.lesson/'lesson.json').read_bytes())
        self.assertFalse((self.f.f.path/'learners').exists())
    def test_regions_have_separate_requests(self):
        with patch.object(ocr,'recognize',return_value=[]) as backend:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(ocr.view,self.f.args(run=True,region=r)) for r in (None,[0,0,.5,.5])]
                results=[f.result(timeout=10) for f in futures]
        self.assertEqual(backend.call_count,2)
        self.assertNotEqual(results[0]['review_id'],results[1]['review_id'])
    def test_failure_releases_request_for_retry(self):
        with patch.object(ocr,'recognize',side_effect=[RuntimeError('Unavailable'),[]]) as backend:
            with self.assertRaisesRegex(RuntimeError,'Unavailable'):ocr.view(self.f.args(run=True))
            self.assertFalse(self.candidate_files())
            self.assertEqual(ocr.view(self.f.args(run=True))['lines'],[])
        self.assertEqual(backend.call_count,2)
    def test_invalid_backend_output_never_published(self):
        with patch.object(ocr,'recognize',return_value=[dict(fixture.LINE,confidence=2)]):
            with self.assertRaises(ValueError):ocr.view(self.f.args(run=True))
        self.assertFalse(self.candidate_files())
    def test_engine_change_never_published(self):
        before=ocr.engine();current=dict(before)
        def run(*_):
            current['os_version']='changed during OCR';return []
        with patch.object(ocr,'engine',side_effect=lambda:dict(current)),patch.object(ocr,'recognize',side_effect=run):
            with self.assertRaisesRegex(ValueError,'engine changed'):ocr.view(self.f.args(run=True))
        self.assertFalse(self.candidate_files())
    def test_waiting_request_revalidates_source_before_backend(self):
        original=ocr.file_lock
        @contextmanager
        def changed(path,timeout=30):
            with original(path,timeout=timeout):
                data=course.read(self.f.f.lesson/'lesson.json');data['prepared_fragments']=[]
                course.save(self.f.f.lesson/'lesson.json',data)
                yield
        with patch.object(ocr,'file_lock',changed),patch.object(ocr,'recognize',side_effect=AssertionError('No inference')):
            with self.assertRaises(ValueError):ocr.view(self.f.args(run=True))
        self.assertFalse(self.candidate_files())
    def test_linked_request_lock_refused(self):
        _,_,binding=ocr.select(self.f.args());key=ocr.signature(dict(binding=binding,region=None,engine=ocr.engine()))
        target=self.f.f.directory/'keep';target.write_text('keep')
        (self.f.f.lesson/('.ocr-'+key+'.lock')).symlink_to(target)
        with patch.object(ocr,'recognize',side_effect=AssertionError('No inference')):
            with self.assertRaises(ValueError):ocr.view(self.f.args(run=True))
        self.assertEqual(target.read_text(),'keep')
    def test_busy_request_does_not_infer_or_publish(self):
        with patch.object(ocr,'file_lock',side_effect=TimeoutError('Busy')),patch.object(ocr,'recognize',side_effect=AssertionError('No inference')):
            with self.assertRaises(TimeoutError):ocr.view(self.f.args(run=True))
        self.assertFalse(self.candidate_files())
