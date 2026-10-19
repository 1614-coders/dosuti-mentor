"""Goal and plan decisions persist without conflating session tasks or assessments."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import io
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import learning_intent as li
import test_learning_tools


class LearningIntent(unittest.TestCase):
    def setUp(self):
        self.f=test_learning_tools.LearningTools(); self.f.setUp()
        self.path=li.path_for(self.f.a)
    def tearDown(self):self.f.tearDown()
    def read(self,**kw):return li.intent(self.f.args(**kw))
    def save(self,changes,revision=None,session=None,**kw):
        if revision is None:revision=self.read()['revision']
        return li.intent(self.f.args(update_file=self.f.write('intent-input.json',changes),
                                    expected_revision=revision,reason='QA user decision',session=session,**kw))
    def goal(self,text='Learn Java'):
        return self.save({'goal':{'description':text}})
    def propose(self):
        return self.save({'proposal':{'summary':'Small steps','steps':['Understand','Practice']}})
    def accept(self):return self.save({'accept_proposal':True})

    def test_old_profile_reads_unknown_without_creating_file(self):
        value=self.read(); self.assertTrue(value['valid']); self.assertEqual(value['revision'],0)
        self.assertIsNone(value['goal']); self.assertFalse(self.path.exists())

    def test_goal_history_reason_and_unknown_purpose(self):
        self.goal(); self.goal('Build a Java project')
        value=self.read(history_limit=100)
        self.assertIsNone(value['goal']['purpose']); self.assertEqual(value['event_count'],2)
        self.assertEqual(value['history'][0]['changes']['goal']['description'],'Learn Java')

    def test_proposal_does_not_replace_accepted_plan(self):
        self.goal(); self.propose(); self.assertIsNone(self.read()['plan']); self.accept()
        old=self.read()['plan']; self.save({'proposal':{'summary':'Another route','steps':['New']}})
        self.assertEqual(self.read()['plan'],old); self.assertEqual(self.read()['proposal']['summary'],'Another route')

    def test_accept_requires_existing_current_proposal(self):
        with self.assertRaises(ValueError):self.accept()
        self.goal(); self.propose(); self.goal('Another goal')
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.accept()
        self.assertEqual(self.path.read_bytes(),before)

    def test_goal_change_preserves_plan_and_marks_review(self):
        self.goal(); self.propose(); self.accept(); self.goal('Career')
        self.assertTrue(self.read()['plan']['needs_review']); self.assertIsNone(self.read()['proposal'])
        self.goal('Learn Java'); self.assertTrue(self.read()['plan']['needs_review'])

    def test_same_goal_preserves_plan(self):
        self.goal(); self.propose(); self.accept(); self.goal()
        self.assertFalse(self.read()['plan']['needs_review'])

    def test_session_tasks_are_independent_of_goal_and_each_other(self):
        self.goal(); self.save({'session':'Prepare for exam'},session='chat-a')
        self.save({'session':'Build project'},session='chat-b')
        self.assertEqual(self.read(session='chat-a')['session_task']['task'],'Prepare for exam')
        self.assertEqual(self.read(session='chat-b')['session_task']['task'],'Build project')
        self.assertEqual(self.read()['goal']['description'],'Learn Java')

    def test_new_goal_and_new_plan_mark_previous_task_for_review(self):
        self.goal(); self.save({'session':'Study'},session='a'); self.propose(); self.accept()
        self.assertTrue(self.read(session='a')['session_task']['needs_review'])
        self.save({'session':'Study under plan'},session='a'); self.assertFalse(self.read(session='a')['session_task']['needs_review'])
        self.goal('New goal'); self.assertTrue(self.read(session='a')['session_task']['needs_review'])
        self.save({'session':'New independent task'},session='a')
        self.assertIsNone(self.read(session='a')['session_task']['plan_revision'])
        self.assertFalse(self.read(session='a')['session_task']['needs_review'])

    def test_skip_unknown_goal_and_clear_session(self):
        self.save({'session':'Discuss lecture'},session='a'); self.assertIsNone(self.read()['goal'])
        self.save({'session':None},session='a'); self.assertIsNone(self.read(session='a')['session_task']['task'])

    def test_profile_and_course_isolation_and_portability(self):
        self.goal(); self.assertIsNone(self.read(profile='friend')['goal'])
        other=self.f.base/'other'
        with contextlib.redirect_stdout(io.StringIO()):course.init(self.f.args(course=other,title='Other'))
        self.assertIsNone(li.view(self.f.args(course=other))['goal'])
        moved=self.f.base/'moved'; shutil.move(self.f.a.course,moved); self.f.a.course=moved
        self.assertEqual(self.read()['goal']['description'],'Learn Java')

    def test_concurrent_updates_only_one_wins(self):
        p=self.f.write('shared-input.json',{'goal':{'description':'Java'}})
        def update(_):
            try:
                li.intent(self.f.args(update_file=p,expected_revision=0,reason='QA')); return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:result=list(pool.map(update,range(2)))
        self.assertEqual(sorted(result),[False,True]); self.assertEqual(self.read()['event_count'],1)

    def test_invalid_updates_do_not_overwrite(self):
        self.goal(); before=self.path.read_bytes()
        for value in [{},[],{'goal':{'description':''}},{'goal':{'description':'Java','level':'expert'}},
                      {'accept_proposal':1},{'accept_proposal':True,'goal':None},{'proposal':{'summary':'x','steps':[]}},
                      {'session':'Task'},{'unknown':True}]:
            with self.subTest(value=value),self.assertRaises(ValueError):self.save(value)
            self.assertEqual(self.path.read_bytes(),before)

    def test_corrupt_journal_preserved_and_blocks_write(self):
        self.goal(); good=course.read(self.path)
        variants=['{',dict(good,schema='future'),dict(good,revision=True)]
        bad=copy.deepcopy(good); bad['events'][0]['revision']=4; variants.append(bad)
        for value in variants:
            self.path.write_text(value if isinstance(value,str) else __import__('json').dumps(value))
            before=self.path.read_bytes(); self.assertFalse(self.read()['valid'])
            with self.assertRaises(ValueError):self.save({'goal':{'description':'Other'}},revision=1)
            self.assertEqual(self.path.read_bytes(),before)

    def test_symlink_and_hardlink_journal_rejected(self):
        import os
        self.goal(); target=self.f.base/'target'; self.path.rename(target)
        self.path.symlink_to(target); self.assertFalse(self.read()['valid']); self.path.unlink()
        os.link(target,self.path); self.assertFalse(self.read()['valid'])
        before=target.read_bytes()
        with self.assertRaises(ValueError):self.goal('Other')
        self.assertEqual(target.read_bytes(),before)

    def test_history_limit_rejects_write_without_truncation(self):
        self.goal(); before=self.path.read_bytes()
        with patch.object(li,'MAX_BYTES',50),self.assertRaises(ValueError):self.goal('Other')
        self.assertEqual(self.path.read_bytes(),before)

    def test_progress_and_preferences_unchanged(self):
        self.f.prefs(values=['style=partner'])
        with contextlib.redirect_stdout(io.StringIO()):course.progress(self.f.args(topic='QA',stage='explained',details='QA'))
        self.goal(); self.propose(); self.accept(); self.goal('Project')
        self.assertEqual(self.f.prefs()['effective']['style'],'partner')
        with course.db(self.f.a) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_context_contains_intent_and_selected_task(self):
        self.goal(); self.save({'session':'Task'},session='a')
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):course.context(self.f.args(time=5,window=10,session='a'))
        import json
        self.assertEqual(json.loads(stream.getvalue())['learning_intent']['session_task']['task'],'Task')


    def test_learning_context_is_optional_and_user_reported_only(self):
        self.assertEqual(self.read()['learning_context'],{})
        self.assertFalse(self.path.exists())
        self.save({'learning_context':{'context':'Taking a Java course',
                                      'self_assessment':'I think I understand loops'}})
        self.save({'learning_context':{'constraints':'Two evenings per week'}})
        value=self.read(history_limit=100)
        self.assertEqual(value['learning_context'],dict(context='Taking a Java course',
            self_assessment='I think I understand loops',constraints='Two evenings per week'))
        self.assertEqual(course.read(self.path)['schema'],li.CONTEXT_SCHEMA)
        self.assertIsNone(value['goal']); self.assertIsNone(value['plan'])
        self.assertFalse((self.path.parent/'unit-learning.json').exists())
        with course.db(self.f.a) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)

    def test_context_change_preserves_goal_history_and_preferences(self):
        self.f.prefs(values=['style=partner'])
        self.goal(); self.propose(); self.accept()
        self.save({'learning_context':{'self_assessment':'Beginner'}})
        self.goal('Build a project')
        self.propose()
        value=self.read(history_limit=100)
        self.assertEqual(value['learning_context'],{'self_assessment':'Beginner'})
        self.assertTrue(value['plan']['needs_review'])
        self.assertFalse(value['proposal']['needs_review'])
        self.assertEqual(value['history'][0]['changes']['goal']['description'],'Learn Java')
        self.assertEqual(self.f.prefs()['effective']['style'],'partner')
        self.assertEqual(course.read(self.path)['schema'],li.CONTEXT_SCHEMA)
        self.save({'learning_context':{'self_assessment':None}})
        self.assertEqual(self.read()['learning_context'],{})
        self.assertEqual(self.read(history_limit=100)['history'][3]['changes']
                         ['learning_context']['self_assessment'],'Beginner')

    def test_context_validation_and_conflict_preserve_journal(self):
        self.save({'learning_context':{'context':'Existing course'}})
        before=self.path.read_bytes()
        invalid=[{},None,[],{'context':''},{'context':42},{'context':'x'*4001},
                 {'verified':True},{'assessment':'confirmed'},{'style':'partner'}]
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.save({'learning_context':value})
            self.assertEqual(self.path.read_bytes(),before)
        with self.assertRaises(ValueError):
            self.save({'learning_context':{'self_assessment':'Expert'}},revision=0)
        self.assertEqual(self.path.read_bytes(),before)

    def test_old_intents_read_without_migration_and_upgrade_on_context_write(self):
        self.goal()
        original=course.read(self.path)
        for schema in (li.SCHEMA,li.CAREER_SCHEMA):
            journal=dict(original,schema=schema)
            self.path.write_text(__import__('json').dumps(journal))
            before=self.path.read_bytes()
            self.assertTrue(self.read()['valid'])
            self.assertEqual(self.read()['learning_context'],{})
            self.assertEqual(self.path.read_bytes(),before)
            self.save({'learning_context':{'self_assessment':'New to Java'}})
            current=course.read(self.path)
            self.assertEqual(current['schema'],li.CONTEXT_SCHEMA)
            self.assertEqual(current['events'][:1],journal['events'])
            current['schema']=schema
            self.path.write_text(__import__('json').dumps(current))
            self.assertFalse(self.read()['valid'])
