"""Argument validation must not build full learner progress before a write."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch
import test_learning_tools
import learning_intent as li
import diagnostic_learning as dl


class WritePreflight(unittest.TestCase):
    def setUp(self):
        self.f=test_learning_tools.LearningTools();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.intent_path=li.path_for(self.f.a);self.diagnostic_path=dl.path_for(self.f.a)

    def args(self,**kw):return self.f.args(reason='QA learner decision',expected_revision=0,**kw)
    def event_args(self,value,**kw):
        return self.args(update_file=self.f.write('preflight-input.json',value),**kw)

    def test_commit_checks_state_without_rendering_full_progress(self):
        with patch.object(li,'learning_progress',side_effect=AssertionError('full progress during preflight')),\
                patch.object(li,'load',wraps=li.load) as loads:
            li.commit(self.args(),dict(goal=dict(description='Learn Java')))
        self.assertEqual(loads.call_count,1)
        self.assertEqual(li.view(self.f.a)['revision'],1)

    def test_public_intent_renders_progress_only_for_the_result(self):
        args=self.event_args(dict(goal=dict(description='Learn Java')))
        with patch.object(li,'learning_progress',wraps=li.learning_progress) as progress:
            result=li.intent(args)
        self.assertEqual(progress.call_count,1)
        self.assertEqual(result,li.intent(self.f.a))

    def test_diagnostic_loads_state_under_lock_and_for_the_result_only(self):
        args=self.event_args(dict(action='decline'))
        with patch.object(dl,'load',wraps=dl.load) as loads,patch.object(dl,'summary',wraps=dl.summary) as summaries:
            result=dl.diagnostic(args)
        self.assertEqual(loads.call_count,2)
        self.assertEqual(summaries.call_count,1)
        self.assertEqual(result['status'],'declined')
        self.assertEqual(result,dl.diagnostic(self.f.a))

    def test_invalid_read_options_reject_before_journal_reads_or_file_changes(self):
        before={p:p.read_bytes() for p in self.f.a.course.rglob('*') if p.is_file()}
        incoming=self.f.write('invalid-options.json',dict(action='pause'))
        for options in ([dict(history_limit=x) for x in (True,None,-1,101,'1')]+[dict(session='../chat')]):
            with self.subTest(options=options),patch.object(li,'load',side_effect=AssertionError('intent journal read')),\
                    patch.object(dl,'load',side_effect=AssertionError('diagnostic journal read')),\
                    patch.object(li,'learning_progress',side_effect=AssertionError('progress read')):
                with self.assertRaises(ValueError):li.commit(self.args(**options),dict(goal=dict(description='Learn')))
                with self.assertRaises(ValueError):dl.diagnostic(self.args(update_file=incoming,**options))
        self.assertEqual({p:p.read_bytes() for p in self.f.a.course.rglob('*') if p.is_file()},before)
        self.assertFalse(self.intent_path.exists());self.assertFalse(self.diagnostic_path.exists())

    def test_invalid_write_metadata_rejects_before_journal_reads(self):
        incoming=self.f.write('invalid-metadata.json',dict(action='pause'))
        for changes in (dict(expected_revision=None),dict(expected_revision=True),dict(expected_revision=-1),dict(reason='')):
            values=dict(reason='QA',expected_revision=0)|changes
            args=self.f.args(update_file=incoming,**values)
            with self.subTest(changes=changes),patch.object(li,'load',side_effect=AssertionError('intent read')),\
                    patch.object(dl,'load',side_effect=AssertionError('diagnostic read')):
                with self.assertRaises(ValueError):li.commit(args,dict(goal=dict(description='Learn')))
                with self.assertRaises(ValueError):dl.diagnostic(args)

    def test_corrupt_journals_still_reject_writes_without_overwriting(self):
        self.intent_path.parent.mkdir(parents=True,exist_ok=True)
        for path in (self.intent_path,self.diagnostic_path):path.write_text('{')
        with self.assertRaises(ValueError):li.commit(self.args(),dict(goal=dict(description='Learn')))
        with self.assertRaises(ValueError):dl.diagnostic(self.event_args(dict(action='decline')))
        for path in (self.intent_path,self.diagnostic_path):self.assertEqual(path.read_text(),'{')

    def test_concurrent_intent_cas_loser_can_retry_and_continue_in_another_chat(self):
        barrier=threading.Barrier(2)
        def save(index):
            barrier.wait(timeout=5)
            try:li.commit(self.args(session='chat-'+str(index)),dict(goal=dict(description='Goal '+str(index))));return True
            except ValueError as error:
                self.assertIn('changed in another chat',str(error));return False
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sorted(pool.map(save,range(2))),[False,True])
        self.assertEqual(li.view(self.f.a)['revision'],1)
        li.commit(self.f.args(reason='Retry with current revision',expected_revision=1,session='new-chat'),dict(session='Continue study'))
        state=li.view(self.f.args(session='new-chat'))
        self.assertEqual(state['revision'],2);self.assertEqual(state['session_task']['task'],'Continue study')

    def test_diagnostic_pause_cas_retry_resume_preserves_pending_question(self):
        dl.diagnostic(self.event_args(dict(action='accept',competences=['Map'],next_difficulty='applied',question_policy='fixed_15_v1')))
        prompt=dict(action='prompt',competence='Map',prompt='Trace two duplicate keys',difficulty='applied',format='code_analysis')
        dl.diagnostic(self.f.args(reason='QA question',expected_revision=1,update_file=self.f.write('prompt.json',prompt)))
        before=dl.view(self.f.a)['current'];incoming=self.f.write('pause.json',dict(action='pause'))
        barrier=threading.Barrier(2)
        def pause(_):
            barrier.wait(timeout=5)
            try:dl.diagnostic(self.f.args(reason='Pause',expected_revision=2,update_file=incoming));return True
            except ValueError as error:
                self.assertIn('changed in another chat',str(error));return False
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sorted(pool.map(pause,range(2))),[False,True])
        state=dl.diagnostic(self.f.args(reason='Resume in another chat',session='other-chat',expected_revision=3,
            update_file=self.f.write('resume.json',dict(action='resume'))))
        self.assertEqual(state['revision'],4);self.assertEqual(state['status'],'accepted')
        self.assertEqual(state['current']['pending_prompt'],before['pending_prompt'])
        self.assertEqual(state['current']['question_budget'],before['question_budget'])

    def test_stale_revision_and_invalid_transitions_still_reject_without_mutation(self):
        li.commit(self.args(),dict(goal=dict(description='Learn')))
        before=self.intent_path.read_bytes()
        with self.assertRaisesRegex(ValueError,'changed in another chat'):li.commit(self.args(),dict(goal=dict(description='Stale')))
        with self.assertRaises(ValueError):li.commit(self.f.args(reason='No proposal',expected_revision=1),dict(accept_proposal=True))
        self.assertEqual(self.intent_path.read_bytes(),before)
        dl.diagnostic(self.event_args(dict(action='decline')));before=self.diagnostic_path.read_bytes()
        with self.assertRaises(ValueError):dl.diagnostic(self.f.args(reason='Invalid resume',expected_revision=1,
            update_file=self.f.write('resume.json',dict(action='resume'))))
        self.assertEqual(self.diagnostic_path.read_bytes(),before)

    def test_intent_rechecks_revision_after_another_chat_writes_between_preflight_and_lock(self):
        validate=li.validate_read_options;changed=False
        def interleave(args):
            nonlocal changed
            result=validate(args)
            if not changed:
                changed=True
                with ThreadPoolExecutor(1) as pool:
                    pool.submit(li.commit,self.args(session='other-chat'),dict(goal=dict(description='Other chat goal'))).result(timeout=5)
            return result
        with patch.object(li,'validate_read_options',side_effect=interleave):
            with self.assertRaisesRegex(ValueError,'changed in another chat'):
                li.commit(self.args(),dict(goal=dict(description='Must not replace the new goal')))
        result=li.view(self.f.a)
        self.assertEqual(result['revision'],1);self.assertEqual(result['goal']['description'],'Other chat goal')

    def test_diagnostic_rechecks_state_after_preflight_and_keeps_latest_answer_budget(self):
        dl.diagnostic(self.event_args(dict(action='accept',competences=['Map'],next_difficulty='basic',question_policy='fixed_15_v1')))
        incoming=self.f.write('concurrent-pause.json',dict(action='pause'))
        args=self.f.args(reason='Pause',update_file=incoming,expected_revision=1)
        validate=dl.validate_read_options;changed=False
        def interleave(value):
            nonlocal changed
            result=validate(value)
            if not changed:
                changed=True
                with ThreadPoolExecutor(1) as pool:pool.submit(dl.diagnostic,args).result(timeout=5)
            return result
        with patch.object(dl,'validate_read_options',side_effect=interleave):
            with self.assertRaisesRegex(ValueError,'changed in another chat'):dl.diagnostic(args)
        state=dl.view(self.f.a)
        self.assertEqual(state['revision'],2);self.assertEqual(state['status'],'paused')
        self.assertEqual(state['current']['question_budget']['asked'],0)
