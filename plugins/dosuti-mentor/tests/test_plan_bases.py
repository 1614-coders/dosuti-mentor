"""Plan bases cite existing facts without accepting or rebuilding a curriculum."""
import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import adaptive_learning
import diagnostic_learning as dl
import learning_intent as li
import test_unit_learning


class PlanBases(unittest.TestCase):
    def setUp(self):
        self.f=test_unit_learning.UnitLearning();self.f.setUp()
        self.path=li.path_for(self.args())
        self.change({'goal':{'description':'Understand maps'}})
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return self.f.f.args(**kw)
    def read(self,**kw):return li.view(self.args(**kw))
    def change(self,value):
        li.commit(self.args(expected_revision=self.read()['revision'],reason='QA learner decision'),value)
        return self.read()
    def proposal(self,**kw):
        return dict(summary='Maps and practice',steps=['Study maps','Practice'],
                    step_bases=[dict(unit_ids=['maps']),dict(prerequisites=[1])])|kw
    def diagnostic(self,value):
        path=self.f.f.f.directory/'diagnostic-input.json';path.write_text(json.dumps(value))
        return dl.diagnostic(self.args(update_file=path,expected_revision=dl.view(self.args())['revision'],reason='QA diagnostic'))
    def answer(self):
        self.diagnostic(dict(action='accept',competences=['Explain a map'],next_difficulty='basic'))
        self.diagnostic(dict(action='prompt',competence='Explain a map',prompt='What is stored?',difficulty='basic',format='open'))
        return self.diagnostic(dict(action='answer',prompt_revision=2,answer='Keys and values',
            evaluation='This example is correct',evidence='confirmed',next_difficulty='applied',
            next_competence='Explain a map',selection_reason='Basic contents identified; next check applying a map to an example'))
    def files(self):
        root=self.f.f.f.path
        return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def test_proposal_acceptance_is_separate_and_preserves_bases(self):
        result=self.change({'proposal':self.proposal()})
        self.assertIsNone(result['plan']);self.assertEqual(result['proposal']['route_state'],'proposed')
        self.assertTrue(result['proposal']['basis_status']['valid'])
        result=self.change({'accept_proposal':True})
        self.assertEqual(result['plan']['route_state'],'accepted')
        self.assertEqual(result['plan']['step_bases'],self.proposal()['step_bases'])
        self.assertEqual(json.loads(self.path.read_text())['schema'],li.BASES_SCHEMA)
        self.assertFalse(self.f.path.exists())
        self.assertFalse(dl.path_for(self.args()).exists())

    def test_declined_diagnostic_still_allows_plan(self):
        self.diagnostic({'action':'decline'});before=dl.path_for(self.args()).read_bytes()
        self.change({'proposal':self.proposal()});self.change({'accept_proposal':True})
        self.assertEqual(dl.path_for(self.args()).read_bytes(),before)

    def test_diagnostic_references_expose_actual_answer_not_self_report(self):
        self.answer()
        value=self.proposal(step_bases=[dict(diagnostic_event_revisions=[3]),dict(prerequisites=[1])])
        result=self.change({'proposal':value})['proposal']
        self.assertEqual(result['basis_status']['diagnostic_evidence']['3']['answer'],'Keys and values')
        self.change({'accept_proposal':True})
        self.assertEqual(dl.view(self.args())['revision'],3)

    def test_missing_prompt_or_other_profile_answer_cannot_be_basis(self):
        self.answer();before=self.path.read_bytes()
        for revision in (1,2,99):
            with self.subTest(revision=revision),self.assertRaisesRegex(ValueError,'diagnostic answer'):
                self.change({'proposal':self.proposal(step_bases=[dict(diagnostic_event_revisions=[revision]),dict(prerequisites=[1])])})
            self.assertEqual(self.path.read_bytes(),before)
        value=self.proposal(step_bases=[dict(diagnostic_event_revisions=[3]),dict(prerequisites=[1])])
        li.commit(self.args(profile='friend',expected_revision=0,reason='QA goal'),{'goal':{'description':'Maps'}})
        with self.assertRaises(ValueError):
            li.commit(self.args(profile='friend',expected_revision=1,reason='QA proposal'),{'proposal':value})

    def test_missing_or_stale_units_cannot_be_proposed_or_accepted(self):
        before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'Missing study unit'):
            self.change({'proposal':self.proposal(step_bases=[dict(unit_ids=['missing']),dict(prerequisites=[1])])})
        self.assertEqual(self.path.read_bytes(),before)
        self.change({'proposal':self.proposal()})
        source=self.f.f.get()['sources'][0]
        snapshot=self.f.f.f.path/source['snapshot'];snapshot.chmod(0o600);snapshot.write_text('changed')
        before=self.path.read_bytes();self.assertTrue(self.read()['proposal']['needs_review'])
        with self.assertRaisesRegex(ValueError,'Study unit needs review'):self.change({'accept_proposal':True})
        self.assertEqual(self.path.read_bytes(),before)

    def test_changed_goal_keeps_proposal_diagnostic_and_learning_history(self):
        self.answer();self.f.save();self.change({'proposal':self.proposal()})
        learning=self.f.path.read_bytes();diagnostic=dl.path_for(self.args()).read_bytes()
        history=self.read(history_limit=100)['history']
        self.change({'goal':{'description':'Build a project'}})
        self.assertTrue(self.read()['proposal']['needs_review'])
        with self.assertRaises(ValueError):self.change({'accept_proposal':True})
        self.assertEqual(self.read(history_limit=100)['history'][:len(history)],history)
        self.assertEqual(self.f.path.read_bytes(),learning);self.assertEqual(dl.path_for(self.args()).read_bytes(),diagnostic)
        self.change({'proposal':self.proposal(summary='New proposed route')})
        self.assertFalse(self.read()['proposal']['needs_review']);self.assertIsNone(self.read()['plan'])

    def test_read_only_next_keeps_all_files_and_proposal(self):
        self.answer();self.f.save();self.change({'proposal':self.proposal()})
        before=self.files();result=adaptive_learning.study_next(self.args(limit=5))
        self.assertEqual(result['learning_intent']['proposal']['step_bases'],self.proposal()['step_bases'])
        self.assertIsNone(result['learning_intent']['plan']);self.assertEqual(self.files(),before)

    def test_legacy_proposal_is_preliminary_without_migration(self):
        proposal=self.proposal();proposal.pop('step_bases')
        self.change({'proposal':proposal});before=self.path.read_bytes()
        self.assertEqual(self.read()['proposal']['route_state'],'preliminary')
        self.assertEqual(self.path.read_bytes(),before)
        self.change({'accept_proposal':True});self.assertEqual(self.read()['plan']['route_state'],'accepted')
        self.assertEqual(json.loads(self.path.read_text())['schema'],li.SCHEMA)

    def test_invalid_bases_do_not_write(self):
        before=self.path.read_bytes()
        for bases in ([],[{}],[{},{}],[dict(prerequisites=[1]),dict(prerequisites=[1])],
                      [dict(unit_ids=['maps']),dict(prerequisites=[2])],
                      [dict(unit_ids=['maps']),dict(prerequisites=[True])],
                      [dict(unit_ids=['maps','maps']),dict(prerequisites=[1])],
                      [dict(diagnostic_event_revisions=[False]),dict(prerequisites=[1])],
                      [dict(self_report=['Expert']),dict(prerequisites=[1])]):
            with self.subTest(bases=bases),self.assertRaises(ValueError):
                self.change({'proposal':self.proposal(step_bases=bases)})
            self.assertEqual(self.path.read_bytes(),before)

    def test_stale_accepted_bases_mark_plan_and_session_without_erasing_attempts(self):
        self.f.save();self.change({'proposal':self.proposal()});self.change({'accept_proposal':True})
        li.commit(self.args(session='a',expected_revision=self.read()['revision'],reason='QA task'),{'session':'Practice maps'})
        before=self.f.path.read_bytes();source=self.f.f.get()['sources'][0]
        snapshot=self.f.f.f.path/source['snapshot'];snapshot.chmod(0o600);snapshot.write_text('changed')
        result=self.read(session='a')
        self.assertTrue(result['plan']['needs_review']);self.assertTrue(result['session_task']['needs_review'])
        self.assertEqual(self.f.path.read_bytes(),before)
        self.assertEqual(result['plan']['route_state'],'accepted')

    def test_legacy_headers_cannot_claim_bases_and_v4_keeps_context(self):
        self.change({'learning_context':{'self_assessment':'Beginner'}})
        self.change({'proposal':self.proposal()});journal=json.loads(self.path.read_text())
        self.change({'learning_context':{'constraints':'Weekends'}})
        self.assertEqual(json.loads(self.path.read_text())['schema'],li.BASES_SCHEMA)
        self.assertEqual(self.read()['learning_context']['self_assessment'],'Beginner')
        for schema in (li.SCHEMA,li.CAREER_SCHEMA,li.CONTEXT_SCHEMA):
            self.path.write_text(json.dumps(dict(copy.deepcopy(journal),schema=schema)))
            before=self.path.read_bytes();self.assertFalse(self.read()['valid']);self.assertEqual(self.path.read_bytes(),before)

    def test_missing_diagnostic_after_proposal_requires_review_without_rewrite(self):
        self.answer()
        self.change({'proposal':self.proposal(step_bases=[dict(diagnostic_event_revisions=[3]),dict(prerequisites=[1])])})
        dl.path_for(self.args()).unlink();before=self.path.read_bytes()
        self.assertTrue(self.read()['proposal']['needs_review'])
        with self.assertRaises(ValueError):self.change({'accept_proposal':True})
        self.assertEqual(self.path.read_bytes(),before)
