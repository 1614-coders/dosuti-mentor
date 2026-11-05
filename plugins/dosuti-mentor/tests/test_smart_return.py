"""Returning guidance projects existing evidence, never selects or writes for a learner."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_lesson_context as fixtures
import adaptive_learning as adaptive
import cli_output
import course
import diagnostic_learning
import learning_intent
import unit_learning


class SmartReturn(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LessonContext();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.u=self.f.f

    def read(self, **kw):return adaptive.study_next(self.u.args(session='new-chat',**kw))
    def files(self):
        return {str(p):p.read_bytes() for p in self.u.f.f.path.rglob('*') if p.is_file()}
    def lesson(self):return self.f.context(self.f.create())['value']['id']
    def task(self,identity=None):
        value=dict(action='task',id='unfinished',question='Trace the replacement',criteria=[1],rationale='Check equal keys')
        if identity:value['lesson_id']=identity
        return self.practice(value)
    def practice(self,value):
        state=self.u.read();path=self.u.f.f.directory/'practice-return.json';path.write_text(json.dumps(value))
        return unit_learning.learn_unit(self.u.args(practice_file=path,session='old-chat',
            expected_revision=state['learning']['revision'],expected_unit_sha256=state['unit']['content_sha256']))

    def test_new_chat_offers_saved_lesson_error_and_practice_without_writing(self):
        identity=self.lesson()
        self.u.save(dict(recap='Traced keys',next_step='Correct replacement',pending_question='What is size?'),
                    checkpoint=True,lesson_id=identity,session='old-chat')
        self.u.save(self.u.event(stage='needs_review'))
        before=self.files();state=self.read();value=state['returning']
        self.assertEqual(value['course']['id'],course.read(self.u.args().course/'course.json')['id'])
        self.assertEqual(value['stopped_at']['lesson_id'],identity)
        self.assertEqual(value['stopped_at']['pending_question'],'What is size?')
        self.assertEqual([a['action'] for a in value['actions']],['continue_lesson','review_error','practice'])
        self.assertTrue(all(a['requires_consent'] and a['reason'] for a in value['actions']))
        self.assertEqual(value['knowledge']['confirmed_criteria'],[])
        self.assertEqual(value['knowledge']['gaps'][0]['unit'],'maps')
        self.assertEqual(self.files(),before)

    def test_context_without_checkpoint_does_not_invent_stopping_point(self):
        self.lesson();value=self.read()['returning']
        self.assertEqual(value['stopped_at']['topic'],'Java Map replacement')
        self.assertIsNone(value['stopped_at']['pending_question'])
        self.assertIsNone(value['stopped_at']['next_step'])
        self.assertEqual(value['stopped_at']['goal'],self.f.value()['goal'])

    def test_multiple_lessons_require_choice_even_in_original_session(self):
        identities={self.lesson(),self.lesson()}
        value=self.read()['returning']
        self.assertTrue(value['continuation_choice_required']);self.assertIsNone(value['stopped_at'])
        self.assertEqual({r['lesson_id'] for r in value['continuations']},identities)
        self.assertEqual(value['actions'][0]['action'],'choose_continuation')
        self.assertFalse(any(a['action']=='continue_lesson' for a in value['actions']))

    def test_legacy_checkpoint_survives_new_process_and_compact_projection(self):
        self.u.save(dict(recap='Legacy chat',next_step='Trace keys',pending_question='Why?'),checkpoint=True,session='old-chat')
        before=self.files()
        process=subprocess.run([sys.executable,'-B',str(Path(course.__file__)),'study-next','--course',str(self.u.args().course),
            '--profile','default','--session','new-chat','--compact'],capture_output=True,text=True,timeout=20)
        self.assertEqual(process.returncode,0,process.stderr)
        state=json.loads(process.stdout)
        self.assertEqual(state['returning']['stopped_at']['pending_question'],'Why?')
        self.assertEqual(state['returning']['stopped_at']['session'],'old-chat')
        self.assertEqual(self.files(),before)

    def test_pending_task_and_shown_solution_require_fresh_independent_task(self):
        identity=self.lesson();self.task(identity)
        self.practice(dict(action='help',task_id='unfinished',level='solution',content='Full worked solution',request='Show it'))
        value=self.read()['returning'];task=value['unfinished_tasks'][0]
        self.assertTrue(task['solution_shown']);self.assertEqual(task['help_count'],1)
        self.assertEqual(task['lesson_id'],identity)
        self.assertEqual(value['actions'][1]['action'],'fresh_independent_task')
        self.assertEqual(value['knowledge']['confirmed_criteria'],[])

    def test_completed_lessons_and_their_unattempted_tasks_are_not_continuations(self):
        identity=self.lesson();self.task(identity)
        state=self.u.read();path=self.u.f.f.directory/'complete-return.json'
        path.write_text(json.dumps(dict(action='complete',checkpoint=dict(recap='Stopped',next_step='Optional',pending_question=None))))
        unit_learning.learn_unit(self.u.args(lesson_id=identity,lesson_state_file=path,session='old-chat',
            expected_revision=state['learning']['revision'],expected_unit_sha256=state['unit']['content_sha256']))
        value=self.read()['returning'];self.assertEqual(value['continuations'],[])
        self.assertEqual(value['unfinished_tasks'],[]);self.assertIsNone(value['stopped_at'])

    def test_explanation_is_unverified_and_partial_independent_criteria_keep_scope(self):
        self.u.f.save(self.u.f.card(criteria=['Trace keys','Implement a fresh solution']))
        self.u.save(self.u.event(stage='explained',criteria=[],question=None,answer=None))
        value=self.read()['returning'];self.assertEqual(value['knowledge']['confirmed_criteria'],[])
        self.assertEqual(len(value['knowledge']['unverified_criteria']),2)
        self.u.save(self.u.event(criteria=[1]));value=self.read()['returning']
        self.assertEqual([r['criterion'] for r in value['knowledge']['confirmed_criteria']],[1])
        self.assertEqual([r['criterion'] for r in value['knowledge']['unverified_criteria']],[2])

    def test_profiles_and_courses_remain_independent(self):
        self.lesson();self.u.save()
        other=fixtures.LessonContext();other.setUp();self.addCleanup(other.doCleanups)
        other_value=adaptive.study_next(other.f.args(session='another'))['returning']
        own=self.read()['returning']
        self.assertNotEqual(other_value['course']['id'],own['course']['id'])
        self.assertEqual(other_value['continuations'],[])
        self.assertEqual(other_value['knowledge']['confirmed_criteria'],[])
        friend=self.read(profile='friend')['returning']
        self.assertEqual(friend['profile'],'friend');self.assertEqual(friend['continuations'],[])
        self.assertEqual(friend['knowledge']['confirmed_criteria'],[])

    def test_absent_progress_and_registry_do_not_create_files_or_invent_lesson(self):
        self.u.f.path.unlink();before=self.files();value=self.read()['returning']
        self.assertIsNone(value['stopped_at']);self.assertEqual(value['knowledge']['confirmed_criteria'],[])
        self.assertEqual([a['action'] for a in value['actions']],['choose_topic','add_material'])
        self.assertEqual(self.files(),before)

    def test_absent_journal_with_prepared_unit_does_not_invent_progress(self):
        before=self.files();value=self.read()['returning']
        self.assertEqual(value['status'],'no_progress');self.assertIsNone(value['stopped_at'])
        self.assertEqual(value['knowledge']['confirmed_criteria'],[])
        self.assertEqual(len(value['actions']),2);self.assertEqual(self.files(),before)

    def test_saved_review_skip_does_not_resurface_unfinished_practice(self):
        self.task();state=self.u.read();path=self.u.f.f.directory/'skip-return.json'
        path.write_text(json.dumps(dict(choice='skip',criteria=[1],until=None,reason='Learner wants to postpone')))
        unit_learning.learn_unit(self.u.args(review_file=path,session='old-chat',
            expected_revision=state['learning']['revision'],expected_unit_sha256=state['unit']['content_sha256']))
        value=self.read()['returning']
        self.assertEqual(len(value['unfinished_tasks']),1)
        self.assertFalse(any(a['action'] in ('practice','fresh_independent_task','review_error') for a in value['actions']))

    def test_pending_diagnostic_is_offered_without_automatic_resume(self):
        path=self.u.f.f.directory/'diagnostic-return.json'
        path.write_text(json.dumps(dict(action='accept',competences=['Maps'],next_difficulty='applied')))
        diagnostic_learning.diagnostic(self.u.args(update_file=path,expected_revision=0,reason='Learner agreed'))
        path.write_text(json.dumps(dict(action='pause')))
        diagnostic_learning.diagnostic(self.u.args(update_file=path,expected_revision=1,reason='Learner paused'))
        before=self.files();value=self.read()['returning']
        self.assertEqual(value['actions'][0]['action'],'continue_diagnostic')
        self.assertEqual(value['knowledge']['confirmed_criteria'],[]);self.assertEqual(self.files(),before)

    def test_accepted_roadmap_is_unchanged_by_returning_options(self):
        for revision,change in enumerate((dict(goal=dict(description='Learn Java maps')),
                dict(proposal=dict(summary='Maps path',steps=['Trace maps','Practice'])),dict(accept_proposal=True))):
            learning_intent.commit(self.u.args(expected_revision=revision,reason='Explicit learner choice'),change)
        self.lesson();before=self.files();value=self.read()
        self.assertEqual(value['learning_intent']['plan']['accepted_revision'],3)
        self.assertTrue(all(a['requires_consent'] for a in value['returning']['actions']))
        self.assertEqual(self.files(),before)

    def test_stale_source_blocks_saved_lesson_and_confirmation(self):
        self.lesson();self.u.save();unit=self.u.read()['unit']
        snapshot=self.u.f.f.path/unit['sources'][0]['snapshot'];snapshot.chmod(0o600);snapshot.write_text('Changed source')
        before=self.files();value=self.read()['returning']
        self.assertIsNone(value['stopped_at']);self.assertEqual(value['knowledge']['confirmed_criteria'],[])
        self.assertEqual(value['actions'][0]['action'],'review_sources');self.assertEqual(self.files(),before)

    def test_legacy_understood_does_not_confirm_unit_or_diagnostic_knowledge(self):
        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(self.u.args(topic='Legacy maps',stage='understood',details='Old recorded claim'))
        value=self.read()['returning']
        self.assertEqual(value['knowledge']['confirmed_criteria'],[])
        self.assertEqual(value['knowledge']['diagnostic']['confirmed_knowledge'],[])

    def test_damaged_journals_and_registry_return_no_confident_actions_or_writes(self):
        for path in (self.u.path,self.u.path.with_name('learning-intent.json'),
                     self.u.path.with_name('diagnostic-learning.json'),self.u.f.path):
            old=path.read_bytes() if path.exists() else None
            try:
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{broken');before=self.files()
                value=self.read()['returning']
                self.assertFalse(value['valid']);self.assertEqual(value['actions'],[])
                self.assertIsNone(value['stopped_at']);self.assertEqual(self.files(),before)
            finally:
                if old is None:path.unlink(missing_ok=True)
                else:path.write_bytes(old)

    def test_io_failure_is_explicit_without_recommendations(self):
        with patch('unit_learning.load',side_effect=PermissionError('QA denied')):
            value=self.read()
        self.assertFalse(value['valid']);self.assertEqual(value['returning']['actions'],[])
        self.assertIn('QA denied',value['returning']['reason'])

    def test_legacy_journal_schemas_are_read_without_migration(self):
        self.u.save()
        for schema in ('unit_learning_v1','unit_learning_v2'):
            raw=json.loads(self.u.path.read_text());raw['schema']=schema
            self.u.path.write_text(json.dumps(raw));before=self.files()
            value=self.read()['returning']
            self.assertTrue(value['valid']);self.assertEqual(len(value['knowledge']['confirmed_criteria']),1)
            self.assertEqual(self.files(),before)

    def test_summary_is_bounded_and_same_in_compact_output(self):
        for _ in range(7):self.lesson()
        state=self.read();value=state['returning']
        self.assertEqual(value['continuation_count'],7);self.assertLessEqual(len(value['continuations']),5)
        self.assertIn('continuations',value['truncated_fields'])
        self.assertTrue(value['continuation_choice_required']);self.assertLessEqual(len(value['actions']),3)
        self.assertEqual(cli_output.compact('study-next',state)['returning'],value)
