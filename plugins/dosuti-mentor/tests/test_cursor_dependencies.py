"""Explicit checkpoint dependencies preserve only evidence actually selected by the caller."""
from concurrent.futures import ThreadPoolExecutor
import copy
import shutil
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import learning_tools as lt
import test_learning_tools


class CursorDependencies(unittest.TestCase):
    def setUp(self):
        self.f = test_learning_tools.LearningTools(); self.f.setUp(); self.f.add_topic()
        self.path = self.f.fixture.lesson/'lesson.json'

    def tearDown(self): self.f.tearDown()

    def value(self, **kw): return self.f.state() | {'dependency_topics': ['maps']} | kw

    def save(self, **kw): return self.f.save_cursor(value=self.value(**kw))

    def cursor(self): return lt.cursor(self.f.a)

    def edit(self, action):
        data = course.read(self.path); action(data); course.save(self.path, data)

    def test_unrelated_packet_and_topic_preserve(self):
        self.save(); self.f.fixture.add('later',60,90,[(65,70,'Later')])
        self.f.add_topic(self.f.card() | {'id':'later','start':65,'end':70})
        self.assertTrue(self.cursor()['valid'])

    def test_unrelated_corrupt_packet_and_topic_preserve(self):
        self.save(); folder=self.f.fixture.add('later',60,90,[(65,70,'Later')])
        (folder/'tutor_packet.json').write_text('{}')
        self.edit(lambda d: d['topics'].update(bad={'start': []}))
        self.assertTrue(self.cursor()['valid'])
        self.assertTrue(self.f.save_cursor(revision=1,value=self.value())['valid'])

    def test_overlap_invalidates_and_stale_save_rejected(self):
        self.save(); self.f.fixture.add('overlap',8,12,[(9,11,'New')])
        self.assertFalse(self.cursor()['valid'])
        with self.assertRaisesRegex(ValueError,'Stale'): self.f.save_cursor(revision=1,value=self.value())

    def test_semantic_fields_and_removal_invalidate(self):
        self.save(); original=course.read(self.path)
        for key, value in [('title','New'),('summary','New'),('keywords',['new']),('start',4),('end',16),
                           ('boundary_review',dict(start='unknown',end='complete',notes='review'))]:
            with self.subTest(key=key):
                course.save(self.path,copy.deepcopy(original))
                self.edit(lambda d: d['topics']['maps'].update({key:value}))
                self.assertFalse(self.cursor()['valid'])
        course.save(self.path, original)
        self.edit(lambda d: d['topics'].pop('maps')); self.assertFalse(self.cursor()['valid'])

    def test_timestamp_and_derived_fields_ignored(self):
        self.save(); self.edit(lambda d: d['topics']['maps'].update(updated='later',stale=True,display_label='new'))
        self.assertTrue(self.cursor()['valid'])

    def test_multiple_dependencies_all_bound_and_inclusive_end(self):
        self.f.add_topic(self.f.card() | {'id':'objects','start':20,'end':30})
        self.assertTrue(self.save(dependency_topics=['maps','objects'],time=15)['valid'])
        self.edit(lambda d: d['topics']['objects'].update(summary='Changed other recap topic'))
        self.assertFalse(self.cursor()['valid'])

    def test_invalid_input_rejected_without_overwrite(self):
        self.save(); path=lt.learner(self.f.a)/'cursor.json'; before=path.read_bytes()
        for names in [None,[],['maps','maps'],['missing'],['../maps'],[1],['maps']*51,'maps',{}]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                self.f.save_cursor(revision=1,value=self.value(dependency_topics=names))
            self.assertEqual(path.read_bytes(),before)
        with self.assertRaises(ValueError): self.f.save_cursor(revision=1,value=self.value(time=40))

    def test_malformed_stored_schema_preserved(self):
        self.save(); path=lt.learner(self.f.a)/'cursor.json'; original=course.read(path)
        mutations=[lambda s:s.update(dependency_topics=[]),lambda s:s.pop('dependency_evidence'),
                   lambda s:s['dependency_evidence'].update(schema='future'),
                   lambda s:s['dependency_evidence'].update(topics=[None]),
                   lambda s:s['dependency_evidence']['topics'][0].update(code_sha256=[]),
                   lambda s:s['dependency_evidence']['topics'][0].update(packet_hashes=[])]
        for mutate in mutations:
            data=copy.deepcopy(original); mutate(data['state']); course.save(path,data); before=path.read_bytes()
            self.assertEqual(self.cursor()['reason'],'invalid_checkpoint')
            with self.assertRaisesRegex(ValueError,'Invalid checkpoint'):
                self.f.save_cursor(revision=1,value=self.value())
            self.assertEqual(path.read_bytes(),before)

    def test_legacy_remains_whole_lesson(self):
        result=self.f.save_cursor(); self.assertNotIn('dependency_topics',result['state'])
        self.f.fixture.add('later',60,90,[(65,70,'Later')]); self.assertFalse(self.cursor()['valid'])

    def test_video_change_invalidates(self):
        self.save(); self.f.fixture.video.write_bytes(b'changed'); self.assertFalse(self.cursor()['valid'])

    def test_selected_packet_tampering_invalidates(self):
        self.save(); (self.f.fragment/'transcript.json').write_text('[]'); self.assertFalse(self.cursor()['valid'])

    def test_code_only_bound_for_topics_using_code(self):
        self.f.attach_code('lesson-01',{'A.java':'class A {}'}); self.save()
        self.f.attach_code('lesson-01',{'A.java':'class A { int x; }'})
        self.assertTrue(self.cursor()['valid'])
        self.f.add_topic(self.f.card() | {'code_files':['A.java']})
        self.assertTrue(self.f.save_cursor(revision=1,value=self.value())['valid'])
        self.f.attach_code('lesson-01',{'A.java':'class A { int y; }'})
        self.assertFalse(self.cursor()['valid'])

    def test_relocation_and_profile_isolation(self):
        self.save(); moved=self.f.base/'moved'; shutil.move(self.f.a.course,moved); self.f.a.course=moved
        self.assertTrue(self.cursor()['valid']); self.assertIsNone(lt.cursor(self.f.args(profile='bob'))['state'])
        with course.db(self.f.a) as db: self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],0)

    def test_revision_cas(self):
        path=self.f.write('input.json',self.value())
        def save(_):
            try: lt.resume(self.f.args(state_file=path,expected_revision=0)); return True
            except ValueError: return False
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(save,range(2)))
        self.assertEqual(sorted(results),[False,True]); self.assertEqual(self.cursor()['revision'],1)
