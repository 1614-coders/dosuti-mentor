"""Evidence-scoped summaries and consent-based route use; no live model evaluation."""
import json
import unittest
import test_diagnostic_assessment as fixtures
import test_roadmap_adaptation as route_fixtures
import diagnostic_learning as dl
import learning_intent as intent
import adaptive_learning as adaptive

class DiagnosticMap(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DiagnosticAssessment();self.f.setUp();self.addCleanup(self.f.doCleanups);self.d=self.f.d

    def answer(self,result='correct',difficulty='applied',help='independent'):
        current=self.d.read()['current'];number=len(current['answers'])+1
        self.d.save(dict(action='prompt',competence='Java duplicate keys',prompt='Trace Java map example '+str(number),
            difficulty=current['next_difficulty'],format='code_analysis',rubric=self.f.rubric()))
        value=self.f.value(self.f.assessment(result,help));value['next_difficulty']=difficulty
        return self.d.save(value)

    def row(self,state=None):return (state or self.d.read())['summary']['competence_map'][0]

    def test_one_success_is_partial_and_two_independent_tasks_confirm_observed_scope(self):
        state=self.answer();self.assertEqual(self.row(state)['status'],'partial')
        self.assertEqual(state['summary']['confirmed_knowledge'],[])
        state=self.answer();row=self.row(state)
        self.assertEqual(row['status'],'confirmed');self.assertEqual(row['confirmed_levels'],['applied'])
        self.assertEqual(row['scope'],'observed_criteria_and_difficulties_only')
        self.assertEqual(len(row['evidence']),2)
        self.assertEqual(state['summary']['confirmed_knowledge'],['Java duplicate keys'])
        self.assertNotIn('Java duplicate keys',[r['competence'] for r in state['summary']['recommendations']])

    def test_earlier_incorrect_is_not_erased_by_last_correct(self):
        first=self.answer('incorrect');state=self.answer()
        row=self.row(state);self.assertEqual(row['status'],'gap');self.assertTrue(row['has_conflicts'])
        self.assertIn(first['revision'],row['criteria'][0]['error_revisions'])
        self.assertIn(state['revision'],row['criteria'][0]['positive_revisions'])
        self.assertEqual(state['summary']['confirmed_knowledge'],[])
        self.assertEqual(state['summary']['identified_gaps'],['Java duplicate keys'])

    def test_reversing_contradictory_answer_order_does_not_change_status(self):
        self.answer();state=self.answer('incorrect')
        self.assertEqual(self.row(state)['status'],'gap');self.assertTrue(self.row(state)['has_conflicts'])

    def test_insufficient_and_legacy_unknown_are_not_gaps(self):
        state=self.answer('insufficient');self.assertEqual(self.row(state)['status'],'insufficient')
        self.assertEqual(state['summary']['identified_gaps'],[])
        self.assertIn('Java duplicate keys',state['summary']['unverified_areas'])
        self.d.save(dict(action='complete'));self.d.accept();self.d.prompt();self.d.answer('unknown','basic')
        state=self.d.read();row=state['summary']['competence_map'][0]
        self.assertEqual(row['status'],'insufficient');self.assertEqual(row['criteria'],[])
        self.assertIsNone(row['evidence'][0]['result']);self.assertEqual(state['summary']['identified_gaps'],[])

    def test_basic_success_does_not_prove_transfer_and_transfer_error_is_kept(self):
        self.answer(difficulty='basic');self.answer(difficulty='basic');self.answer(difficulty='transfer')
        state=self.answer('incorrect');row=self.row(state)
        self.assertEqual(row['status'],'gap');self.assertIn('basic',row['confirmed_levels'])
        self.assertNotIn('transfer',row['confirmed_levels'])
        self.assertTrue(any(r['difficulty']=='transfer' and r['error_revisions'] for r in row['criteria']))
        focus=state['summary']['recommendations'][0]['focus_criteria']
        self.assertFalse(any(r['difficulty']=='basic' for r in focus))
        self.assertTrue(any(r['difficulty']=='transfer' for r in focus))

    def test_repeated_identical_task_is_not_two_independent_examples(self):
        for _ in range(2):
            self.f.prompt();self.d.save(self.f.value())
        row=self.row()
        self.assertEqual(row['status'],'partial')
        self.assertTrue(all(c['independent_tasks']==1 for c in row['criteria']))

    def test_assisted_or_unknown_help_does_not_confirm_independent_knowledge(self):
        self.answer(help='assisted');state=self.answer(help='unknown')
        self.assertEqual(self.row(state)['status'],'partial');self.assertEqual(self.row(state)['confirmed_levels'],[])
        self.assertEqual(state['summary']['confirmed_knowledge'],[])

    def test_partial_response_preserves_actual_missing_criterion(self):
        state=self.answer('partial');row=self.row(state)
        self.assertEqual(row['status'],'gap')
        self.assertEqual(row['gaps'],[self.f.rubric()['required'][1]])
        self.assertEqual(row['evidence'][0]['result'],'partial')

    def test_skip_of_already_confirmed_criteria_does_not_erase_knowledge(self):
        self.answer();self.answer();self.f.prompt()
        state=self.d.save(dict(action='skip',prompt_revision=self.d.read()['current']['pending_prompt']['revision'],
            next_difficulty='applied',next_competence='Design contrast',selection_reason='Skip another example of covered criteria'))
        self.assertEqual(self.row(state)['status'],'confirmed')
        self.assertNotIn('Java duplicate keys',[r['competence'] for r in state['summary']['recommendations']])

    def test_skip_and_early_completion_add_limits_without_fictional_answers(self):
        self.f.prompt();state=self.d.save(dict(action='skip',prompt_revision=self.d.read()['current']['pending_prompt']['revision'],
            next_difficulty='applied',next_competence='Design contrast',selection_reason='Continue another topic'))
        state=self.d.save(dict(action='complete'));row=self.row(state)
        self.assertEqual(row['status'],'insufficient');self.assertEqual(row['evidence'],[])
        self.assertEqual(state['summary']['identified_gaps'],[])
        self.assertIn('diagnostic_completed_early',state['summary']['limitations'])

    def test_summary_and_progress_use_goal_and_do_not_write(self):
        intent.commit(self.d.f.args(expected_revision=0,reason='Learner goal'),dict(goal=dict(description='Build a Java request cache')))
        self.answer('partial');paths=[self.d.path,intent.path_for(self.d.f.a)]
        before={str(p):p.read_bytes() for p in paths};state=self.d.read(session='another-chat')
        self.assertEqual(state['route_guidance']['goal']['description'],'Build a Java request cache')
        progress=intent.view(self.d.f.a)['learning_progress']['diagnostic']
        self.assertEqual(progress['competence_map'],state['summary']['competence_map'])
        self.assertEqual(progress['goal'],state['route_guidance']['goal'])
        self.assertEqual({str(p):p.read_bytes() for p in paths},before)

class DiagnosticMapRoute(unittest.TestCase):
    def setUp(self):
        self.f=route_fixtures.RoadmapAdaptation();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def structured(self,result):
        f=self.f.f
        if dl.view(f.args())['status']=='not_started':f.diagnostic(dict(action='accept',competences=['Explain a map'],next_difficulty='applied'))
        f.diagnostic(dict(action='prompt',competence='Explain a map',prompt='A Java map example '+str(dl.view(f.args())['revision']),
            difficulty='applied',format='open',rubric=dict(action='Explain map',required=['Explain key replacement'],critical_errors=[])))
        revision=dl.view(f.args())['current']['pending_prompt']['revision']
        assessment=dict(result=result,met=[1] if result=='correct' else [],unmet=[1] if result=='incorrect' else [],
            unverified=[1] if result=='insufficient' else [],critical_errors=[],assistance=dict(status='independent',details=None),
            verification=dict(method='assistant_review',details='Check explanation',limitations=['One Java example']))
        return f.diagnostic(dict(action='answer',prompt_revision=revision,answer='Actual QA response',evaluation='Criterion-specific review',
            evidence='confirmed' if result=='correct' else 'unknown',assessment=assessment,next_difficulty='applied',
            next_competence='Explain a map',selection_reason='Another example'))

    def test_legacy_unknown_route_requests_evidence_not_relearning_a_proven_gap(self):
        f=self.f.f;f.answer()
        f.diagnostic(dict(action='prompt',competence='Explain a map',prompt='Unclear question',difficulty='applied',format='open'))
        answer=f.diagnostic(dict(action='answer',prompt_revision=4,answer='Unclear',evaluation='Insufficient information',evidence='unknown',
            next_difficulty='basic',next_competence=None,selection_reason='Pause and clarify'))
        self.f.change({'proposal':self.f.proposal(self.f.target(diagnostic_event_revisions=[answer['revision']]))})
        self.f.change({'accept_proposal':True})
        progress=self.f.read()['learning_progress']
        self.assertNotEqual(progress['next_step']['diagnostic_guidance'],'address_identified_gap')
        self.assertEqual(progress['diagnostic']['identified_gaps'],[])

    def test_route_proposal_requires_consent_and_another_chat_answer_invalidates_it(self):
        state=self.structured('incorrect');rev=state['revision']
        proposal=self.f.proposal(self.f.target(diagnostic_event_revisions=[rev]))
        state=self.f.change({'proposal':proposal});self.assertIsNone(state['plan'])
        self.structured('insufficient');before=self.f.f.path.read_bytes()
        with self.assertRaises(ValueError):self.f.change({'accept_proposal':True})
        self.assertEqual(self.f.f.path.read_bytes(),before)
        self.f.change({'proposal':proposal});accepted=self.f.change({'accept_proposal':True})
        self.assertIsNotNone(accepted['plan'])
        self.assertEqual(accepted['learning_progress']['diagnostic']['identified_gaps'],['Explain a map'])
        self.assertEqual(adaptive.study_next(self.f.f.args(limit=10))['roadmap']['uncovered_diagnostic_gaps'],['Explain a map'])

    def test_new_attempt_does_not_reinterpret_old_route_evidence(self):
        self.structured('correct');state=self.structured('correct')
        self.f.change({'proposal':self.f.proposal(self.f.target(diagnostic_event_revisions=[state['revision']]))})
        self.f.change({'accept_proposal':True})
        f=self.f.f;f.diagnostic(dict(action='complete'))
        f.diagnostic(dict(action='accept',competences=['Explain a map'],next_difficulty='applied'))
        progress=self.f.read()['learning_progress']
        self.assertEqual(progress['diagnostic']['unverified_areas'],['Explain a map'])
        self.assertEqual(progress['next_step']['diagnostic_guidance'],'advance_confirmed_scope')

    def test_confirmed_scope_does_not_default_to_reteaching_and_reads_keep_plan(self):
        self.structured('correct');state=self.structured('correct')
        self.f.change({'proposal':self.f.proposal(self.f.target(diagnostic_event_revisions=[state['revision']]))})
        accepted=self.f.change({'accept_proposal':True});before=self.f.f.files()
        result=adaptive.study_next(self.f.f.args(limit=10))
        self.assertEqual(result['learning_progress']['diagnostic']['confirmed_knowledge'],['Explain a map'])
        self.assertEqual(result['roadmap']['recommendations'][0]['action'],'check_unverified_scope')
        self.assertEqual(result['recommendations'][0]['action'],'check_unverified_scope')
        self.assertEqual(result['learning_intent']['plan'],accepted['plan'])
        self.assertEqual(before,self.f.f.files())
        self.assertTrue(result['roadmap']['proposal_guidance']['acceptance_required'])

if __name__=='__main__':unittest.main()
