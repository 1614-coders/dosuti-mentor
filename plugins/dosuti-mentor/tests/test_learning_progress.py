"""Unified progress must preserve independent history and ambiguous continuations."""
import contextlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import learning_intent as intent
import learning_tools as tools
import test_unit_learning


class LearningProgress(unittest.TestCase):
    def setUp(self):
        self.f = test_unit_learning.UnitLearning(); self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def args(self, **kw): return self.f.f.args(**kw)
    def read(self, **kw): return intent.view(self.args(**kw))['learning_progress']
    def change(self, changes):
        intent.commit(self.args(expected_revision=intent.view(self.args())['revision'], reason='QA decision'), changes)
    def files(self):
        return {str(p): p.read_bytes() for p in self.f.f.f.path.rglob('*') if p.is_file()}
    def legacy(self, stage='explained'):
        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(self.args(topic='Legacy maps', stage=stage, details='Recorded lesson result'))

    def test_unit_history_visible_without_plan_and_after_goal_change(self):
        self.f.save()
        self.assertEqual(self.read()['demonstrated_units'], ['maps'])
        self.change({'goal': {'description': 'Learn maps'}})
        self.change({'proposal': {'summary': 'Maps', 'steps': ['Maps'], 'step_bases': [{'unit_ids': ['maps']}]}})
        self.change({'accept_proposal': True})
        self.change({'goal': {'description': 'Build another project'}})
        value = self.read()
        self.assertEqual(value['studied_units'], ['maps'])
        self.assertEqual(value['demonstrated_units'], ['maps'])
        self.assertIsNone(value['next_step'])
        self.assertEqual(len(value['unit_history']), 1)

    def test_legacy_history_explanation_and_tasks_are_visible_read_only(self):
        self.legacy()
        first = self.read()
        self.assertEqual(first['studied_lessons'][0]['topic'], 'Legacy maps')
        self.assertEqual(first['confirmed_lesson_knowledge'], [])
        self.legacy('practice_checked')
        before = self.files()
        value = self.read(session='another-chat')
        self.assertEqual(len(value['lesson_history']), 2)
        self.assertEqual(value['completed_lesson_tasks'][0]['stage'], 'practice_checked')
        self.assertEqual(self.files(), before)

    def test_completed_independent_later_step_not_lost_and_dependencies_separate(self):
        self.f.save()
        self.change({'goal': {'description': 'Learn maps'}})
        # First unit is a distinct, unassessed topic.
        self.f.f.save(self.f.f.card('unassessed'))
        self.change({'proposal': {'summary': 'Independent steps', 'steps': ['Unfinished', 'Completed', 'Blocked'],
                                 'step_bases': [{'unit_ids': ['unassessed']}, {'unit_ids': ['maps']}, {'prerequisites': [1]}]}})
        self.change({'accept_proposal': True})
        value = self.read()
        self.assertEqual(value['completed_steps'], [2])
        self.assertEqual(value['next_step']['index'], 1)
        self.assertEqual(value['blocked_steps'], [3])

    def test_all_continuations_visible_no_arbitrary_choice_across_chats(self):
        self.f.f.f.add('qa', 0, 60, [(5, 15, 'Map examples')])
        path = self.f.f.f.directory / 'cursor.json'
        path.write_text(json.dumps(dict(lesson='lesson-01', time=10, topic='Maps', recap='Read example',
                                        next_step='Practice', pending_question='Why?', misconceptions=[])))
        tools.resume(self.args(state_file=path, expected_revision=0))
        self.f.save(dict(recap='Unit A', next_step='Practice A', pending_question=None), checkpoint=True, session='a')
        self.f.save(dict(recap='Unit B', next_step='Practice B', pending_question=None), checkpoint=True, session='b')
        before = self.files()
        value = self.read(session='a')
        self.assertEqual(len(value['continuations']), 3)
        self.assertIsNone(value['stopped_at'])
        self.assertTrue(value['continuation_choice_required'])
        self.assertEqual({c.get('session') for c in value['continuations'] if c['kind'] == 'unit'}, {'a', 'b'})
        self.assertEqual(self.files(), before)

    def test_read_empty_progress_creates_no_database(self):
        before = self.files()
        self.read()
        self.assertEqual(self.files(), before)

    def test_read_status_resume_and_unit_share_progress_without_writes(self):
        self.legacy()
        self.f.save()
        before=self.files()
        expected=self.read(session='a')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):course.status(self.args(session='a'))
        self.assertEqual(json.loads(output.getvalue())['learning_progress'],expected)
        self.assertEqual(tools.resume(self.args(session='a'))['learning_progress'],expected)
        self.assertEqual(self.f.read(session='a')['learning_progress'],expected)
        self.assertEqual(self.files(),before)

    def test_old_sqlite_columns_read_without_schema_migration(self):
        path=self.f.f.f.path/'learners/default/progress.sqlite3'
        path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE progress (lesson TEXT,topic TEXT,stage TEXT,details TEXT,updated TEXT)')
            connection.execute("INSERT INTO progress VALUES ('lesson-01','Old maps','explained','Old explanation','2026-01-01')")
        before=self.files()
        self.assertEqual(self.read()['studied_lessons'][0]['topic'],'Old maps')
        self.assertEqual(self.files(),before)

    def test_partial_unit_success_retains_tasks_but_does_not_claim_full_mastery(self):
        self.f.f.save(self.f.f.card(criteria=['Explain','Implement']))
        self.f.save()
        value=self.read()
        self.assertEqual(value['demonstrated_units'],[])
        self.assertEqual(value['completed_unit_tasks'][0]['criteria'],[1])
        self.assertTrue(value['completed_unit_tasks'][0]['current'])
        self.f.f.save(self.f.f.card(criteria=['Explain','Implement'],summary='New version'))
        value=self.read()
        self.assertEqual(value['studied_units'],['maps'])
        self.assertFalse(value['completed_unit_tasks'][0]['current'])

    def test_source_needs_review_preserves_history_but_blocks_route(self):
        self.f.save()
        self.change({'goal':{'description':'Maps'}})
        self.change({'proposal':{'summary':'Maps','steps':['Maps'],'step_bases':[{'unit_ids':['maps']}]}})
        self.change({'accept_proposal':True})
        source=self.f.f.get()['sources'][0]
        snapshot=self.f.f.f.path/source['snapshot']
        snapshot.chmod(0o600);snapshot.write_text('Damaged source')
        value=self.read()
        self.assertEqual(value['studied_units'],['maps'])
        self.assertEqual(len(value['unit_history']),1)
        self.assertEqual(value['demonstrated_units'],[])
        self.assertIsNone(value['next_step'])

    def test_two_units_same_chat_require_choice_and_invalid_checkpoint_is_visible(self):
        self.f.save(dict(recap='Maps',next_step='Practice',pending_question=None),checkpoint=True,session='a')
        self.f.f.save(self.f.f.card('other'))
        import unit_learning
        path=self.f.f.f.directory/'other-checkpoint.json'
        path.write_text(json.dumps(dict(recap='Other',next_step='Review',pending_question=None)))
        current=unit_learning.learn_unit(self.args(unit_id='other'))
        unit_learning.learn_unit(self.args(unit_id='other',session='a',checkpoint_file=path,
            expected_revision=current['learning']['revision'],expected_unit_sha256=current['unit']['content_sha256']))
        self.assertIsNone(self.read(session='a')['stopped_at'])
        self.assertTrue(self.read(session='a')['continuation_choice_required'])
        self.f.f.save(self.f.f.card('other',summary='Changed'))
        value=self.read(session='b')
        self.assertEqual(len(value['continuations']),2)
        self.assertEqual(value['stopped_at']['unit'],'maps')
        self.assertFalse(next(c for c in value['continuations'] if c['unit']=='other')['valid'])

    def test_corrupt_unit_journal_does_not_hide_legacy_results(self):
        self.legacy('theory_checked')
        self.f.path.write_text('{')
        before=self.files()
        value=self.read()
        self.assertFalse(value['valid'])
        self.assertEqual(value['confirmed_lesson_knowledge'][0]['topic'],'Legacy maps')
        self.assertEqual(self.files(),before)

    def test_wal_database_is_preserved_and_reported_without_writing_shared_memory(self):
        self.legacy()
        path=self.f.f.f.path/'learners/default/progress.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
        before=self.files()
        value=self.read()
        self.assertFalse(value['valid'])
        self.assertIn('WAL',value['reason'])
        self.assertEqual(self.files(),before)

    def test_resume_cli_accepts_session(self):
        with patch.object(sys,'argv',['course.py','resume','--course',str(self.args().course),'--session','a']),contextlib.redirect_stdout(io.StringIO()) as output:
            course.main()
        self.assertIn('learning_progress',json.loads(output.getvalue()))

    def test_segmented_history_and_tasks_are_bounded_without_losing_counts(self):
        import test_streaming_learning
        seeded=test_streaming_learning.Streaming();seeded.f=self.f;seeded.seed(count=160)
        before=self.files()
        value=self.read()
        self.assertEqual(value['unit_event_count'],160)
        self.assertEqual(len(value['unit_history']),100)
        self.assertTrue(value['unit_history_truncated'])
        self.assertEqual(value['completed_unit_task_count'],160)
        self.assertEqual(len(value['completed_unit_tasks']),100)
        self.assertEqual(value['unit_history'][0]['revision'],61)
        self.assertEqual(value['demonstrated_units'],['maps'])
        self.assertEqual(self.files(),before)

    def test_completed_evidence_does_not_bypass_prerequisite_chain(self):
        self.f.save()
        self.f.f.save(self.f.f.card('unassessed'))
        self.change({'goal':{'description':'Maps'}})
        self.change({'proposal':{'summary':'Dependencies','steps':['Start','Known independently','Apply'],
            'step_bases':[{'unit_ids':['unassessed']},{'unit_ids':['maps'],'prerequisites':[1]}, {'prerequisites':[2]}]}})
        self.change({'accept_proposal':True})
        value=self.read()
        self.assertEqual(value['completed_steps'],[2])
        self.assertEqual(value['blocked_steps'],[2,3])
        self.assertEqual(value['next_step']['index'],1)


if __name__ == '__main__': unittest.main()
