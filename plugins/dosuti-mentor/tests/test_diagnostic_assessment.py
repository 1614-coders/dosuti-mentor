"""Structured assertions are assistant judgements, not semantic grading by Python."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_diagnostic_learning as fixtures
import diagnostic_learning as dl

PLUGIN=Path(__file__).resolve().parents[1]

class DiagnosticAssessment(unittest.TestCase):
    def setUp(self):
        self.d=fixtures.DiagnosticLearning();self.d.setUp();self.addCleanup(self.d.tearDown)
        self.d.save(dict(action='accept',competences=['Java duplicate keys','Design contrast'],next_difficulty='applied',question_policy='fixed_15_v1'))

    def rubric(self):
        return dict(action='Explain duplicate-key replacement and map size',
            required=['State the final value for the duplicate key','Explain why the size stays one'],
            alternatives=['A correct execution trace or equivalent explanation'],
            critical_errors=['Claim both equal keys remain as separate entries'])

    def prompt(self,competence='Java duplicate keys',rubric=None):
        return self.d.save(dict(action='prompt',competence=competence,prompt=('A poster has dark text on a dark background. Explain the issue and propose a readable palette.'
                if competence=='Design contrast' else 'After put("a",2); put("a",3), what are get("a") and size()?'),
            difficulty='applied',format='code_analysis',rubric=self.rubric() if rubric is None else rubric))

    def assessment(self,result='correct',assistance='independent'):
        met,unmet,unverified={'correct':([1,2],[],[]),'partial':([1],[2],[]),
            'incorrect':([],[1,2],[]),'insufficient':([],[],[1,2])}[result]
        return dict(result=result,met=met,unmet=unmet,unverified=unverified,critical_errors=[1] if result=='incorrect' else [],
            assistance=dict(status=assistance,details='Teacher traced the replacement' if assistance=='assisted' else None),
            verification=dict(method='assistant_review',details='Compared this answer with both saved criteria',
                              limitations=['One example; code was not executed']))

    def value(self,assessment=None,**overrides):
        assessment=self.assessment() if assessment is None else assessment
        evidence=('confirmed' if assessment['result']=='correct' and assessment['assistance']['status']=='independent' else
                  'needs_practice' if assessment['result'] in ('correct','partial') else 'unknown')
        return dict(action='answer',prompt_revision=self.d.read()['current']['pending_prompt']['revision'],
            answer={'correct':'3 and size 1: the equal key replaces the old value',
                    'partial':'3 and size 2 because put was called twice',
                    'incorrect':'2 and size 2; both equal keys stay in the map',
                    'insufficient':'I am not sure; could you clarify the question?'}.get(assessment['result'],'Actual response'),
            evaluation={'correct':'Both required elements explained on this example',
                        'partial':'Replacement value is correct, but the size explanation is wrong',
                        'incorrect':'Neither the final value nor the size is correctly explained',
                        'insufficient':'No assessable value or explanation was provided'}.get(assessment['result'],'Invalid evaluation fixture'),evidence=evidence,
            next_difficulty='applied',next_competence='Java duplicate keys',selection_reason='Continue based on this response',
            assessment=assessment,**overrides)

    def reject(self,value):
        raw=self.d.path.read_bytes()
        with self.assertRaises(ValueError):self.d.save(value)
        self.assertEqual(self.d.path.read_bytes(),raw)

    def test_all_four_results_preserve_criteria_and_coarse_evidence(self):
        for result in ['correct','partial','incorrect','insufficient']:
            with self.subTest(result=result):
                self.prompt();assessment=self.assessment(result);value=self.value(assessment)
                state=self.d.save(value);answer=state['current']['answers'][-1]
                self.assertEqual(answer['assessment'],assessment);self.assertEqual(answer['rubric'],self.rubric())
                self.assertEqual(answer['evidence'],value['evidence']);self.assertEqual(answer['prompt_revision'],value['prompt_revision'])
        answers=self.d.read()['current']['answers']
        self.assertNotEqual(answers[1]['assessment']['result'],answers[2]['assessment']['result'])

    def test_rubric_is_fixed_before_answer_and_cannot_be_replaced(self):
        rubric=self.rubric();state=self.prompt(rubric=rubric);rubric['required'][0]='Changed caller object'
        self.assertEqual(self.d.read()['current']['pending_prompt']['rubric'],self.rubric())
        value=self.value();value['rubric']=rubric;self.reject(value)
        self.reject(dict(action='prompt',competence='Java duplicate keys',prompt='Replace task',difficulty='applied',format='open',rubric=rubric))
        self.d.save(self.value());self.assertEqual(self.d.read()['current']['answers'][0]['rubric'],self.rubric())
        self.assertEqual(state['current']['question_budget']['asked'],1)

    def test_invalid_rubrics_do_not_spend_question_slot(self):
        for patch in [dict(required=[]),dict(required=['Same','Same']),dict(action=''),dict(critical_errors='wrong'),
                      dict(alternatives=[None]),dict(invented=True)]:
            with self.subTest(patch=patch):
                value=dict(action='prompt',competence='Java duplicate keys',prompt='Task',difficulty='applied',format='open',rubric=self.rubric()|patch)
                self.reject(value)
        self.assertEqual(self.d.read()['current']['question_budget']['asked'],0)

    def test_structured_prompt_requires_assessment_and_legacy_prompt_cannot_add_rubric_after_answer(self):
        self.prompt();value=self.value();value.pop('assessment');self.reject(value)
        self.d.save(dict(action='complete'))
        self.d.accept('basic');self.d.prompt()
        self.reject(self.value())
        # Legacy answers are still usable on legacy prompts.
        self.d.answer(next_difficulty='basic')
        answer=self.d.read()['current']['answers'][0]
        self.assertNotIn('assessment',answer);self.assertNotIn('rubric',answer)

    def test_independent_assisted_and_unknown_help_are_distinct(self):
        for status in ['independent','assisted','unknown']:
            self.prompt();value=self.value(self.assessment(assistance=status));state=self.d.save(value)
            answer=state['current']['answers'][-1]
            self.assertEqual(answer['assessment']['assistance']['status'],status)
            self.assertEqual(answer['evidence'],'confirmed' if status=='independent' else 'needs_practice')
        self.assertEqual(len(self.d.read()['current']['answers']),3)

    def test_help_cannot_be_claimed_independent_and_insufficient_cannot_be_confirmed(self):
        self.prompt()
        for result,status,evidence in [('correct','assisted','confirmed'),('correct','unknown','confirmed'),
                                      ('insufficient','independent','confirmed'),('partial','independent','confirmed')]:
            with self.subTest(result=result,status=status):
                value=self.value(self.assessment(result,status));value['evidence']=evidence;self.reject(value)

    def test_invalid_assessments_and_conflicting_criteria_are_rejected(self):
        self.prompt();base=self.assessment()
        cases=[base|dict(result='wrong'),base|dict(met=[True,2]),base|dict(met=[1,1]),base|dict(met=[1,3]),
               base|dict(met=[1],unmet=[1,2]),base|dict(met=[1]),base|dict(met=[1],unverified=[2]),
               base|dict(critical_errors=[1]),self.assessment('partial')|dict(unmet=[]),
               self.assessment('insufficient')|dict(unverified=[]),base|dict(assistance=dict(status='assisted',details=None)),
               base|dict(verification=dict(method='unknown',details='No check',limitations=['Unknown']))]
        for assessment in cases:
            with self.subTest(assessment=assessment):self.reject(self.value(assessment))
        value=self.value();value['assessment'].pop('verification');self.reject(value)

    def test_critical_error_can_make_response_incorrect_despite_one_met_criterion(self):
        self.prompt();assessment=self.assessment('incorrect')
        assessment.update(met=[1],unmet=[2],critical_errors=[1])
        value=self.value(assessment);value.update(answer='3, size 2: both equal keys remain',evaluation='Value is right but the saved critical error is present')
        state=self.d.save(value);self.assertEqual(state['current']['answers'][0]['assessment']['result'],'incorrect')

    def test_execution_claim_requires_recorded_run_and_review_does_not_claim_execution(self):
        self.prompt();value=self.value();value['assessment']['verification']['method']='execution';self.reject(value)
        value=self.value();value['assessment']['verification'].update(method='execution',execution=dict(command='java MapCheck',exit_code=0,output='3 / 1'))
        # This is a fixture of a reported run, not an actual Java process or verified attestation.
        state=self.d.save(value);self.assertEqual(state['current']['answers'][0]['assessment']['verification']['execution']['exit_code'],0)
        self.prompt();value=self.value();value['assessment']['verification']['execution']=dict(command='fake',exit_code=0,output='ok');self.reject(value)

    def test_unknown_verification_preserves_insufficient_without_fake_check(self):
        self.prompt();assessment=self.assessment('insufficient',assistance='unknown')
        assessment['verification']=dict(method='unknown',details='No reliable verification available',limitations=['Unverified response'])
        state=self.d.save(self.value(assessment))
        saved=state['current']['answers'][0]
        self.assertEqual(saved['evidence'],'unknown')
        self.assertEqual(saved['assessment']['result'],'insufficient')
        self.assertEqual(saved['assessment']['unverified'],[1,2])

    def test_unknown_verification_cannot_claim_a_checked_criterion(self):
        self.prompt();assessment=self.assessment('insufficient')
        assessment.update(met=[1],unverified=[2])
        assessment['verification']=dict(method='unknown',details='No checking information',limitations=['No reliable check'])
        self.reject(self.value(assessment))

    def test_skip_preserves_rubric_without_fictitious_result(self):
        self.prompt();state=self.d.save(dict(action='skip',prompt_revision=self.d.read()['current']['pending_prompt']['revision'],
            next_difficulty='applied',next_competence='Design contrast',selection_reason='Try a different competence'))
        self.assertEqual(state['current']['answers'],[])
        skip=state['current']['skips'][0];self.assertEqual(skip['rubric'],self.rubric())
        self.assertNotIn('assessment',skip);self.assertNotIn('evidence',skip)
        self.assertEqual(state['current']['question_budget']['asked'],1)

    def test_wrong_prompt_revision_cannot_receive_assessment(self):
        self.prompt();value=self.value();value['prompt_revision']-=1;self.reject(value)

    def test_nontechnical_alternative_solution_can_meet_saved_criteria(self):
        rubric=dict(action='Improve readability of a poster',required=['Identify the contrast issue','Propose a readable alternative'],
            alternatives=['Change background or text colour; another justified readable palette is acceptable'],critical_errors=[])
        self.prompt(competence='Design contrast',rubric=rubric)
        value=self.value();value.update(answer='Keep the dark background and make the text light instead of the suggested opposite palette',
            evaluation='Alternative palette meets both saved criteria',next_competence='Design contrast')
        state=self.d.save(value);self.assertEqual(state['current']['answers'][0]['assessment']['met'],[1,2])

    def test_pause_and_new_cli_process_preserve_original_rubric_and_assessment(self):
        self.prompt();self.d.save(dict(action='pause'));before=self.d.path.read_bytes()
        command=[sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'diagnostic','--course',str(self.d.f.a.course),
                 '--profile',self.d.f.a.profile,'--session','another-chat']
        result=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=20,env=dict(os.environ,PYTHONUTF8='1'))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['current']['pending_prompt']['rubric'],self.rubric())
        self.assertEqual(self.d.path.read_bytes(),before)
        self.d.save(dict(action='resume'),session='another-chat');self.d.save(self.value(self.assessment('partial')))
        before=self.d.path.read_bytes();state=self.d.read(session='third-chat')
        self.assertEqual(state['current']['answers'][0]['assessment']['result'],'partial');self.assertEqual(self.d.path.read_bytes(),before)

    def test_fifteen_structured_answers_still_complete_automatically(self):
        for _ in range(15):self.prompt();state=self.d.save(self.value())
        self.assertEqual(state['status'],'completed');self.assertEqual(state['current']['question_budget']['asked'],15)
        self.assertEqual(len(state['current']['answers']),15)
        self.assertTrue(all('assessment' in row for row in state['current']['answers']))

    def test_legacy_history_is_not_given_invented_result(self):
        self.d.save(dict(action='complete'));self.d.accept();self.d.prompt();self.d.answer()
        raw=self.d.path.read_bytes();state=self.d.read()
        self.assertNotIn('assessment',state['current']['answers'][0]);self.assertEqual(self.d.path.read_bytes(),raw)
        self.assertEqual(json.loads(raw)['schema'],'diagnostic_learning_v1')

if __name__=='__main__':unittest.main()
