"""Routes remain proposals until accepted; changed sources never erase learning."""
import copy,json,sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_career_evidence
import career_route as cr
import learning_intent as li
import unit_learning as ul

class Route(unittest.TestCase):
    def setUp(self):
        self.f=test_career_evidence.Career();self.f.setUp();
        import course
        today=course.now()[:10];self.f.save(self.f.card(as_of=today,postings=[self.f.posting(observed_on=today)]));self.f.f.f.save()
        self.path=li.path_for(self.args());self.change(dict(goal=dict(description='QA Java backend career')))
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return self.f.args(**kw)
    def change(self,changes):
        revision=li.view(self.args())['revision'];li.commit(self.args(expected_revision=revision,reason='Synthetic user decision'),changes);return li.view(self.args())
    def card(self,**kw):
        return dict(summary='QA route',sample_id='java',learner_context=dict(self_report='QA knows Java basics',available_time='QA3–5h/week',timeframe=None,unknowns=['Actual independent ability untested']),steps=[dict(id='base',title='Check fundamentals',rationale='Prerequisite, not inferred market frequency',kind='foundation',requirements=[],prerequisites=[],units=['maps']),dict(id='practice',title='Java practice',rationale='Explicit requirement in QA sample',kind='market',requirements=['java'],prerequisites=['base'],units=[])])|kw
    def save(self,card=None,revision=None):
        p=self.f.f.f.f.directory/'route.json';p.write_text(json.dumps(card or self.card()));return cr.route(self.args(route_file=p,expected_revision=li.view(self.args())['revision'] if revision is None else revision,reason='QA proposal, not real consent'))
    def test_proposal_separate_from_acceptance_and_schema_upgrade(self):
        before=json.loads(self.path.read_text());self.assertEqual(before['schema'],li.SCHEMA)
        x=self.save();self.assertIsNone(x['intent']['plan']);self.assertTrue(x['proposal']['valid']);self.assertEqual(json.loads(self.path.read_text())['schema'],li.CAREER_SCHEMA)
        self.assertEqual(json.loads(self.path.read_text())['events'][0],before['events'][0])
        self.change(dict(accept_proposal=True));x=cr.route(self.args());self.assertTrue(x['plan']['valid']);self.assertIsNone(x['intent']['proposal'])
    def test_route_passes_step_bases_to_ordinary_proposal(self):
        bases=[dict(unit_ids=['maps']),dict(prerequisites=[1])]
        result=self.save(self.card(step_bases=bases))
        self.assertEqual(result['intent']['proposal']['step_bases'],bases)
        self.assertTrue(result['intent']['proposal']['basis_status']['valid'])
        self.assertEqual(result['intent']['proposal']['route_state'],'proposed')
        self.change(dict(accept_proposal=True))
        self.assertEqual(li.view(self.args())['plan']['step_bases'],bases)
        self.assertEqual(json.loads(self.path.read_text())['schema'],li.BASES_SCHEMA)

    def test_market_zero_support_refused_foundation_without_sample_allowed(self):
        self.f.save(self.f.card(postings=[self.f.posting(status='archived')]))
        with self.assertRaises(ValueError):self.save()
        card=self.card(sample_id=None);card['steps']=card['steps'][:1];self.assertTrue(self.save(card)['proposal']['valid'])
    def test_new_sample_marks_accepted_route_but_keeps_plan(self):
        self.save();self.change(dict(accept_proposal=True));self.f.save(self.f.card(title='Updated market sample'))
        x=cr.route(self.args());self.assertTrue(x['plan']['needs_review']);self.assertIsNotNone(x['intent']['plan'])
    def test_stale_proposal_cannot_be_accepted(self):
        self.save();before=self.path.read_bytes();self.f.save(self.f.card(title='Changed'))
        with self.assertRaises(ValueError):self.change(dict(accept_proposal=True))
        self.assertEqual(before,self.path.read_bytes())
    def test_review_withdrawal_marks_route_for_review(self):
        self.save();self.f.f.save(self.f.f.review(verdict='excluded',version_fit='unknown'));self.assertTrue(cr.route(self.args())['proposal']['needs_review'])
    def test_unit_change_blocks_acceptance(self):
        self.save();self.f.f.f.save(self.f.f.f.card(title='New unit'))
        with self.assertRaises(ValueError):self.change(dict(accept_proposal=True))
    def test_goal_change_does_not_replace_accepted_plan(self):
        self.save();self.change(dict(accept_proposal=True));self.change(dict(goal=dict(description='Another QA goal')))
        x=cr.route(self.args());self.assertTrue(x['plan']['needs_review']);self.assertTrue(x['intent']['plan']['needs_review'])
    def test_unknown_prerequisite_or_forward_reference_rejected(self):
        for dependency in ['missing','practice']:
            card=self.card();card['steps'][0]['prerequisites']=[dependency]
            with self.subTest(dependency=dependency),self.assertRaises(ValueError):self.save(card)
    def test_unknown_requirement_rejected(self):
        card=self.card();card['steps'][1]['requirements']=['missing']
        with self.assertRaises(ValueError):self.save(card)
    def test_unknown_unit_rejected(self):
        card=self.card();card['steps'][0]['units']=['missing']
        with self.assertRaises(ValueError):self.save(card)
    def test_stale_intent_revision_refused(self):
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.save(revision=0)
        self.assertEqual(before,self.path.read_bytes())
    def test_legacy_header_cannot_claim_career_metadata(self):
        self.save();d=json.loads(self.path.read_text());d['schema']=li.SCHEMA;self.path.write_text(json.dumps(d));self.assertFalse(li.view(self.args())['valid'])
    def test_proposal_does_not_create_grades(self):
        self.save();x=cr.route(self.args());self.assertFalse(x['proposal']['progress']['maps']['all_criteria_demonstrated']);self.assertFalse((self.path.parent/'unit-learning.json').exists())
    def test_other_profile_does_not_inherit_route(self):
        self.save();self.assertIsNone(cr.route(self.args(profile='friend'))['intent']['proposal'])
    def test_revised_proposal_preserves_previous_plan_until_acceptance(self):
        self.save();self.change(dict(accept_proposal=True));old=li.view(self.args())['plan'];self.save(self.card(summary='New proposed route'))
        x=li.view(self.args());self.assertEqual(x['plan'],old);self.assertEqual(x['proposal']['summary'],'New proposed route')
    def test_unit_progress_shared_and_not_modified_by_route(self):
        root=self.f.root;unit=self.f.f.f.get();p=self.f.f.f.f.directory/'answer.json';p.write_text(json.dumps(dict(stage='solved_independently',details='QA',question='QA?',answer='QA response',hints=0,criteria=[1])))
        ul.learn_unit(self.args(unit_id='maps',event_file=p,expected_revision=0,expected_unit_sha256=unit['content_sha256']))
        journal=self.path.parent/'unit-learning.json';before=journal.read_bytes();self.save();x=cr.route(self.args())
        self.assertTrue(x['proposal']['progress']['maps']['all_criteria_demonstrated']);self.assertEqual(before,journal.read_bytes())
    def test_plan_source_change_marks_session_task(self):
        self.save();self.change(dict(accept_proposal=True));li.commit(self.args(session='a',reason='QA task',expected_revision=3),dict(session='Practice'))
        self.f.save(self.f.card(title='Changed sample'));self.assertTrue(li.view(self.args(session='a'))['session_task']['needs_review'])

    def test_new_goal_independent_task_not_bound_to_old_route(self):
        self.save();self.change(dict(accept_proposal=True));self.change(dict(goal=dict(description='New independent QA goal')))
        current=li.view(self.args())['revision'];li.commit(self.args(session='a',reason='QA independent task',expected_revision=current),dict(session='Independent study'))
        task=li.view(self.args(session='a'))['session_task'];self.assertIsNone(task['plan_revision']);self.assertFalse(task['needs_review'])

    def test_malformed_legacy_changes_return_invalid_without_crashing(self):
        data=json.loads(self.path.read_text());data['events'][0]['changes']=None;self.path.write_text(json.dumps(data));before=self.path.read_bytes()
        self.assertFalse(li.view(self.args())['valid']);self.assertEqual(before,self.path.read_bytes())
