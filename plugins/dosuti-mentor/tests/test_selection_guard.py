"""Preparing a variant must not override an explicit choice, including a late worker."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import fragment_selection as fs
import learning_tools as lt
import test_context
import tutor_packet


class SelectionGuard(unittest.TestCase):
    def setUp(self):
        self.f=test_context.ContextRegression(); self.f.setUp()
        self.a=self.f.add('a',0,60,[(5,10,'Chosen text')])
        self.b=self.f.add('b',0,60,[(5,10,'Alternative text')])
    def tearDown(self): self.f.tearDown()
    def args(self,**kw): return NS(**(vars(self.f.args)|kw))
    def data(self): return course.read(self.f.lesson/'lesson.json')
    def edit(self,action):
        data=self.data(); action(data); course.save(self.f.lesson/'lesson.json',data)
    def select(self,keep):
        return fs.fragments(self.args(keep=keep,expected_revision=self.data().get('revision',0)))
    def prepare(self,source):
        course.prepare(self.args(import_prepared=source))
    def active(self): return [Path(f['folder']).name for f in self.data()['prepared_fragments']]
    def detached(self,name,start,end):
        p=self.f.add(name,start,end,[(start+1,start+2,'New material')])
        self.edit(lambda d:d['prepared_fragments'].pop())
        return p

    def test_later_import_is_candidate_and_can_be_selected(self):
        self.select(['a']); self.prepare(self.b)
        self.assertEqual(self.active(),['a'])
        reg=fs.fragments(self.args(keep=None)); candidate=next(r for r in reg['fragments'] if r['activation_reason']=='explicit_selection_overlap')
        self.assertFalse(candidate['active']); self.assertTrue(reg['selection_protection']['enabled'])
        self.assertEqual(self.f.context(0,20)['speech'][0]['text'],'Chosen text')
        self.select([candidate['id']]); self.assertEqual(self.f.context(0,20)['speech'][0]['text'],'Alternative text')
        self.assertNotIn('activation_reason',self.data()['prepared_fragments'][0])

    def test_prepare_started_before_selection_finishes_after_it(self):
        ready=threading.Event(); release=threading.Event(); original=tutor_packet.build_packet
        def blocked(folder,*args,**kw):
            result=original(folder,*args,**kw); ready.set()
            if not release.wait(10): raise RuntimeError('Selection test timed out')
            return result
        with patch.object(tutor_packet,'build_packet',blocked),ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.prepare,self.b)
            try:
                self.assertTrue(ready.wait(10)); self.select(['a'])
            finally: release.set()
            future.result(timeout=10)
        self.assertEqual(self.active(),['a'])
        self.assertEqual(len(self.data()['retired_fragments']),2)

    def test_new_disjoint_range_activates_but_does_not_extend_protection(self):
        tail=self.detached('tail',60,90); self.select(['a']); self.prepare(tail)
        self.assertEqual(len(self.active()),2)
        self.assertTrue(self.f.context(0,90)['coverage']['complete'])
        self.assertEqual(fs.protected_ranges(self.data()),[(0,60)])

    def test_mixed_protected_new_range_is_whole_candidate(self):
        mixed=self.detached('mixed',40,90); self.select(['a']); self.prepare(mixed)
        self.assertEqual(self.active(),['a']); self.assertFalse(self.f.context(0,90)['coverage']['complete'])
        candidate=next(f for f in self.data()['retired_fragments'] if f.get('activation_reason'))
        self.select(['a',Path(candidate['folder']).name]); self.assertTrue(self.f.context(0,90)['coverage']['complete'])

    def test_no_explicit_selection_keeps_legacy_activation(self):
        self.prepare(self.b); self.assertEqual(len(self.active()),3)
        self.assertFalse(fs.fragments(self.args(keep=None))['selection_protection']['enabled'])

    def test_old_selection_history_protects_current_active_set(self):
        self.select(['a']); self.edit(lambda d:d.pop('fragment_selection_guard'))
        self.prepare(self.b); self.assertEqual(self.active(),['a'])
        self.assertEqual(fs.protected_ranges(self.data()),[(0,60)])

    def test_corrupt_guard_fails_closed(self):
        self.select(['a']); self.edit(lambda d:d['fragment_selection_guard'].update(schema='future'))
        with self.assertRaisesRegex(ValueError,'guard'): self.prepare(self.b)
        self.assertEqual(self.active(),['a'])

    def test_guard_for_different_source_fails_closed(self):
        self.select(['a']); self.edit(lambda d:d['fragment_selection_guard'].update(source_sha256='0'*64))
        with self.assertRaisesRegex(ValueError,'guard'): self.prepare(self.b)
        self.assertEqual(self.active(),['a'])

    def test_cached_selected_target_stays_active(self):
        self.select(['a']); data=self.data()
        self.assertEqual(fs.defer_to_selection(data,data['prepared_fragments'][0]),'active')
        self.assertEqual([Path(f['folder']).name for f in data['prepared_fragments']],['a'])

    def test_repeated_candidate_registration_does_not_duplicate_id(self):
        self.select(['a']); data=self.data(); candidate=dict(data['retired_fragments'][0])
        fs.defer_to_selection(data,candidate); fs.defer_to_selection(data,candidate)
        self.assertEqual(len(fs.registry(data)),2)

    def test_protection_moves_with_course(self):
        import shutil
        self.select(['a']); new=self.f.directory/'moved'; shutil.move(self.f.path,new)
        self.f.path=new; self.f.args.course=new; self.f.lesson=new/'lessons/lesson-01'
        self.prepare(self.f.lesson/'prepared/b'); self.assertEqual(self.active(),['a'])

    def test_candidate_does_not_invalidate_topic_cursor(self):
        import test_learning_tools
        f=test_learning_tools.LearningTools(); f.setUp()
        try:
            f.add_topic(); fs.fragments(f.args(keep=['a'],expected_revision=course.read(f.fixture.lesson/'lesson.json').get('revision',0)))
            f.save_cursor(value=f.state()|{'dependency_topics':['maps']})
            course.prepare(f.args(import_prepared=f.fragment))
            self.assertTrue(lt.cursor(f.a)['valid'])
            with course.db(f.a) as db:self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],0)
        finally:f.tearDown()
