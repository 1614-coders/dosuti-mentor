"""A versioned attempt budget is replayed from prompts, never a separate counter."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_diagnostic_learning as fixtures
import diagnostic_learning as dl

PLUGIN = Path(__file__).resolve().parents[1]


class DiagnosticBudget(unittest.TestCase):
    def setUp(self):
        self.d = fixtures.DiagnosticLearning(); self.d.setUp(); self.addCleanup(self.d.tearDown)

    def accept(self):
        return self.d.save(dict(action='accept', competences=['Trace a loop', 'Explain a map'],
            next_difficulty='basic', question_policy='fixed_15_v1'))

    def ask(self):
        current=self.d.read()['current']
        competence=current.get('next_competence', 'Trace a loop')
        return self.d.save(dict(action='prompt',competence=competence,
            prompt='Trace example '+str(len(current['answers'])+len(current.get('skips',[]))+1),
            difficulty='basic',format='open'))

    def answer(self, next_competence='Trace a loop'):
        return self.d.answer(next_difficulty='basic',next_competence=next_competence)

    def skip(self):
        current=self.d.read()['current'];prompt=current['pending_prompt']
        other='Explain a map' if prompt['competence']=='Trace a loop' else 'Trace a loop'
        return self.d.save(dict(action='skip',prompt_revision=prompt['revision'],next_difficulty='basic',
            next_competence=other,selection_reason='Continue with another competence',skip_reason='Not studied'))

    def fill(self, count):
        for _ in range(count):self.ask(); self.answer()

    def budget(self, state=None):
        return (state or self.d.read())['current']['question_budget']

    def test_new_attempt_has_fifteen_slots_and_no_pending_number(self):
        self.assertEqual(self.budget(self.accept()),dict(limit=15,asked=0,remaining=15,current_question=None,limit_reached=False))
        journal=json.loads(self.d.path.read_text())
        self.assertEqual(journal['schema'],'diagnostic_learning_v1')
        self.assertEqual(journal['events'][0]['value']['question_policy'],'fixed_15_v1')
        self.assertNotIn('question_budget',journal['events'][0]['value'])

    def test_prompt_uses_one_slot_and_answer_does_not_use_another(self):
        self.accept();state=self.ask()
        self.assertEqual(self.budget(state),dict(limit=15,asked=1,remaining=14,current_question=1,limit_reached=False))
        self.assertEqual(self.budget(self.answer()),dict(limit=15,asked=1,remaining=14,current_question=None,limit_reached=False))

    def test_skip_uses_only_prompt_slot_without_evidence(self):
        self.accept();self.ask();state=self.skip()
        self.assertEqual(self.budget(state)['asked'],1);self.assertEqual(self.budget(state)['remaining'],14)
        self.assertEqual(state['current']['answers'],[]);self.assertEqual(len(state['current']['skips']),1)
        self.assertEqual(state['summary']['identified_gaps'],[])
        self.assertEqual(self.budget(self.ask())['current_question'],2)

    def test_pause_resume_and_repeated_reads_do_not_increment(self):
        self.accept();self.ask();before=self.budget()
        self.d.save(dict(action='pause'));self.assertEqual(self.budget(),before)
        self.d.save(dict(action='resume'),session='another-chat')
        raw=self.d.path.read_bytes()
        for _ in range(3):self.assertEqual(self.budget(self.d.read(session='another-chat')),before)
        self.assertEqual(self.d.path.read_bytes(),raw)

    def test_new_cli_process_restores_pending_number_and_remaining(self):
        self.accept();self.fill(3);self.ask();raw=self.d.path.read_bytes()
        result=subprocess.run([sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'diagnostic',
            '--course',str(self.d.f.a.course),'--profile',self.d.f.a.profile,'--session','new-chat'],
            capture_output=True,encoding='utf-8',timeout=20,env=dict(os.environ,PYTHONUTF8='1'))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.budget(json.loads(result.stdout)),dict(limit=15,asked=4,remaining=11,current_question=4,limit_reached=False))
        self.assertEqual(self.d.path.read_bytes(),raw)

    def test_fifteenth_pending_question_exhausts_budget_and_sixteenth_is_rejected(self):
        self.accept();self.fill(14);state=self.ask()
        self.assertEqual(self.budget(state),dict(limit=15,asked=15,remaining=0,current_question=15,limit_reached=True))
        self.assertEqual(state['status'],'accepted');raw=self.d.path.read_bytes()
        with self.assertRaises(ValueError):self.ask()
        self.assertEqual(self.d.path.read_bytes(),raw)

    def test_fifteenth_answer_automatically_completes_even_if_model_proposes_next(self):
        self.accept();self.fill(14);self.ask();state=self.answer()
        self.assertEqual(state['status'],'completed');self.assertIsNone(state['current']['next_competence'])
        self.assertEqual(len(state['current']['answers']),15)
        self.assertEqual(self.budget(state),dict(limit=15,asked=15,remaining=0,current_question=None,limit_reached=True))
        raw=self.d.path.read_bytes()
        for action in ('prompt','resume'):
            with self.assertRaises(ValueError):
                if action=='prompt':self.ask()
                else:self.d.save(dict(action=action))
        self.assertEqual(self.d.path.read_bytes(),raw)

    def test_fifteenth_skip_automatically_completes_without_answer(self):
        self.accept();self.fill(14);self.ask();state=self.skip()
        self.assertEqual(state['status'],'completed');self.assertEqual(self.budget(state)['remaining'],0)
        self.assertEqual(len(state['current']['answers']),14);self.assertEqual(len(state['current']['skips']),1)
        self.assertIsNone(state['current']['next_competence'])

    def test_all_fifteen_skips_complete_without_invented_evidence(self):
        self.accept()
        for _ in range(15):self.ask();state=self.skip()
        self.assertEqual(state['status'],'completed');self.assertEqual(state['current']['answers'],[])
        self.assertEqual(len(state['current']['skips']),15);self.assertEqual(self.budget(state)['asked'],15)
        self.assertEqual(state['summary']['checked_competences'],[])

    def test_early_completion_preserves_actual_used_budget(self):
        self.accept();self.fill(3);state=self.d.save(dict(action='complete'))
        self.assertEqual(state['status'],'completed');self.assertEqual(self.budget(state)['asked'],3)
        self.assertEqual(self.budget(state)['remaining'],12)

    def test_completion_with_pending_question_preserves_it_without_fake_answer_or_skip(self):
        self.accept();self.ask();pending=self.d.read()['current']['pending_prompt']
        state=self.d.save(dict(action='complete'))
        self.assertEqual(self.budget(state)['asked'],1);self.assertEqual(state['current']['pending_prompt'],pending)
        self.assertIsNone(self.budget(state)['current_question'])
        self.assertEqual(state['current']['answers'],[]);self.assertEqual(state['current'].get('skips',[]),[])
        self.assertEqual(state['summary']['checked_competences'],[])

    def test_legacy_active_and_completed_attempt_with_sixteen_questions_is_unchanged(self):
        self.d.accept()
        for _ in range(16):self.d.prompt();self.answer()
        raw=self.d.path.read_bytes();state=self.d.read()
        self.assertEqual(state['status'],'accepted');self.assertNotIn('question_budget',state['current'])
        self.assertEqual(len(state['current']['answers']),16);self.assertEqual(self.d.path.read_bytes(),raw)
        self.d.save(dict(action='complete'));raw=self.d.path.read_bytes()
        self.assertEqual(len(self.d.read()['current']['answers']),16);self.assertEqual(self.d.path.read_bytes(),raw)

    def test_new_attempt_resets_budget_and_preserves_previous_attempt(self):
        self.accept();self.fill(15);old=self.d.read()['current'];state=self.accept()
        self.assertEqual(state['attempts'][0],old);self.assertEqual(self.budget(state)['asked'],0)
        self.assertEqual(self.budget(state)['remaining'],15);self.assertEqual(self.budget(self.ask())['current_question'],1)

    def test_legacy_then_new_attempt_uses_marker_only_for_new_attempt(self):
        self.d.accept();self.d.prompt();self.answer();self.d.save(dict(action='complete'))
        old=self.d.read()['current'];state=self.accept()
        self.assertEqual(state['attempts'][0],old);self.assertNotIn('question_budget',old)
        self.assertEqual(self.budget(state)['limit'],15)

    def test_concurrent_writers_cannot_save_two_fifteenth_prompts(self):
        self.accept();self.fill(14);revision=self.d.read()['revision']
        source=self.d.f.write('concurrent-prompt.json',dict(action='prompt',competence='Trace a loop',
            prompt='Fifteenth question',difficulty='basic',format='open'))
        def writer(_):
            try:
                dl.diagnostic(self.d.f.args(update_file=source,expected_revision=revision,reason='Boundary QA'))
                return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sorted(pool.map(writer,range(2))),[False,True])
        self.assertEqual(self.budget()['asked'],15);self.assertEqual(self.d.read()['revision'],revision+1)
        raw=self.d.path.read_bytes()
        with self.assertRaises(ValueError):self.ask()
        self.assertEqual(self.d.path.read_bytes(),raw)

    def test_unknown_policy_and_editable_budget_are_rejected_without_writing(self):
        for extra in [dict(question_policy='fixed_16_v1'),dict(question_policy=True),dict(question_budget=dict(asked=0))]:
            with self.subTest(extra=extra),self.assertRaises(ValueError):
                self.d.save(dict(action='accept',competences=['Trace a loop'],next_difficulty='basic',**extra))
            self.assertFalse(self.d.path.exists())

    def test_skill_marks_new_attempts_and_renders_persisted_question_number(self):
        for relative in ['references/adaptive-learning.md','references/start-learning.md','skills/start-learning/SKILL.md']:
            with self.subTest(relative=relative):
                content=(PLUGIN/relative).read_text()
                self.assertIn('fixed_15_v1',content)
                self.assertIn('question_budget',content)
                self.assertNotIn('10–20',content)
        self.assertIn('Вопрос N из 15',(PLUGIN/'skills/start-learning/SKILL.md').read_text())

    def test_counter_is_not_global_event_revision(self):
        self.accept();self.ask();self.answer();self.d.save(dict(action='pause'));self.d.save(dict(action='resume'))
        state=self.ask();self.assertGreater(state['revision'],2)
        self.assertEqual(self.budget(state)['asked'],2)


if __name__=='__main__':unittest.main()
