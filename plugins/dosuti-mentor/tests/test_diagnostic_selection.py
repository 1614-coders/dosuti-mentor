"""Selection advice is not a semantic grader or an automatic question generator."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_diagnostic_assessment as fixtures
import diagnostic_learning as dl

PLUGIN=Path(__file__).resolve().parents[1]

class DiagnosticSelection(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DiagnosticAssessment();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.d=self.f.d
        self.d.save(dict(action='complete'))
        self.d.save(dict(action='accept',competences=['Java duplicate keys','Design contrast'],next_difficulty='applied',
            question_policy='fixed_15_v1',selection_policy='adaptive_v1'))

    def ask(self,number=1,competence='Java duplicate keys',difficulty='applied'):
        return self.d.save(dict(action='prompt',competence=competence,prompt='Diagnostic example '+str(number),
            difficulty=difficulty,format='code_analysis',rubric=self.f.rubric()))

    def answer(self,result='correct',assistance='independent',competence='Java duplicate keys',difficulty='applied'):
        value=self.f.value(self.f.assessment(result,assistance))
        value.update(next_competence=competence,next_difficulty=difficulty)
        return self.d.save(value)

    def guide(self,state=None):return (state or self.d.read())['selection_guidance']

    def test_four_results_produce_distinct_actions_and_preserve_criterion_context(self):
        for i,(result,action) in enumerate([('correct','increase_or_change'),('partial','probe_gap'),
                                          ('incorrect','check_prerequisite'),('insufficient','clarify_response')],1):
            with self.subTest(result=result):
                self.ask(i)
                state=self.answer(result,competence='Design contrast')
                guide=self.guide(state)
                self.assertEqual(guide['action'],action)
                self.assertEqual(guide['last_response']['assessment']['result'],result)
                if result=='partial':self.assertEqual(guide['focus_criteria'],[self.f.rubric()['required'][1]])
                if result=='insufficient':self.assertEqual(guide['focus_criteria'],self.f.rubric()['required'])
                self.ask('other-'+str(i),competence='Design contrast')
                self.answer(competence='Java duplicate keys')

    def test_assisted_and_unknown_help_require_independent_recheck(self):
        for i,status in enumerate(['assisted','unknown'],1):
            self.ask(i);guide=self.guide(self.answer(assistance=status))
            self.assertEqual(guide['action'],'independent_recheck')
            self.assertEqual(guide['last_response']['assessment']['assistance']['status'],status)

    def test_actual_saved_transition_controls_difficulty_and_next_competence(self):
        self.ask();state=self.answer(difficulty='transfer')
        self.assertEqual(state['current']['next_difficulty'],'transfer')
        self.f.reject(dict(action='prompt',competence='Java duplicate keys',prompt='Another task',difficulty='basic',format='open',rubric=self.f.rubric()))
        self.ask(2,difficulty='transfer')
        state=self.answer('incorrect',competence='Design contrast',difficulty='basic')
        self.assertEqual(state['current']['next_competence'],'Design contrast')
        self.assertEqual(self.guide(state)['action'],'check_prerequisite')
        self.ask(3,competence='Design contrast',difficulty='basic')

    def test_exact_duplicate_is_rejected_without_spending_slot(self):
        self.ask();self.answer();before=self.d.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'repeat'):self.ask()
        self.assertEqual(self.d.path.read_bytes(),before)
        self.assertEqual(self.d.read()['current']['question_budget']['asked'],1)
        self.assertIn('Diagnostic example 1',self.guide()['used_prompts'])

    def test_fourth_consecutive_choice_is_rejected_before_bad_transition_is_saved(self):
        self.ask(1);self.answer();self.ask(2);self.answer();self.ask(3)
        raw=self.d.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'competence'):self.answer('partial')
        self.assertEqual(self.d.path.read_bytes(),raw)
        state=self.answer('partial',competence='Design contrast')
        self.assertTrue(self.guide(state)['rotation_required'])
        self.ask(4,competence='Design contrast')
        self.assertEqual(self.guide()['action'],'resume_pending')

    def test_rotation_is_advice_after_two_and_single_competence_does_not_deadlock(self):
        self.ask(1);self.answer();self.ask(2);self.answer()
        self.assertTrue(self.guide()['rotation_recommended'])
        self.d.save(dict(action='complete'))
        self.d.save(dict(action='accept',competences=['Java duplicate keys'],next_difficulty='applied',
            question_policy='fixed_15_v1',selection_policy='adaptive_v1'))
        for i in range(4):self.ask(i);self.answer('partial')
        self.assertFalse(self.guide()['rotation_required'])

    def test_remaining_slots_prioritize_unassessed_not_automatically_a_gap(self):
        # Visit the other topic, but an insufficient response does not make it assessed.
        for i in range(12):
            topic='Java duplicate keys' if i%2==0 else 'Design contrast'
            following='Design contrast' if i%2==0 else 'Java duplicate keys'
            self.ask(i,competence=topic)
            self.answer('correct' if i%2==0 else 'insufficient',competence=following)
        guide=self.guide()
        self.assertEqual(guide['remaining'],3);self.assertTrue(guide['near_end'])
        self.assertEqual(guide['priority_competences'],['Design contrast'])
        self.assertNotIn('identified_gaps',guide)
        self.assertEqual(guide['unassessed_competences'],['Design contrast'])

    def test_fifteenth_answer_completes_without_rotation_deadlock(self):
        for i in range(15):
            topic='Java duplicate keys' if i%2==0 else 'Design contrast'
            following='Design contrast' if i%2==0 else 'Java duplicate keys'
            self.ask(i,competence=topic);state=self.answer(competence=following)
        self.assertEqual(state['status'],'completed')
        self.assertEqual(self.guide(state)['action'],'completed')
        self.assertEqual(self.guide(state)['remaining'],0)

    def test_skip_and_pending_resume_do_not_invent_result(self):
        self.ask();state=self.d.save(dict(action='skip',prompt_revision=self.d.read()['current']['pending_prompt']['revision'],
            next_difficulty='applied',next_competence='Design contrast',selection_reason='Other untested competence'))
        self.assertEqual(self.guide(state)['action'],'switch_after_skip')
        self.assertIsNone(self.guide(state)['last_response'])
        self.assertEqual(self.guide(state)['unassessed_competences'],['Java duplicate keys','Design contrast'])
        self.ask(2,competence='Design contrast');self.d.save(dict(action='pause'))
        before=self.d.path.read_bytes()
        result=subprocess.run([sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'diagnostic','--course',str(self.d.f.a.course),
            '--profile',self.d.f.a.profile,'--session','another-chat'],capture_output=True,encoding='utf-8',timeout=20,env=dict(os.environ,PYTHONUTF8='1'))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.guide(json.loads(result.stdout))['action'],'resume_pending')
        self.assertEqual(self.d.path.read_bytes(),before)
        self.d.save(dict(action='resume'));self.assertEqual(self.guide()['action'],'resume_pending')

    def test_guidance_carries_saved_goal_and_does_not_write_or_select_for_model(self):
        import learning_intent
        learning_intent.commit(self.d.f.args(expected_revision=0,reason='QA learner goal'),dict(goal=dict(description='Build a Java backend cache')))
        path=learning_intent.path_for(self.d.f.a);raw=path.read_bytes();journal=self.d.path.read_bytes()
        guide=self.guide()
        self.assertEqual(guide['goal']['description'],'Build a Java backend cache')
        self.assertEqual(path.read_bytes(),raw);self.assertEqual(self.d.path.read_bytes(),journal)
        self.assertNotIn('next_competence',guide)

    def test_legacy_events_keep_repeats_and_unknown_result_is_not_reclassified(self):
        self.d.save(dict(action='complete'));self.d.accept();self.d.prompt();self.d.answer('unknown','basic')
        self.d.prompt();self.d.answer('unknown','basic')
        raw=self.d.path.read_bytes();state=self.d.read();guide=self.guide(state)
        self.assertEqual(guide['action'],'review_legacy_evidence')
        self.assertNotIn('assessment',guide['last_response'])
        self.assertEqual(self.d.path.read_bytes(),raw)
        self.assertNotIn('selection_policy',state['current'])

    def test_corrupt_journal_does_not_offer_a_new_diagnostic(self):
        self.d.path.write_text('{broken')
        before=self.d.path.read_bytes();state=self.d.read()
        self.assertFalse(state['valid'])
        self.assertFalse(self.guide(state)['valid'])
        self.assertEqual(self.guide(state)['action'],'repair_diagnostic')
        self.assertEqual(self.d.path.read_bytes(),before)

    def test_new_cli_chat_restores_partial_focus_and_saved_transition(self):
        self.ask();self.answer('partial',competence='Design contrast',difficulty='basic')
        before=self.d.path.read_bytes()
        result=subprocess.run([sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'diagnostic','--course',str(self.d.f.a.course),
            '--profile',self.d.f.a.profile,'--session','another-chat'],capture_output=True,encoding='utf-8',timeout=20,env=dict(os.environ,PYTHONUTF8='1'))
        self.assertEqual(result.returncode,0,result.stderr);state=json.loads(result.stdout)
        self.assertEqual(self.guide(state)['action'],'probe_gap')
        self.assertEqual(self.guide(state)['focus_criteria'],[self.f.rubric()['required'][1]])
        self.assertEqual(state['current']['next_competence'],'Design contrast')
        self.assertEqual(state['current']['next_difficulty'],'basic')
        self.assertEqual(self.d.path.read_bytes(),before)

    def test_invalid_selection_policy_does_not_modify_history(self):
        self.d.save(dict(action='complete'))
        for patch in [dict(selection_policy='other'),dict(selection_policy='adaptive_v1')]:
            value=dict(action='accept',competences=['X'],next_difficulty='basic',**patch)
            if patch['selection_policy']=='other':value['question_policy']='fixed_15_v1'
            self.f.reject(value)

if __name__=='__main__':unittest.main()
