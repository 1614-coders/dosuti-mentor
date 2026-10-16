"""OCR is a bounded, explicit hint tied to verified evidence, never a code grade."""
import copy,io,json,sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import screen_ocr as ocr
import test_context,course
from PIL import Image

LINE=dict(text='UUID.randomUUIDO);',confidence=1.0,box=dict(x=.1,y=.2,width=.3,height=.1))

class OCR(unittest.TestCase):
    def setUp(self):
        self.f=test_context.ContextRegression();self.f.setUp();buffer=io.BytesIO();Image.new('RGB',(640,360),'white').save(buffer,format='PNG')
        self.folder=self.f.add('one',0,30,frames=[(5,buffer.getvalue()),(20,buffer.getvalue())])
        from lecture_context import assemble
        self.frames=assemble(self.f.lesson,course.read(self.f.lesson/'lesson.json'),0,30)['frames']
        self.before=(self.f.lesson/'lesson.json').read_bytes()
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return NS(**(vars(self.f.args)|dict(start=0,window=30,frame_id=self.frames[0]['id'],region=None,run=False)|kw))
    def save(self,**kw):
        with patch.object(ocr,'recognize',return_value=[copy.deepcopy(LINE)]) as backend:return ocr.view(self.args(run=True,**kw)),backend.call_count
    def test_read_is_optional_no_folder_or_backend(self):
        with patch.object(ocr,'recognize',side_effect=AssertionError('Unexpected run')):
            x=ocr.view(self.args());self.assertEqual(x['status'],'not_prepared');self.assertFalse((self.f.lesson/'evidence/ocr').exists())
    def test_cache_pins_time_hash_and_never_verifies_code_or_progress(self):
        x,calls=self.save();self.assertEqual(calls,1);self.assertEqual(x['binding']['frame_seconds'],5);self.assertEqual(x['binding']['frame_sha256'],self.frames[0]['sha256'])
        self.assertEqual(x['code_video_match'],'unverified');self.assertTrue(x['requires_source_review']);self.assertEqual(x['lines'][0]['confidence'],1)
        self.assertFalse((self.f.path/'learners').exists());self.assertEqual(self.before,(self.f.lesson/'lesson.json').read_bytes())
    def test_cached_read_never_calls_engine_even_with_run(self):
        x,_=self.save()
        with patch.object(ocr,'recognize',side_effect=AssertionError('Cache should be reused')):
            self.assertEqual(ocr.view(self.args(run=True)),x)
    def test_same_pixels_different_time_do_not_share_cache(self):
        x,_=self.save();y=ocr.view(self.args(frame_id=self.frames[1]['id']));self.assertEqual(y['status'],'not_prepared');self.assertEqual(y['binding']['frame_seconds'],20)
    def test_region_and_engine_changes_require_own_cache(self):
        self.save();self.assertEqual(ocr.view(self.args(region=[0,0,.5,.5]))['status'],'not_prepared')
        real=ocr.engine()
        with patch.object(ocr,'engine',return_value=real|dict(os_version='QA changed')):self.assertEqual(ocr.view(self.args())['status'],'not_prepared')
    def test_corrupt_cache_not_silently_overwritten(self):
        x,_=self.save();p=Path(x['cache_file']);d=json.loads(p.read_text());d['lines'][0]['text']='changed';p.write_text(json.dumps(d));before=p.read_bytes()
        with patch.object(ocr,'recognize',side_effect=AssertionError('No repair')):
            with self.assertRaises(ValueError):ocr.view(self.args(run=True))
        self.assertEqual(before,p.read_bytes())
    def test_frame_damage_blocks_cached_result(self):
        self.save();p=Path(self.frames[0]['absolute_path']);p.write_bytes(b'changed')
        with self.assertRaises(ValueError):ocr.view(self.args())
    def test_retired_frame_cannot_use_old_hint(self):
        self.save();d=course.read(self.f.lesson/'lesson.json');d['prepared_fragments']=[];course.save(self.f.lesson/'lesson.json',d)
        with self.assertRaises(ValueError):ocr.view(self.args())
    def test_source_replaced_during_recognition_does_not_publish(self):
        def change(raw,roi):
            d=course.read(self.f.lesson/'lesson.json');d['prepared_fragments']=[];course.save(self.f.lesson/'lesson.json',d);return [LINE]
        with patch.object(ocr,'recognize',side_effect=change),self.assertRaises(ValueError):ocr.view(self.args(run=True))
        self.assertFalse((self.f.lesson/'evidence/ocr').exists())
    def test_unavailable_or_timeout_never_creates_candidate(self):
        for error in [RuntimeError('Vision unavailable'),TimeoutError('QA timed out')]:
            with patch.object(ocr,'recognize',side_effect=error),self.assertRaises(type(error)):ocr.view(self.args(run=True))
            self.assertFalse((self.f.lesson/'evidence/ocr').exists())
    def test_region_rejects_invalid_and_unknown_id(self):
        for roi in [[0,0,0,1],[True,0,1,1],[0,0,float('nan'),1],[.9,0,.2,1],[],[0,0,1,2]]:
            with self.subTest(roi=roi),self.assertRaises(ValueError):ocr.view(self.args(region=roi))
        with self.assertRaises(ValueError):ocr.view(self.args(frame_id='../../outside'))
        with self.assertRaises(ValueError):ocr.view(self.args(window=301))
    def test_invalid_observations_rejected(self):
        for value in [[],None,'text',dict(LINE,confidence=float('nan')),dict(LINE,confidence=2),dict(LINE,text=''),dict(LINE,box=dict(x=0,y=0,width=2,height=1))]:
            lines=value if value in ([],None,'text') else [value]
            if lines==[]:self.assertEqual(ocr.observations(lines),[]);continue
            with self.assertRaises(ValueError):ocr.observations(lines)
    def test_non_macos_explicit_failure_without_download(self):
        with patch('screen_ocr.platform.system',return_value='Linux'),self.assertRaisesRegex(RuntimeError,'unavailable'):ocr.recognize(b'image',None)
    def test_preceding_sample_uses_actual_frame_time(self):
        x,_=self.save();y=ocr.view(self.args(start=10,window=5));self.assertEqual(y['binding']['frame_seconds'],5);self.assertEqual(y['frame_selection'],'preceding_sample')
