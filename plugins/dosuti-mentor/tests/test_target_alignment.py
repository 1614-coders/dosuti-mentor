"""A real criterion result must not prove an unreviewed roadmap competence."""
import json
import unittest
import test_roadmap_adaptation
import learning_intent as intent


class TargetAlignment(unittest.TestCase):
    def setUp(self):
        self.r=test_roadmap_adaptation.RoadmapAdaptation();self.r.setUp();self.addCleanup(self.r.doCleanups)
        self.f=self.r.f
        self.f.f.f.text.write_text('Java HashMap\nput with an existing key replaces its value without increasing size.\n')
        self.f.f.f.save(self.f.f.f.card(title='Java HashMap',criteria=['Trace duplicate-key replacement and size']))

    def review(self,verdict='supported'):
        return dict(verdict=verdict,competence='Explain a map',diagnostic_event_revisions=[],
            diagnostic_reason='The learner confuses duplicate keys with extra entries; trace replacement.',
            criterion_links=[dict(criterion=1,criterion_text='Trace duplicate-key replacement and size',
                source=1,quote='put with an existing key replaces its value without increasing size.',
                explanation='Tracing put on the same key checks replacement and size, the exact map gap.')] if verdict=='supported' else [])

    def accept(self,target):
        self.r.change({'proposal':self.r.proposal(target)});return self.r.change({'accept_proposal':True})

    def solve_map(self):
        self.f.f.save(self.f.f.event(question='put("a",2);put("a",3): value and size?',
            answer='3 and 1: the second put replaces the existing value',details='Correct independent HashMap trace'))

    def visibility_gap(self, competence):
        self.f.diagnostic(dict(action='accept',competences=[competence],next_difficulty='basic'))
        self.f.diagnostic(dict(action='prompt',competence=competence,prompt='What visibility does synchronized guarantee?',
            difficulty='basic',format='open',rubric=dict(action='Explain synchronized visibility',
            required=['Explain the happens-before guarantee when releasing and acquiring the same monitor'],critical_errors=[])))
        return self.f.diagnostic(dict(action='answer',prompt_revision=2,answer='Only mutual exclusion, no visibility',
            evaluation='The required happens-before guarantee is denied',evidence='unknown',next_difficulty='basic',next_competence=None,
            selection_reason='Study visibility',assessment=dict(result='incorrect',met=[],unmet=[1],unverified=[],critical_errors=[],
            assistance=dict(status='independent',details=None),verification=dict(method='assistant_review',
                details='The actual response explicitly denies the required visibility guarantee',limitations=['One conceptual response']))))

    def test_unreviewed_java_mismatch_never_completes_concurrency_step(self):
        competence='Java synchronized: visibility and happens-before'
        self.visibility_gap(competence)
        self.accept(self.r.target(reviewed=False,competence=competence,diagnostic_event_revisions=[3]))
        self.solve_map();result=self.r.next();progress=self.r.read()['learning_progress']
        self.assertEqual(progress['demonstrated_units'],['maps'])
        self.assertEqual(progress['completed_steps'],[])
        self.assertIn(competence,result['uncovered_diagnostic_gaps'])
        self.assertEqual(result['steps'][0]['targets'][0]['outcome'],'alignment_unverified')
        self.assertEqual([r['action'] for r in result['recommendations']],['review_route'])

    def test_reviewed_map_mapping_retains_real_criterion_results_and_history(self):
        self.accept(self.r.target(alignment_review=self.review()))
        self.solve_map();before=self.f.files();result=self.r.next()
        self.assertEqual(result['steps'][0]['targets'][0]['outcome'],'criteria_demonstrated')
        self.assertEqual(self.r.read()['learning_progress']['criterion_completed_steps'],[1])
        self.assertEqual(self.r.read()['learning_progress']['completed_steps'],[])
        self.assertEqual(result['steps'][0]['targets'][0]['competence_status'],'unverified')
        self.assertEqual(before,self.f.files())
        self.assertEqual(json.loads(self.f.path.read_text())['schema'],intent.ALIGNMENT_SCHEMA)
        self.assertTrue(self.r.read(session='other')['valid'])

    def test_rejected_or_uncertain_alignment_cannot_use_success_as_competence(self):
        for verdict in ('unsupported','uncertain'):
            with self.subTest(verdict=verdict):
                review=self.review(verdict)
                review['diagnostic_reason']='Map replacement does not test synchronized visibility; choose concurrency material.'
                target=self.r.target(competence='Java synchronized visibility',alignment_review=review)
                review['competence']=target['competence']
                if self.r.read()['plan']:
                    self.r.change({'proposal':self.r.proposal(target,adaptation_basis=self.r.snapshot())})
                    self.r.change({'accept_proposal':True})
                else:self.accept(target)
                self.solve_map()
                self.assertEqual(self.r.read()['learning_progress']['completed_steps'],[])
                self.assertEqual(self.r.next()['steps'][0]['targets'][0]['alignment']['status'],verdict)

    def test_review_needs_actual_source_quote_and_exact_criterion(self):
        for field,value in (('quote','synchronized creates a happens-before relation'),
                            ('quote','Read selected lines'),
                            ('criterion_text','Explain thread visibility'),('source',2),('source',True),
                            ('explanation','')):
            review=self.review();review['criterion_links'][0][field]=value
            before=self.f.path.read_bytes()
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                self.r.change({'proposal':self.r.proposal(self.r.target(alignment_review=review))})
            self.assertEqual(self.f.path.read_bytes(),before)

    def test_bare_supported_flag_and_incomplete_criterion_mapping_are_rejected(self):
        for review in ({'verdict':'supported'},dict(self.review(),criterion_links=[])):
            with self.subTest(review=review),self.assertRaises(ValueError):
                self.r.change({'proposal':self.r.proposal(self.r.target(alignment_review=review))})

    def test_old_v5_targets_read_without_migration_or_loss_of_unit_success(self):
        self.accept(self.r.target(reviewed=False));self.solve_map();before=self.f.files()
        self.assertEqual(json.loads(self.f.path.read_text())['schema'],intent.TARGETS_SCHEMA)
        self.assertEqual(self.r.read()['learning_progress']['completed_steps'],[])
        self.assertEqual(before,self.f.files())

    def test_review_upgrade_is_proposed_separately_preserving_old_events_and_answers(self):
        self.accept(self.r.target(reviewed=False));self.solve_map()
        old=json.loads(self.f.path.read_text());learning=self.f.f.path.read_bytes()
        accepted=self.r.read()['plan']
        self.r.change({'proposal':self.r.proposal(self.r.target(alignment_review=self.review()),adaptation_basis=self.r.snapshot())})
        self.assertEqual(self.r.read()['plan'],accepted)
        self.r.change({'accept_proposal':True})
        self.assertEqual(self.r.read()['learning_progress']['criterion_completed_steps'],[1])
        self.assertEqual(self.r.read()['learning_progress']['completed_steps'],[])
        self.r.change({'learning_context':{'constraints':'Weekends'}})
        journal=json.loads(self.f.path.read_text())
        self.assertEqual(journal['schema'],intent.ALIGNMENT_SCHEMA)
        self.assertEqual(journal['events'][:len(old['events'])],old['events'])
        self.assertEqual(self.f.f.path.read_bytes(),learning)

    def test_all_criteria_need_evidence_and_duplicate_links_are_rejected(self):
        self.f.f.f.save(self.f.f.f.card(criteria=['Trace duplicate-key replacement and size','Explain unique keys']))
        review=self.review()
        for links in (review['criterion_links'],review['criterion_links']*2):
            with self.subTest(links=links),self.assertRaises(ValueError):
                self.r.change({'proposal':self.r.proposal(self.r.target(criteria=[1,2],alignment_review=dict(review,criterion_links=links)))})

    def test_video_review_quotes_actual_speech_not_frame_metadata(self):
        self.f.f.f.f.add('video',0,60,[(5,10,'put replaces the existing value')])
        self.f.f.f.save(self.f.f.f.card(criteria=['Trace duplicate-key replacement and size'],
            sources=[dict(kind='video',role='teacher',notes='Reviewed speech',lesson='lesson-01',start=0,end=20)]))
        review=self.review();review['criterion_links'][0]['quote']='put replaces the existing value'
        self.accept(self.r.target(alignment_review=review));self.solve_map()
        self.assertEqual(self.r.read()['learning_progress']['criterion_completed_steps'],[1])
        self.assertEqual(self.r.read()['learning_progress']['completed_steps'],[])

    def test_v5_header_cannot_claim_a_review(self):
        self.accept(self.r.target(alignment_review=self.review()))
        journal=json.loads(self.f.path.read_text());journal['schema']=intent.TARGETS_SCHEMA
        self.f.path.write_text(json.dumps(journal));before=self.f.path.read_bytes()
        self.assertFalse(self.r.read()['valid'])
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_map_review_cannot_be_reused_for_concurrency_or_other_diagnostic_answers(self):
        for changes in (dict(competence='synchronized visibility and happens-before'),
                        dict(diagnostic_event_revisions=[3])):
            target=self.r.target(alignment_review=self.review())
            target['alignment_review'].update(changes)
            before=self.f.path.read_bytes()
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'Alignment review.*target'):
                self.r.change({'proposal':self.r.proposal(target)})
            self.assertEqual(self.f.path.read_bytes(),before)

    def test_existing_unbound_v6_review_is_unverified_without_rewriting_history(self):
        target=self.r.target(alignment_review=self.review());self.accept(target);self.solve_map()
        journal=json.loads(self.f.path.read_text())
        for event in journal['events']:
            proposal=event['changes'].get('proposal')
            if proposal:
                review=proposal['step_bases'][0]['targets'][0]['alignment_review']
                review.pop('competence');review.pop('diagnostic_event_revisions')
        self.f.path.write_text(json.dumps(journal));before=self.f.files()
        self.assertTrue(self.r.read()['valid'])
        self.assertEqual(self.r.read()['learning_progress']['demonstrated_units'],['maps'])
        self.assertEqual(self.r.read()['learning_progress']['completed_steps'],[])
        self.assertEqual(self.r.next()['steps'][0]['targets'][0]['alignment']['status'],'unreviewed')
        self.assertEqual(before,self.f.files())


if __name__=='__main__':unittest.main()
