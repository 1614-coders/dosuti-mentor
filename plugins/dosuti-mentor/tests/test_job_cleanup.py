import contextlib,io,json,shutil,sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,job_cleanup,test_context,tutor_packet
from storage_lock import file_lock

class JobCleanup(unittest.TestCase):
    def setUp(self):
        self.f=test_context.ContextRegression();self.f.setUp();self.key='job1'
        self.pub=self.f.add('published',0,60,[(10,20,'test')],[(15,b'image')]);(self.pub/'audio.wav').write_bytes(b'audio')
        m=course.read(self.pub/'manifest.json');m['audio_sha256']=course.file_hash(self.pub/'audio.wav');course.save(self.pub/'manifest.json',m);tutor_packet.build_packet(self.pub)
        course.save(self.pub/'publication.json',dict(job_key=self.key,packet_sha256=course.file_hash(self.pub/'tutor_packet.json')))
        self.work=self.f.lesson/'.jobs'/self.key;shutil.copytree(self.pub,self.work)
        course.save(self.work/'checkpoint.json',dict(status='complete',source_sha256=course.file_hash(self.f.video)))
        (self.work/'visual_notes.json').write_text('keep this')
        p=self.f.lesson/'lesson.json';d=course.read(p);d['prepared_fragments'][0].update(job_key=self.key,packet_sha256=course.file_hash(self.pub/'tutor_packet.json'));d['processing_jobs']={'base':dict(job_key=self.key,status='complete')};course.save(p,d)
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return NS(**(vars(self.f.args)|dict(job_key=self.key,apply=False,expected_revision=None)|kw))
    def plan(self):return job_cleanup.cleanup_job(self.args())['plan']
    def data(self):return course.read(self.f.lesson/'lesson.json')
    def test_prune_only_matching_payload_preserves_publication_and_notes(self):
        p=self.plan();before=(self.pub/'audio.wav').read_bytes();meta=(self.f.lesson/'lesson.json').read_bytes()
        r=job_cleanup.cleanup_job(self.args(apply=True,expected_revision=p['revision']));self.assertTrue(r['applied'])
        self.assertFalse((self.work/'audio.wav').exists());self.assertFalse((self.work/'frames/0.jpg').exists())
        self.assertEqual((self.pub/'audio.wav').read_bytes(),before);self.assertEqual((self.work/'visual_notes.json').read_text(),'keep this');self.assertTrue((self.work/'.processing.lock').exists());self.assertEqual(meta,(self.f.lesson/'lesson.json').read_bytes())
        self.assertEqual(job_cleanup.cleaned_publication(self.work,self.f.lesson,self.data(),self.key),self.pub)
    def test_old_plan_rejected(self):
        p=self.plan();d=self.data();d['title']='changed';course.save(self.f.lesson/'lesson.json',d)
        with self.assertRaises(ValueError):job_cleanup.cleanup_job(self.args(apply=True,expected_revision=p['revision']))
        self.assertTrue((self.work/'audio.wav').exists())
    def test_damaged_publication_protected(self):
        (self.pub/'audio.wav').write_bytes(b'broken')
        with self.assertRaises(ValueError):self.plan()
    def test_pending_job_protected(self):
        d=self.data();d['processing_jobs']['base']['status']='pending';course.save(self.f.lesson/'lesson.json',d)
        with self.assertRaises(ValueError):self.plan()
    def test_referenced_payload_protected(self):
        d=self.data();d['future_path']=str(self.work/'frames');course.save(self.f.lesson/'lesson.json',d)
        with self.assertRaises(ValueError):self.plan()
    def test_busy_work_lock_protected(self):
        with file_lock(self.work/'.processing.lock'):
            with self.assertRaises(TimeoutError):self.plan()
    def test_symlink_payload_protected(self):
        p=self.work/'frames/0.jpg';p.unlink();p.symlink_to(self.pub/'frames/0.jpg')
        with self.assertRaises(ValueError):self.plan()
    def test_cleaned_job_without_publication_fails_closed(self):
        p=self.plan();job_cleanup.cleanup_job(self.args(apply=True,expected_revision=p['revision']));(self.pub/'audio.wav').write_bytes(b'bad')
        with self.assertRaises(ValueError):job_cleanup.cleaned_publication(self.work,self.f.lesson,self.data(),self.key)
    def test_incomplete_checkpoint_protected(self):
        course.save(self.work/'checkpoint.json',dict(status='pending'))
        with self.assertRaises(ValueError):self.plan()

    def test_other_lesson_reference_protected(self):
        other=self.f.path/'lessons/other';other.mkdir();course.save(other/'lesson.json',dict(video=dict(source=str(self.f.video)),future=str(self.work/'audio.wav')))
        with self.assertRaises(ValueError):self.plan()
    def test_partial_prune_receipt_allows_publication_reuse(self):
        p=self.plan();real=Path.unlink
        def interrupt(path,*args,**kw):
            if path.name=='0.jpg':raise OSError('simulated interruption')
            return real(path,*args,**kw)
        with patch.object(Path,'unlink',interrupt):
            with self.assertRaises(OSError):job_cleanup.cleanup_job(self.args(apply=True,expected_revision=p['revision']))
        self.assertTrue((self.work/'payload-cleaned.json').exists());self.assertFalse((self.work/'audio.wav').exists())
        self.assertEqual(job_cleanup.cleaned_publication(self.work,self.f.lesson,self.data(),self.key),self.pub)
