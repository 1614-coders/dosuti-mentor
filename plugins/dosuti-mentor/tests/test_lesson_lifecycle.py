"""Lesson lifecycle reuses the evidence journal; completion never grades the learner."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_lesson_context as fixtures
import unit_learning as ul
import learning_intent as intent
import adaptive_learning as adaptive

PLUGIN=Path(__file__).resolve().parents[1]

class LessonLifecycle(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LessonContext();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.u=self.f.f
        self.identity=self.f.context(self.f.create())['value']['id']

    def checkpoint(self):return dict(recap='Discussed equal keys',next_step='Trace another map',pending_question='How many keys?')
    def read(self,identity=None,**kw):return self.u.read(lesson_id=identity or self.identity,**kw)
    def state(self,identity=None):return self.read(identity)['learning']['lesson_state']
    def change(self,action,identity=None,revision=None,**value):
        state=self.read(identity);p=self.u.f.f.directory/'lifecycle-input.json'
        data=dict(action=action,**value)
        if action in ('pause','complete','confirm_switch'):data.setdefault('checkpoint',self.checkpoint())
        p.write_text(json.dumps(data))
        return ul.learn_unit(self.u.args(lesson_id=identity or self.identity,lesson_state_file=p,session='a',
            expected_revision=state['learning']['revision'] if revision is None else revision,
            expected_unit_sha256=state['unit']['content_sha256']))

    def test_pause_resume_complete_preserves_checkpoint_and_does_not_grade(self):
        self.assertEqual(self.state()['status'],'active')
        self.change('pause');self.assertEqual(self.state()['status'],'paused')
        self.change('resume');self.assertEqual(self.state()['status'],'active')
        state=self.change('complete');learning=state['learning']
        self.assertEqual(learning['lesson_state']['status'],'completed')
        self.assertEqual(learning['lesson_checkpoint']['event']['value']['pending_question'],'How many keys?')
        self.assertEqual(learning['attempt_count'],0);self.assertFalse(learning['all_criteria_demonstrated'])
        self.assertEqual(state['learning_progress']['demonstrated_units'],[])
        before=self.u.path.read_bytes()
        for action in ('resume','pause','request_switch'):
            with self.subTest(action=action),self.assertRaises(ValueError):
                self.change(action,**(dict(request_id='after-complete',target_topic='HTTP') if action=='request_switch' else {}))
            self.assertEqual(self.u.path.read_bytes(),before)

    def test_completed_lesson_is_not_a_continuation_anywhere(self):
        self.u.save(self.checkpoint(),checkpoint=True,lesson_id=self.identity)
        self.change('complete')
        self.assertFalse(self.read()['learning']['checkpoint']['valid'])
        progress=intent.view(self.u.args(session='another-chat'))['learning_progress']
        self.assertEqual(progress['continuations'],[]);self.assertIsNone(progress['stopped_at'])
        suggestions=adaptive.study_next(self.u.args(session='a',limit=20))
        self.assertEqual(suggestions['continuation_count'],0)
        self.assertEqual(len(self.read()['learning']['lesson_contexts']),1)

    def test_switch_confirmation_is_once_and_pauses_old_lesson(self):
        self.change('request_switch',request_id='change-to-http',target_topic='Java HTTP')
        self.assertTrue(self.state()['confirmation_required'])
        before=self.u.path.read_bytes()
        self.change('request_switch',request_id='change-to-http',target_topic='Java HTTP')
        self.assertEqual(self.u.path.read_bytes(),before)
        result=self.change('confirm_switch',request_id='change-to-http')
        self.assertEqual(result['learning']['lesson_state']['status'],'paused')
        self.assertFalse(self.state()['confirmation_required'])
        before=self.u.path.read_bytes()
        self.change('confirm_switch',request_id='change-to-http')
        self.change('request_switch',request_id='change-to-http',target_topic='Java HTTP')
        self.assertEqual(self.u.path.read_bytes(),before)
        self.assertEqual(self.state()['switch_request']['decision'],'confirmed')
        notes=self.u.f.f.directory/'http.txt';notes.write_text('HTTP response codes\n404 means the requested resource was not found.\n')
        self.u.f.save(self.u.f.card('http',title='Java HTTP',criteria=['Explain a 404 response'],
            sources=[dict(kind='text',role='external',notes='HTTP fixture',path=str(notes),start_line=1,end_line=2)]))
        state=ul.learn_unit(self.u.f.args(unit_id='http',session='new-topic-chat'))
        path=self.u.f.f.directory/'http-context.json'
        path.write_text(json.dumps(dict(kind='new_topic',topic='Java HTTP',goal='Explain HTTP response codes',plan=['Trace an absent resource'])))
        state=ul.learn_unit(self.u.f.args(unit_id='http',session='new-topic-chat',lesson_context_file=path,
            expected_revision=state['learning']['revision'],expected_unit_sha256=state['unit']['content_sha256']))
        self.assertEqual(state['unit']['id'],'http')
        self.assertEqual(state['learning']['lesson_state']['status'],'active')
        self.assertEqual(self.state()['status'],'paused')
        self.assertEqual(self.read()['learning']['lesson_checkpoint']['event']['value']['recap'],self.checkpoint()['recap'])

    def test_cancel_keeps_previous_lesson_and_does_not_reprompt(self):
        self.change('request_switch',request_id='switch-1',target_topic='Java HTTP')
        self.change('cancel_switch',request_id='switch-1')
        self.assertEqual(self.state()['status'],'active');self.assertFalse(self.state()['confirmation_required'])
        before=self.u.path.read_bytes()
        self.change('request_switch',request_id='switch-1',target_topic='Java HTTP')
        self.change('cancel_switch',request_id='switch-1')
        self.assertEqual(self.u.path.read_bytes(),before)
        with self.assertRaises(ValueError):self.change('confirm_switch',request_id='switch-1')
        self.change('request_switch',request_id='new-intent',target_topic='Java HTTP')
        self.assertTrue(self.state()['confirmation_required'])

    def test_pause_requires_no_confirmation_and_checkpoint_is_atomic(self):
        self.change('pause')
        self.assertIsNone(self.state()['switch_request']);self.assertFalse(self.state()['confirmation_required'])
        before=self.u.path.read_bytes()
        with self.assertRaises(ValueError):self.change('complete',checkpoint=dict(recap='',next_step='Next',pending_question=None))
        self.assertEqual(self.u.path.read_bytes(),before)
        self.assertEqual(self.state()['status'],'paused')
        with patch('learning_segments.append',side_effect=OSError('QA write failure')):
            with self.assertRaises(OSError):self.change('complete')
        self.assertEqual(self.u.path.read_bytes(),before)

    def test_new_process_restores_paused_lesson_and_pending_decision(self):
        self.change('request_switch',request_id='restore-switch',target_topic='Design basics')
        self.change('pause');before=self.u.path.read_bytes()
        command=[sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'learn-unit','--course',str(self.u.args().course),
            '--profile','default','--unit-id','maps','--session','new-chat','--lesson-id',self.identity]
        process=subprocess.run(command,capture_output=True,text=True,timeout=20,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        saved=json.loads(process.stdout)['learning']
        self.assertEqual(saved['lesson_state']['status'],'paused')
        self.assertEqual(saved['lesson_state']['switch_request']['id'],'restore-switch')
        self.assertFalse(saved['lesson_state']['confirmation_required'])
        self.assertEqual(self.u.path.read_bytes(),before)
        p=self.u.f.f.directory/'resume-state.json';p.write_text(json.dumps(dict(action='resume')))
        process=subprocess.run(command+['--lesson-state-file',str(p),'--expected-revision',str(saved['revision']),
            '--expected-unit-sha256',self.read()['unit']['content_sha256']],capture_output=True,text=True,timeout=20,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(json.loads(process.stdout)['learning']['lesson_state']['status'],'active')
        self.change('cancel_switch',request_id='restore-switch')
        self.assertEqual(self.state()['status'],'active')

    def test_multiple_lessons_and_chats_keep_independent_continuations(self):
        other=self.f.context(self.f.create())['value']['id']
        self.u.save(self.checkpoint(),checkpoint=True,lesson_id=self.identity,session='a')
        self.u.save(self.checkpoint()|dict(next_step='Other task'),checkpoint=True,lesson_id=other,session='a')
        progress=self.read(session='new-chat')['learning_progress']
        self.assertEqual({c['lesson_id'] for c in progress['continuations']},{self.identity,other})
        self.assertTrue(progress['continuation_choice_required']);self.assertIsNone(progress['stopped_at'])
        self.change('complete',identity=other)
        progress=self.read(session='new-chat')['learning_progress']
        self.assertEqual(len(progress['continuations']),1)
        self.assertEqual(progress['stopped_at']['lesson_id'],self.identity)
        self.assertEqual(self.read(other)['learning']['lesson_state']['status'],'completed')

    def test_conflicting_switches_and_stale_cas_do_not_write(self):
        self.change('request_switch',request_id='first',target_topic='HTTP')
        before=self.u.path.read_bytes()
        for action,values in [('request_switch',dict(request_id='second',target_topic='SQL')),
                              ('request_switch',dict(request_id='first',target_topic='Different')),
                              ('confirm_switch',dict(request_id='missing'))]:
            with self.subTest(action=action),self.assertRaises(ValueError):self.change(action,**values)
            self.assertEqual(self.u.path.read_bytes(),before)
        with self.assertRaises(ValueError):self.change('confirm_switch',request_id='first',revision=1)
        self.assertEqual(self.u.path.read_bytes(),before)

    def test_concurrent_decisions_only_one_wins(self):
        state=self.change('request_switch',request_id='race',target_topic='HTTP');revision=state['learning']['revision']
        paths=[]
        for action in ('confirm_switch','cancel_switch'):
            p=self.u.f.f.directory/(action+'.json');value=dict(action=action,request_id='race')
            if action=='confirm_switch':value['checkpoint']=self.checkpoint()
            p.write_text(json.dumps(value));paths.append(p)
        def save(path):
            try:
                ul.learn_unit(self.u.args(lesson_id=self.identity,lesson_state_file=path,session='another-chat',
                    expected_revision=revision,expected_unit_sha256=state['unit']['content_sha256']));return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(save,paths))
        self.assertEqual(sorted(results),[False,True])
        self.assertFalse(self.state()['confirmation_required'])

    def test_completed_lesson_rejects_bound_checkpoint_and_result(self):
        self.change('complete');before=self.u.path.read_bytes()
        for checkpoint in (True,False):
            with self.subTest(checkpoint=checkpoint),self.assertRaises(ValueError):
                self.u.save(self.checkpoint() if checkpoint else self.u.event(),checkpoint=checkpoint,lesson_id=self.identity)
            self.assertEqual(self.u.path.read_bytes(),before)
        self.assertEqual(self.read()['learning']['lesson_contexts'][0]['status'],'completed')

    def test_lifecycle_requires_matching_identity_and_valid_payload(self):
        for value in (dict(action='pause'),dict(action='unknown'),dict(action='request_switch',request_id='x',target_topic=''),
                      dict(action='resume',lesson_id='other'),dict(action='confirm_switch',request_id='missing',checkpoint=self.checkpoint())):
            with self.subTest(value=value):
                state=self.read();before=self.u.path.read_bytes();p=self.u.f.f.directory/'invalid-state.json'
                p.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    ul.learn_unit(self.u.args(lesson_id=self.identity,lesson_state_file=p,session='a',
                        expected_revision=state['learning']['revision'],expected_unit_sha256=state['unit']['content_sha256']))
                self.assertEqual(self.u.path.read_bytes(),before)

    def test_segmented_history_and_legacy_context_keep_states_without_read_writes(self):
        self.assertEqual(self.state()['status'],'active') # Stage 1 contexts need no migration.
        self.change('pause');self.change('resume');self.change('complete')
        import learning_segments
        journal=json.loads(self.u.path.read_text())
        with patch('learning_segments.LIMIT',1800):learning_segments.write(self.u.path,journal)
        before=self.u.path.read_bytes()
        self.assertEqual(self.state()['status'],'completed')
        self.assertEqual(self.u.path.read_bytes(),before)
        self.assertEqual(self.read()['learning_progress']['continuations'],[])

if __name__=='__main__':unittest.main()
