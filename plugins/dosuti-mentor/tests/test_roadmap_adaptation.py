"""Learning targets bind route advice to actual, versioned criterion evidence."""
import copy
import json
import unittest
from unittest.mock import patch
import test_plan_bases
import learning_intent as intent
import diagnostic_learning as diagnostic
import adaptive_learning as adaptive


class RoadmapAdaptation(unittest.TestCase):
    def setUp(self):
        self.f=test_plan_bases.PlanBases();self.f.setUp();self.addCleanup(self.f.tearDown)
    def read(self,**kw):return intent.view(self.f.args(**kw))
    def target(self,reviewed=True,**kw):
        unit=self.f.f.f.get()
        target=dict(competence='Explain a map',rationale='Practise the map criterion on a new example',
            unit_id='maps',unit_sha256=unit['content_sha256'],criteria=[1],diagnostic_event_revisions=[])|kw
        # Synthetic contract fixtures; semantic Java acceptance is test_target_alignment.
        if reviewed and 'alignment_review' not in target:
            target['alignment_review']=dict(verdict='supported',competence=target['competence'],
                diagnostic_event_revisions=target['diagnostic_event_revisions'],diagnostic_reason='Map evidence is checked with a fresh map task.',
                criterion_links=[dict(criterion=i,criterion_text=unit['criteria'][i-1] if type(i) is int and 1<=i<=len(unit['criteria']) else 'Invalid',
                    source=1,quote=unit['source_contexts'][0]['text'],explanation='The selected map material supports this map criterion.')
                    for i in target['criteria']])
        return target
    def proposal(self,target=None,**kw):
        target=self.target() if target is None else target
        basis=dict(unit_ids=['maps'],targets=[target])
        if target['diagnostic_event_revisions']:basis['diagnostic_event_revisions']=target['diagnostic_event_revisions']
        return dict(summary='Maps competence',steps=['Apply maps'],step_bases=[basis])|kw
    def change(self,value):return self.f.change(value)
    def accept(self):
        self.change({'proposal':self.proposal()});return self.change({'accept_proposal':True})
    def next(self):return adaptive.study_next(self.f.args(session='b',limit=10))['roadmap']
    def snapshot(self):
        return dict(base_plan_revision=self.read()['plan']['accepted_revision'],
            diagnostic_revision=diagnostic.view(self.f.args())['revision'],
            unit_learning_revision=self.f.f.read()['learning']['revision'])

    def test_targets_persist_with_existing_journal_and_optional_diagnostic(self):
        accepted=self.accept()
        self.assertEqual(json.loads(self.f.path.read_text())['schema'],intent.ALIGNMENT_SCHEMA)
        self.assertEqual(accepted['plan']['step_bases'][0]['targets'][0],self.target())
        before=self.f.files();other=self.read(session='another')
        self.assertEqual(other['plan'],accepted['plan']);self.assertEqual(before,self.f.files())

    def test_unrelated_diagnostic_competence_and_bad_criterion_are_rejected(self):
        self.f.answer();before=self.f.path.read_bytes()
        for target in (self.target(competence='Write HTTP handler',diagnostic_event_revisions=[3]),
                       self.target(criteria=[2]),self.target(unit_sha256='a'*64)):
            with self.subTest(target=target),self.assertRaises(ValueError):self.change({'proposal':self.proposal(target)})
            self.assertEqual(self.f.path.read_bytes(),before)

    def test_invalid_target_fields_and_snapshots_preserve_existing_journal(self):
        before=self.f.path.read_bytes()
        proposals=[self.proposal(self.target(criteria=[True])),self.proposal(self.target(rationale='')),
                   self.proposal(self.target(criteria=[])),self.proposal(self.target(diagnostic_event_revisions=[True])),
                   self.proposal(adaptation_basis={'base_plan_revision':None,'diagnostic_revision':True,'unit_learning_revision':0})]
        malformed=self.proposal();malformed['step_bases']=[{'targets':[self.target()],'unit_ids':None}]
        proposals.append(malformed)
        for proposal in proposals:
            with self.subTest(proposal=proposal),self.assertRaises(ValueError):self.change({'proposal':proposal})
            self.assertEqual(self.f.path.read_bytes(),before)

    def test_explanation_assistance_errors_and_success_drive_distinct_advice(self):
        self.accept();self.assertEqual(self.next()['recommendations'][0]['action'],'initial_check')
        self.f.f.save(self.f.f.event(stage='explained',question=None,answer=None,criteria=[]))
        self.assertEqual(self.next()['recommendations'][0]['action'],'check_understanding')
        self.assertFalse(self.read()['learning_progress']['completed_steps'])
        self.f.f.save(self.f.f.event(stage='practice_with_help',hints=1))
        self.assertEqual(self.next()['recommendations'][0]['action'],'fresh_independent_task')
        self.f.f.save(self.f.f.event(stage='needs_review',error_tags=['concept']))
        advice=self.next()['recommendations'][0]
        self.assertEqual(advice['action'],'review_error');self.assertEqual(advice['competence'],'Explain a map')
        self.assertIsNotNone(advice['evidence_revision'])
        self.f.f.save();self.assertEqual(self.read()['learning_progress']['criterion_completed_steps'],[1])
        self.assertEqual(self.read()['learning_progress']['completed_steps'],[])
        self.assertEqual(self.next()['steps'][0]['targets'][0]['outcome'],'criteria_demonstrated')
        self.f.f.save(self.f.f.event(stage='needs_review'))
        self.assertEqual(self.next()['recommendations'][0]['action'],'review_error')

    def test_legacy_uncertainty_is_preserved_without_inventing_a_gap(self):
        self.f.answer()
        self.f.diagnostic(dict(action='prompt',competence='Explain a map',prompt='Trace duplicate keys',difficulty='applied',format='open'))
        gap=self.f.diagnostic(dict(action='answer',prompt_revision=4,answer='I cannot trace it',evaluation='Key replacement is not understood',
            evidence='unknown',next_difficulty='basic',next_competence=None,selection_reason='Teach map replacement'))
        self.change({'proposal':self.proposal(self.target(diagnostic_event_revisions=[gap['revision']]))})
        self.change({'accept_proposal':True})
        self.assertEqual(self.read()['learning_progress']['diagnostic']['identified_gaps'],[])
        before=diagnostic.path_for(self.f.args()).read_bytes();self.f.f.save()
        self.assertEqual(self.next()['steps'][0]['targets'][0]['outcome'],'criteria_demonstrated')
        self.assertEqual(self.next()['uncovered_diagnostic_gaps'],[])
        self.assertEqual(diagnostic.path_for(self.f.args()).read_bytes(),before)

    def test_targeted_criteria_allow_next_topic_without_claiming_entire_unit_mastered(self):
        self.f.f.f.save(self.f.f.f.card(criteria=['Explain map','Implement hash function']))
        first=self.target(criteria=[1]);second=self.target(criteria=[2])
        self.change({'proposal':dict(summary='Two competencies',steps=['Explain','Implement'],step_bases=[
            dict(unit_ids=['maps'],targets=[first]),dict(unit_ids=['maps'],targets=[second],prerequisites=[1])])})
        self.change({'accept_proposal':True})
        before=self.next()
        self.assertEqual({row['step'] for row in before['recommendations'] if 'step' in row}, {1})
        self.assertTrue(any(row['action']=='review_route' for row in before['recommendations']))
        self.f.f.save()
        after=self.read()['learning_progress']
        self.assertEqual(after['criterion_completed_steps'],[1]);self.assertEqual(after['next_step']['index'],2)
        self.assertEqual(after['completed_steps'],[])
        self.assertEqual(after['demonstrated_units'],[])
        self.assertEqual(self.next()['recommendations'][0]['step'],2)

    def test_repeat_and_user_skip_reuse_existing_policy_without_changing_route(self):
        self.accept()
        with patch('course.now',return_value='2026-01-01T00:00:00Z'):self.f.f.save()
        accepted=self.read()['plan']
        with patch('course.now',return_value='2026-01-02T00:00:00Z'):
            self.assertEqual(self.next()['recommendations'][0]['action'],'recheck_due')
        import unit_learning
        state=self.f.f.read();path=self.f.f.f.f.directory/'skip.json'
        path.write_text(json.dumps(dict(choice='skip',criteria=[1],until=None,reason='Learner chose to skip')))
        unit_learning.learn_unit(self.f.f.args(review_file=path,expected_revision=state['learning']['revision'],
            expected_unit_sha256=state['unit']['content_sha256']))
        self.assertTrue(all(row['action']=='review_route' for row in self.next()['recommendations']))
        self.assertEqual(self.read()['plan'],accepted)

    def test_new_diagnostic_or_goal_invalidates_pending_adaptation(self):
        self.accept();self.change({'proposal':self.proposal(adaptation_basis=self.snapshot())})
        self.f.diagnostic(dict(action='decline'))
        self.assertTrue(self.read()['proposal']['needs_review'])
        with self.assertRaises(ValueError):self.change({'accept_proposal':True})
        self.change({'proposal':self.proposal(adaptation_basis=self.snapshot())})
        self.change({'goal':dict(description='New goal')})
        with self.assertRaises(ValueError):self.change({'accept_proposal':True})

    def test_legacy_uncertainty_keeps_route_review_without_inventing_gap_mapping(self):
        self.f.answer()
        self.f.diagnostic(dict(action='prompt',competence='Explain a map',prompt='Trace duplicate keys',difficulty='applied',format='open'))
        self.f.diagnostic(dict(action='answer',prompt_revision=4,answer='Unsure',evaluation='Not understood',evidence='unknown',
            next_difficulty='basic',next_competence=None,selection_reason='Teach replacement'))
        self.change({'proposal':self.proposal(self.target(competence='Explain HTTP routing'))})
        self.change({'accept_proposal':True})
        result=self.next()
        self.assertEqual(result['uncovered_diagnostic_gaps'],[])
        self.assertTrue(any(row['action']=='review_route' for row in result['recommendations']))

    def test_legacy_plan_recommendation_does_not_write_or_fabricate_targets(self):
        self.change({'proposal':self.f.proposal()});self.change({'accept_proposal':True})
        before=self.f.files();result=self.next()
        self.assertEqual(result['status'],'legacy_unmapped')
        self.assertTrue(all(not step['targets'] for step in result['steps']))
        self.assertEqual(self.f.files(),before)

    def test_changed_unit_between_reads_returns_review_instead_of_crashing(self):
        self.accept();accepted=self.read()
        import unit_learning
        journal=unit_learning.load(self.f.f.path,compact=True)
        result=adaptive.roadmap_recommendations(accepted,{},journal,'2026-01-01T00:00:00Z')
        self.assertEqual(result['status'],'needs_review')

    def test_unchanged_proposal_accepts_and_v5_survives_later_context_write(self):
        self.accept();self.change({'proposal':self.proposal(adaptation_basis=self.snapshot(),summary='Revised learning order')})
        self.change({'accept_proposal':True})
        self.change({'learning_context':{'constraints':'Two evenings'}})
        self.assertEqual(self.read()['plan']['summary'],'Revised learning order')
        self.assertEqual(json.loads(self.f.path.read_text())['schema'],intent.ALIGNMENT_SCHEMA)

    def test_first_targeted_proposal_is_sealed_against_new_answers_before_acceptance(self):
        proposal=self.proposal();unchanged=copy.deepcopy(proposal)
        self.change({'proposal':proposal})
        self.assertEqual(proposal,unchanged)
        self.assertIsNone(self.read()['proposal']['adaptation_basis']['base_plan_revision'])
        self.f.f.save(session='another-chat')
        self.assertTrue(self.read()['proposal']['needs_review'])
        with self.assertRaisesRegex(ValueError,'adaptation'):self.change({'accept_proposal':True})

    def test_version_change_blocks_pending_proposal_even_without_new_assessment(self):
        self.change({'proposal':self.proposal()})
        self.f.f.f.save(self.f.f.f.card(title='Updated learning content'))
        self.assertTrue(self.read()['proposal']['needs_review'])
        with self.assertRaises(ValueError):self.change({'accept_proposal':True})

    def test_advice_reads_do_not_modify_plan_or_other_stores(self):
        self.accept();self.f.f.save(self.f.f.event(stage='needs_review'))
        before=self.f.files();result=self.next()
        self.assertEqual(result['recommendations'][0]['action'],'review_error')
        self.assertEqual(before,self.f.files())

    def test_career_route_uses_same_targets_and_adaptation_proposal(self):
        import test_career_route,study_units
        route=test_career_route.Route();route.setUp();self.addCleanup(route.tearDown)
        registry=study_units.load(route.args().course)
        target=self.target(unit_sha256=registry['units']['maps']['content_sha256'])
        bases=[dict(unit_ids=['maps'],targets=[target]),dict(prerequisites=[1])]
        route.save(route.card(step_bases=bases));route.change(dict(accept_proposal=True))
        accepted=intent.view(route.args())['plan']
        snapshot=dict(base_plan_revision=accepted['accepted_revision'],diagnostic_revision=0,unit_learning_revision=0)
        proposed=route.save(route.card(step_bases=bases,adaptation_basis=snapshot,summary='Proposed career revision'))
        self.assertEqual(proposed['intent']['plan'],accepted)
        self.assertEqual(proposed['intent']['proposal']['adaptation_basis'],snapshot)
        route.change(dict(accept_proposal=True))
        self.assertEqual(intent.view(route.args())['plan']['summary'],'Proposed career revision')

    def test_proposal_does_not_replace_plan_and_new_chat_answer_blocks_acceptance(self):
        self.accept();accepted=self.read()['plan'];basis=self.snapshot()
        self.change({'proposal':self.proposal(summary='More practice',adaptation_basis=basis)})
        self.assertEqual(self.read()['plan'],accepted)
        self.f.f.save(session='another-chat')
        before=self.f.path.read_bytes()
        self.assertTrue(self.read()['proposal']['needs_review'])
        with self.assertRaisesRegex(ValueError,'adaptation'):self.change({'accept_proposal':True})
        self.assertEqual(self.f.path.read_bytes(),before)
        self.change({'proposal':self.proposal(summary='Updated proposal',adaptation_basis=self.snapshot())})
        self.change({'accept_proposal':True})
        self.assertEqual(self.read()['plan']['summary'],'Updated proposal')

    def test_targeted_replacement_requires_explicit_adaptation_basis(self):
        self.accept();before=self.f.path.read_bytes()
        with self.assertRaises(ValueError):self.change({'proposal':self.proposal(summary='Replacement')})
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_declined_diagnostic_and_legacy_plan_stay_readable(self):
        self.f.diagnostic(dict(action='decline'));self.accept()
        self.assertEqual(self.next()['status'],'ready')
        self.assertFalse(diagnostic.view(self.f.args())['current']['answers'])

    def test_changed_sources_block_advice_not_learner_results(self):
        self.accept();self.f.f.save()
        source=self.f.f.f.get()['sources'][0];path=self.f.f.f.f.path/source['snapshot']
        path.chmod(0o600);path.write_text('Damaged')
        result=self.next()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(result['recommendations'][0]['action'],'review_route')

    def test_old_headers_cannot_claim_targets(self):
        self.accept();journal=json.loads(self.f.path.read_text())
        for schema in (intent.SCHEMA,intent.CAREER_SCHEMA,intent.CONTEXT_SCHEMA,intent.BASES_SCHEMA,intent.TARGETS_SCHEMA):
            self.f.path.write_text(json.dumps(dict(copy.deepcopy(journal),schema=schema)))
            self.assertFalse(self.read()['valid'])


if __name__=='__main__':unittest.main()
