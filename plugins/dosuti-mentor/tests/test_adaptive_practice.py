"""Practice provenance uses the unit journal, never a second grading system."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_lesson_context as contexts
import test_unit_learning as fixtures
import unit_learning as ul


class AdaptivePractice(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.UnitLearning();self.f.setUp();self.addCleanup(self.f.tearDown)

    def write(self,value,revision=None,session='a',**kw):
        state=self.f.read();p=self.f.f.f.directory/'practice.json';p.write_text(json.dumps(value))
        return ul.learn_unit(self.f.args(practice_file=p,session=session,
            expected_revision=state['learning']['revision'] if revision is None else revision,
            expected_unit_sha256=state['unit']['content_sha256'],**kw))

    def task(self,identity='map-1',question='What does a map store?'):
        return self.write(dict(action='task',id=identity,question=question,criteria=[1],
            rationale='Practice the lesson goal using the unverified map criterion'))

    def help(self,level='hint',request=None,identity='map-1'):
        return self.write(dict(action='help',task_id=identity,level=level,
            content='Consider how keys identify values',request=request))

    def attempt(self,**kw):return self.f.save(self.f.event(task_id='map-1',**kw))

    def test_explanation_cannot_reference_task_without_changing_journal_or_revision(self):
        self.task()
        explained=self.f.event(stage='explained',question=None,answer=None,criteria=[])
        state=self.f.save(explained)['learning']
        before=self.f.path.read_bytes();revision=state['revision']
        for question,identity in ((None,'map-1'),('Explanation of keys','map-1'),(None,'unknown-task')):
            with self.subTest(question=question,task_id=identity):
                with self.assertRaisesRegex(ValueError,'Explanation cannot reference a practice task'):
                    self.f.save(dict(explained,question=question,task_id=identity))
                self.assertEqual(self.f.path.read_bytes(),before)
                self.assertEqual(ul.load(self.f.path)['revision'],revision)
        state=self.attempt()['learning']
        self.assertEqual(state['revision'],revision+1)
        self.assertEqual(state['attempt_count'],1);self.assertEqual(state['explanation_count'],1)
        self.assertTrue(state['all_criteria_demonstrated'])

    def test_cli_rejects_task_explanation_without_traceback_or_writes(self):
        state=self.task();before=self.f.path.read_bytes();revision=state['learning']['revision']
        path=self.f.f.f.directory/'invalid-task-explanation.json'
        path.write_text(json.dumps(self.f.event(stage='explained',question=None,answer=None,criteria=[],task_id='map-1')))
        process=subprocess.run([sys.executable,'-B',str(Path(ul.__file__).with_name('course.py')),
            'learn-unit','--course',str(self.f.args().course),'--profile','default','--unit-id','maps',
            '--session','cli-chat','--event-file',str(path),'--expected-revision',str(revision),
            '--expected-unit-sha256',state['unit']['content_sha256']],capture_output=True,text=True,
            timeout=20,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,1)
        self.assertIn('Error: Explanation cannot reference a practice task',process.stderr)
        self.assertNotIn('Traceback',process.stderr);self.assertNotIn('TypeError',process.stderr)
        self.assertEqual(process.stdout,'');self.assertEqual(self.f.path.read_bytes(),before)
        self.assertEqual(ul.load(self.f.path)['revision'],revision)

    def test_gradual_help_and_no_phantom_attempt(self):
        self.task()
        for level in ('hint','concrete','explanation'):
            state=self.help(level)['learning']
            self.assertEqual(state['attempt_count'],0)
            self.assertFalse(state['all_criteria_demonstrated'])
        task=state['practice'][0]
        self.assertEqual([h['level'] for h in task['help']],['hint','concrete','explanation'])
        self.assertEqual(task['next_action'],'try_with_help')
        with self.assertRaisesRegex(ValueError,'help'):self.attempt()
        with self.assertRaisesRegex(ValueError,'help'):self.attempt(stage='practice_with_help',hints=1)
        self.assertFalse(self.attempt(stage='practice_with_help',hints=3)['learning']['all_criteria_demonstrated'])

    def test_full_solution_requires_request_but_bypasses_hint_steps(self):
        self.task();before=self.f.path.read_bytes()
        with self.assertRaises(ValueError):self.help('solution')
        self.assertEqual(self.f.path.read_bytes(),before)
        state=self.help('solution','Please show the full solution')['learning']
        self.assertTrue(state['practice'][0]['solution_shown'])
        self.assertEqual(state['practice'][0]['help'][0]['request'],'Please show the full solution')
        with self.assertRaises(ValueError):self.attempt()

    def test_error_correction_and_fresh_independent_task(self):
        self.task();state=self.attempt(stage='needs_review',answer='Only keys')['learning']
        self.assertEqual(state['practice'][0]['next_action'],'explain_error_then_retry')
        self.help();self.attempt(stage='practice_with_help',hints=1)
        state=self.task('map-2','Trace put(a,2), put(a,3): what value and size?')
        result=self.f.save(self.f.event(task_id='map-2',question=state['learning']['practice'][1]['question']))
        self.assertTrue(result['learning']['all_criteria_demonstrated'])
        self.assertEqual(result['learning']['attempt_count'],3)
        self.assertEqual(result['learning']['practice'][0]['next_action'],'fresh_independent_task')

    def test_no_unlinked_or_relabelled_copy_bypass(self):
        self.task();self.help('solution','Show solution')
        with self.assertRaisesRegex(ValueError,'help'):self.f.save()
        self.task('map-2')
        with self.assertRaisesRegex(ValueError,'help'):
            self.f.save(self.f.event(task_id='map-2'))

    def test_objective_constraints_cas_and_preservation(self):
        self.task();before=self.f.path.read_bytes()
        for operation in (lambda:self.help('concrete'),lambda:self.task(),
                          lambda:self.help(identity='unknown'),lambda:self.attempt(question='Other question')):
            with self.subTest(operation=operation),self.assertRaises(ValueError):operation()
            self.assertEqual(self.f.path.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'another chat'):
            self.write(dict(action='help',task_id='map-1',level='hint',content='Look at the keys',request=None),revision=0)
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_unfinished_practice_survives_new_process_without_writes(self):
        self.task();self.help();before=self.f.path.read_bytes()
        script=Path(ul.__file__).with_name('course.py')
        process=subprocess.run([sys.executable,'-B',str(script),'learn-unit','--course',str(self.f.args().course),
            '--unit-id','maps','--profile','default','--session','new-chat'],capture_output=True,text=True,
            timeout=20,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        task=json.loads(process.stdout)['learning']['practice'][0]
        self.assertIsNone(task['last_attempt']);self.assertEqual(len(task['help']),1)
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_legacy_assessment_remains_unchanged(self):
        self.f.save();raw=json.loads(self.f.path.read_text());raw['schema']=ul.LEGACY_SCHEMA
        self.f.path.write_text(json.dumps(raw));state=self.f.read()['learning'];before=self.f.path.read_bytes()
        self.assertTrue(state['all_criteria_demonstrated']);self.assertEqual(state['practice'],[])
        self.assertEqual(self.f.read()['learning']['criteria'],state['criteria'])
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_pause_with_unattempted_task_has_no_quota_or_grade(self):
        self.task();self.help()
        checkpoint=dict(recap='One hint given; no answer yet',next_step='Return to map-1',pending_question='What does a map store?')
        self.f.save(checkpoint,checkpoint=True)
        state=self.f.read(session='new-chat')['learning']
        self.assertIsNone(state['practice'][0]['last_attempt'])
        self.assertEqual(state['attempt_count'],0)
        self.assertEqual(len(ul.checkpoint_events(ul.load(self.f.path))),1)

    def test_normalized_copy_and_solution_provenance_are_preserved(self):
        self.task();self.help('solution','Show the full solution')
        with self.assertRaisesRegex(ValueError,'help'):
            self.f.save(self.f.event(question='  WHAT does a MAP store!!!  '))
        with self.assertRaises(ValueError):self.help('hint')
        self.attempt(stage='needs_review',hints=1,answer='Wrong again')
        self.attempt(stage='practice_with_help',hints=1)
        task=self.f.read()['learning']['practice'][0]
        self.assertTrue(task['solution_shown']);self.assertEqual(task['next_action'],'fresh_independent_task')

    def test_segmented_journal_keeps_help_and_old_attempts(self):
        import learning_segments
        self.task();self.attempt(stage='needs_review',answer='Only keys')
        for level in ('hint','concrete','explanation','explanation'):self.help(level)
        raw=json.loads(self.f.path.read_text())
        with patch('learning_segments.LIMIT',2200):learning_segments.write(self.f.path,raw)
        self.assertEqual(json.loads(self.f.path.read_text())['schema'],'unit_learning_v3')
        before=self.f.path.read_bytes();state=self.f.read()['learning']
        self.assertEqual(len(state['practice'][0]['help']),4)
        self.assertEqual(state['practice'][0]['last_attempt']['answer'],'Only keys')
        self.assertEqual(state['practice'][0]['next_action'],'try_with_help')
        self.assertEqual(self.f.path.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'help'):self.attempt()
        self.attempt(stage='practice_with_help',hints=4)

    def test_lesson_binding_and_completed_lesson_guard(self):
        f=contexts.LessonContext();f.setUp();self.addCleanup(f.doCleanups)
        self.f=f.f
        first=f.context(f.create())['value']['id']
        second=f.context(f.create())['value']['id']
        self.write(dict(action='task',id='map-1',question='What does a map store?',criteria=[1],rationale='Practice lesson goal'),lesson_id=first)
        self.assertEqual(self.f.read(lesson_id=second)['learning']['practice'],[])
        with self.assertRaisesRegex(ValueError,'another lesson'):
            self.f.save(self.f.event(task_id='map-1'),lesson_id=second)
        state=self.f.read();p=self.f.f.f.directory/'complete.json'
        p.write_text(json.dumps(dict(action='complete',checkpoint=dict(recap='Stopped',next_step='New lesson',pending_question=None))))
        ul.learn_unit(self.f.args(lesson_state_file=p,lesson_id=first,session='a',
            expected_revision=state['learning']['revision'],expected_unit_sha256=state['unit']['content_sha256']))
        before=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'Completed'):self.help()
        with self.assertRaisesRegex(ValueError,'Completed'):self.attempt()
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_real_cli_task_write(self):
        state=self.f.read();p=self.f.f.f.directory/'cli-task.json'
        p.write_text(json.dumps(dict(action='task',id='cli-task',question='Explain key equality',criteria=[1],rationale='Verify key criterion')))
        script=Path(ul.__file__).with_name('course.py')
        process=subprocess.run([sys.executable,'-B',str(script),'learn-unit','--course',str(self.f.args().course),
            '--unit-id','maps','--session','cli-chat','--practice-file',str(p),'--expected-revision','0',
            '--expected-unit-sha256',state['unit']['content_sha256']],capture_output=True,text=True,
            timeout=20,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(json.loads(process.stdout)['learning']['practice'][0]['id'],'cli-task')


if __name__=='__main__':unittest.main()
