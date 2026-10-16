"""Shared attempts, separate checkpoints, source revisions and actual answer constraints."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import study_units
import unit_learning as ul
import test_study_units


class UnitLearning(unittest.TestCase):
    def setUp(self):
        self.f=test_study_units.StudyUnits();self.f.setUp();self.f.save()
        self.path=self.f.f.path/'learners/default/unit-learning.json'
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return self.f.args(unit_id='maps',**kw)
    def read(self,**kw):return ul.learn_unit(self.args(**kw))
    def event(self,**kw):
        return dict(stage='solved_independently',details='Checked answer',question='What does a map store?',answer='Keys and values',hints=0,criteria=[1])|kw
    def save(self,value=None,checkpoint=False,revision=None,checksum=None,session='a',**kw):
        state=self.read(**kw);p=self.f.f.directory/'learning-input.json';p.write_text(json.dumps(value or self.event()))
        return ul.learn_unit(self.args(**{'checkpoint_file' if checkpoint else 'event_file':p},
                  expected_revision=state['learning']['revision'] if revision is None else revision,
                  expected_unit_sha256=state['unit']['content_sha256'] if checksum is None else checksum,session=session,**kw))

    def test_read_old_profile_does_not_create_learning_file(self):
        x=self.read();self.assertTrue(x['learning']['valid']);self.assertEqual(x['learning']['attempt_count'],0)
        self.assertFalse(x['learning']['all_criteria_demonstrated']);self.assertFalse(self.path.exists())
    def test_explained_is_not_independent_result(self):
        x=self.save(self.event(stage='explained',answer=None,question=None,criteria=[]))
        self.assertFalse(x['learning']['all_criteria_demonstrated']);self.assertEqual(x['learning']['criteria']['1'],'not_assessed')
        self.assertEqual(x['learning']['attempt_count'],0);self.assertEqual(x['learning']['explanation_count'],1)
    def test_assisted_and_independent_answers_differ(self):
        x=self.save(self.event(stage='practice_with_help',hints=1));self.assertFalse(x['learning']['all_criteria_demonstrated'])
        x=self.save();self.assertTrue(x['learning']['all_criteria_demonstrated']);self.assertEqual(x['learning']['attempt_count'],2)
    def test_each_criterion_requires_independent_evidence(self):
        self.f.save(self.f.card(criteria=['Explain','Implement']))
        self.assertFalse(self.save()['learning']['all_criteria_demonstrated'])
        self.assertTrue(self.save(self.event(criteria=[2]))['learning']['all_criteria_demonstrated'])
        self.assertFalse(self.save(self.event(stage='needs_review',criteria=[1]))['learning']['all_criteria_demonstrated'])
    def test_invalid_attempts_do_not_overwrite(self):
        self.save();before=self.path.read_bytes()
        for patch in [dict(answer=None),dict(question=None),dict(hints=1),dict(criteria=[]),dict(criteria=[2]),dict(criteria=[True]),dict(stage=[]),dict(stage='practice_with_help',hints=0)]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):self.save(self.event(**patch))
            self.assertEqual(self.path.read_bytes(),before)
    def test_unit_change_preserves_history_but_not_current_assessment(self):
        self.save();old=self.read()['unit']['content_sha256'];self.f.save(self.f.card(summary='Updated'))
        x=self.read(history_limit=100)['learning'];self.assertEqual(x['outdated_attempt_count'],1)
        self.assertFalse(x['all_criteria_demonstrated']);self.assertEqual(len(x['history']),1)
        with self.assertRaisesRegex(ValueError,'changed'):self.save(checksum=old)
    def test_source_damage_blocks_new_record_and_hides_context(self):
        self.save();source=self.read()['unit']['sources'][0];p=self.f.f.path/source['snapshot'];p.chmod(0o600);p.write_text('changed')
        x=self.read();self.assertTrue(x['unit']['stale']);self.assertEqual(x['unit']['source_contexts'],[])
        self.assertFalse(x['learning']['all_criteria_demonstrated'])
        with self.assertRaises(ValueError):self.save()
    def test_session_checkpoints_independent_assessments_shared(self):
        a=dict(recap='A',next_step='Next A',pending_question='Why A?');b=dict(recap='B',next_step='Next B',pending_question=None)
        self.save(a,checkpoint=True,session='a');self.save(b,checkpoint=True,session='b');self.save()
        x=self.read(session='a')['learning'];y=self.read(session='b')['learning']
        self.assertEqual(x['checkpoint']['event']['value'],a);self.assertEqual(y['checkpoint']['event']['value'],b)
        self.assertTrue(x['all_criteria_demonstrated']);self.assertTrue(y['all_criteria_demonstrated'])
    def test_checkpoint_invalidates_after_unit_change(self):
        self.save(dict(recap='A',next_step='B',pending_question=None),checkpoint=True)
        self.f.save(self.f.card(summary='Updated'))
        self.assertFalse(self.read(session='a')['learning']['checkpoint']['valid'])
    def test_checkpoint_requires_explicit_session(self):
        with self.assertRaises(ValueError):self.save(dict(recap='A',next_step='B',pending_question=None),checkpoint=True,session=None)
    def test_profiles_isolated(self):
        self.save();self.assertFalse(self.read(profile='friend')['learning']['all_criteria_demonstrated'])
    def test_cas_conflict_preserves_attempts(self):
        self.save();before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'another chat'):self.save(revision=0)
        self.assertEqual(self.path.read_bytes(),before)
    def test_concurrent_records_one_revision(self):
        p=self.f.f.directory/'common.json';p.write_text(json.dumps(self.event()));sha=self.read()['unit']['content_sha256']
        def write(_):
            try:ul.learn_unit(self.args(event_file=p,expected_revision=0,expected_unit_sha256=sha));return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:result=list(pool.map(write,range(2)))
        self.assertEqual(sorted(result),[False,True]);self.assertEqual(self.read()['learning']['attempt_count'],1)
    def test_corrupt_journal_not_overwritten(self):
        self.save();original=json.loads(self.path.read_text())
        for mutate in [lambda d:d.update(schema='future'),lambda d:d['events'][0].update(unit_sha256='bad'),lambda d:d['events'][0]['value'].update(answer=None)]:
            bad=copy.deepcopy(original);mutate(bad);self.path.write_text(json.dumps(bad));before=self.path.read_bytes()
            self.assertFalse(self.read()['learning']['valid'])
            with self.assertRaises(ValueError):self.save(revision=1)
            self.assertEqual(self.path.read_bytes(),before)
    def test_existing_lecture_progress_not_merged_or_removed(self):
        with contextlib.redirect_stdout(io.StringIO()):course.progress(self.f.args(topic='QA',stage='explained',details='Old event'))
        self.save()
        with course.db(self.f.f.args) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)
    def test_video_and_text_use_same_learning_api(self):
        self.save();self.f.f.add('a',0,60,[(5,10,'Maps')])
        self.f.save(self.f.card(sources=[dict(kind='video',role='teacher',notes='Reviewed',lesson='lesson-01',start=0,end=20)]))
        x=self.save();self.assertTrue(x['learning']['all_criteria_demonstrated'])
        self.assertEqual(x['learning']['outdated_attempt_count'],1)
    def test_move_preserves_learning_and_checkpoint(self):
        self.save();self.save(dict(recap='A',next_step='B',pending_question=None),checkpoint=True)
        new=self.f.f.directory/'moved';shutil.move(self.f.f.path,new);self.f.f.path=new;self.f.f.args.course=new
        x=self.read(session='a')['learning'];self.assertTrue(x['checkpoint']['valid']);self.assertTrue(x['all_criteria_demonstrated'])
