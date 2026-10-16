"""Manual transcription keeps raw OCR, source identity and checkpoint dependencies."""
import json,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_screen_ocr,screen_ocr as ocr,ocr_review as review,material_dependencies as md,course

class Review(unittest.TestCase):
    def setUp(self):self.f=test_screen_ocr.OCR();self.f.setUp();self.raw,_=self.f.save();self.key=self.raw['review_id'];self.file=self.f.f.directory/'review.json';self.card=dict(text='UUID.randomUUID();',notes='Synthetic QA transcription, not teacher attestation',uncertainties=['Surrounding code not reviewed']);self.file.write_text(json.dumps(self.card))
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return self.f.args(review_file=self.file,expected_revision=0,**kw)
    def save(self,revision=0):return ocr.view(self.f.args(review_file=self.file,expected_revision=revision))
    def test_review_is_separate_from_raw_and_grades(self):
        p=Path(self.raw['cache_file']);before=p.read_bytes();x=self.save();self.assertEqual(x['lines'],self.raw['lines']);self.assertEqual(x['review']['latest']['value'],self.card);self.assertEqual(p.read_bytes(),before)
        self.assertEqual(x['code_video_match'],'unverified');self.assertTrue(x['requires_source_review']);self.assertFalse((self.f.f.path/'learners').exists())
    def test_repeat_noop_and_stale_revision_rejected(self):
        x=self.save();self.assertEqual(self.save(1)['review']['revision'],1)
        with self.assertRaisesRegex(ValueError,'another chat'):self.save(0)
    def test_versions_preserved_and_dependency_changes(self):
        self.save();directory=self.f.f.lesson;chosen=dict(ocr_reviews=[self.key]);old=md.collect(directory,course.read(directory/'lesson.json'),chosen)
        self.card['uncertainties']=[];self.file.write_text(json.dumps(self.card));self.save(1);new=md.collect(directory,course.read(directory/'lesson.json'),chosen)
        self.assertNotEqual(old,new);_,data=review.load(directory,self.key);self.assertEqual(len(data['events']),2);self.assertEqual(data['events'][0]['value']['uncertainties'],['Surrounding code not reviewed'])
    def test_other_region_and_time_do_not_inherit_review(self):
        self.save();x=ocr.view(self.f.args(region=[0,0,.5,.5]));self.assertEqual(x['review']['revision'],0)
        y=ocr.view(self.f.args(frame_id=self.f.frames[1]['id']));self.assertEqual(y['review']['revision'],0)
    def test_no_candidate_and_run_combination_refused(self):
        with self.assertRaises(ValueError):ocr.view(self.f.args(frame_id=self.f.frames[1]['id'],review_file=self.file,expected_revision=0))
        with self.assertRaises(ValueError):ocr.view(self.f.args(run=True,review_file=self.file,expected_revision=0))
    def test_corrupt_review_preserved(self):
        self.save();p=self.f.f.lesson/'evidence/ocr'/(self.key+'.review.json');data=json.loads(p.read_text());data['events'][0]['value']['text']='wrong';p.write_text(json.dumps(data));before=p.read_bytes()
        with self.assertRaises(ValueError):ocr.view(self.f.args())
        with self.assertRaises(ValueError):self.save(1)
        self.assertEqual(before,p.read_bytes())
    def test_source_or_raw_damage_blocks_dependency(self):
        self.save();Path(self.f.frames[0]['absolute_path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):review.dependency(self.f.f.lesson,self.key)
    def test_corrupt_raw_blocks_review_even_with_valid_review_file(self):
        self.save();p=Path(self.raw['cache_file']);data=json.loads(p.read_text());data['lines'][0]['text']='changed';p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):review.dependency(self.f.f.lesson,self.key)
    def test_concurrent_review_same_revision_only_one(self):
        def write(_):
            try:self.save();return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sorted(pool.map(write,range(2))),[False,True])
    def test_bad_review_fields_do_not_publish(self):
        for patch_value in [dict(text=''),dict(notes=''),dict(uncertainties='unknown'),dict(uncertainties=['']),dict(extra=True)]:
            self.file.write_text(json.dumps(self.card|patch_value))
            with self.assertRaises(ValueError):self.save()
        self.assertFalse((self.f.f.lesson/'evidence/ocr'/(self.key+'.review.json')).exists())
    def test_missing_revision_and_disk_failure(self):
        with self.assertRaises(ValueError):ocr.view(self.f.args(review_file=self.file))
        self.save();p=self.f.f.lesson/'evidence/ocr'/(self.key+'.review.json');before=p.read_bytes();self.card['text']='new';self.file.write_text(json.dumps(self.card))
        with patch.object(review,'atomic_json',side_effect=OSError('QA disk full')),self.assertRaises(OSError):self.save(1)
        self.assertEqual(before,p.read_bytes())
    def test_missing_dependency_never_creates_review(self):
        with self.assertRaises(ValueError):review.dependency(self.f.f.lesson,self.key)
        with self.assertRaises(ValueError):review.dependency(self.f.f.lesson,'../wrong')

    def test_checkpoint_requires_review_after_change_without_losing_progress(self):
        import test_learning_tools,learning_tools as lt
        from lecture_context import assemble
        f=test_learning_tools.LearningTools();f.setUp()
        try:
            f.fixture.add('screen',0,60,frames=[(5,b'Synthetic checked frame identity')])
            frame=assemble(f.fixture.lesson,course.read(f.fixture.lesson/'lesson.json'),0,60)['frames'][0]
            args=f.args(start=0,window=60,frame_id=frame['id'],run=True,region=None)
            with patch.object(ocr,'recognize',return_value=[test_screen_ocr.LINE]):x=ocr.view(args)
            key=x['review_id'];file=f.write('review.json',self.card)
            ocr.view(f.args(start=0,window=60,frame_id=frame['id'],run=False,region=None,review_file=file,expected_revision=0))
            state=f.state()|dict(dependency_materials=dict(ocr_reviews=[key]));self.assertTrue(f.save_cursor(value=state)['valid'])
            path=f.fixture.path/'learners/default/cursor.json';before=path.read_bytes()
            file=f.write('review.json',self.card|dict(text='Corrected QA transcription'))
            ocr.view(f.args(start=0,window=60,frame_id=frame['id'],run=False,region=None,review_file=file,expected_revision=1))
            self.assertFalse(lt.cursor(f.a)['valid']);self.assertEqual(path.read_bytes(),before)
        finally:f.tearDown()
