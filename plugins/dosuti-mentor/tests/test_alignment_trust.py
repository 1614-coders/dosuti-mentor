"""Model-written alignment is not independent proof of a competence, even with valid citations."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
import test_target_alignment
import learning_intent as intent


class AlignmentTrust(unittest.TestCase):
    def setUp(self):
        self.f=test_target_alignment.TargetAlignment();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_p1_agreeing_supported_fields_cannot_prove_concurrency_from_hashmap(self):
        competence='Java synchronized visibility and happens-before';r=self.f.r
        r.f.diagnostic(dict(action='accept',competences=[competence],next_difficulty='basic'))
        r.f.diagnostic(dict(action='prompt',competence=competence,prompt='Does synchronized provide visibility?',difficulty='basic',format='open'))
        gap=r.f.diagnostic(dict(action='answer',prompt_revision=2,answer='It only excludes other threads; it gives no visibility guarantee',
            evaluation='Visibility guarantee is denied',evidence='unknown',next_difficulty='basic',next_competence=None,
            selection_reason='Needs visibility learning'))
        review=self.f.review();review.update(competence=competence,diagnostic_event_revisions=[gap['revision']])
        target=r.target(competence=competence,diagnostic_event_revisions=[gap['revision']],alignment_review=review)
        self.f.accept(target);self.f.solve_map();before=r.f.files()
        progress=r.read()['learning_progress'];advice=r.next();mapped=advice['steps'][0]['targets'][0]
        self.assertEqual(progress['completed_steps'],[])
        self.assertEqual(progress['demonstrated_units'],['maps'])
        self.assertEqual(progress['criterion_completed_steps'],[1])
        self.assertEqual(mapped['competence_status'],'unverified')
        self.assertEqual(mapped['outcome'],'criteria_demonstrated')
        self.assertIn(competence,advice['uncovered_diagnostic_gaps'])
        self.assertEqual(before,r.f.files())

    def test_p2_word_put_is_not_a_complete_evidence_excerpt(self):
        review=self.f.review();review['criterion_links'][0]['quote']='put';r=self.f.r
        before=r.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'complete source'):
            r.change({'proposal':r.proposal(r.target(alignment_review=review))})
        self.assertEqual(r.f.path.read_bytes(),before)

    def test_full_valid_map_excerpt_preserves_criterion_success_without_semantic_claim(self):
        self.f.accept(self.f.r.target(alignment_review=self.f.review()));self.f.solve_map()
        value=self.f.r.read();mapped=self.f.r.next()['steps'][0]['targets'][0]
        self.assertTrue(value['valid'])
        self.assertEqual(value['learning_progress']['demonstrated_units'],['maps'])
        self.assertEqual(value['learning_progress']['completed_steps'],[])
        self.assertEqual(value['learning_progress']['criterion_completed_steps'],[1])
        self.assertEqual(mapped['alignment']['status'],'unverified')
        self.assertTrue(mapped['alignment']['evidence_valid'])
        self.assertEqual(mapped['alignment']['review_verdict'],'supported')

    def test_missing_verdict_empty_or_fabricated_quotes_do_not_write(self):
        good=self.f.review();missing=copy.deepcopy(good);missing.pop('verdict')
        absent=copy.deepcopy(good);absent['verdict']=None
        for review in (missing,absent,dict(good,verdict=''),
                       dict(good,criterion_links=[dict(good['criterion_links'][0],quote='')]),
                       dict(good,criterion_links=[dict(good['criterion_links'][0],quote='A fabricated visibility guarantee.')])):
            with self.subTest(review=review):
                before=self.f.r.f.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.f.r.change({'proposal':self.f.r.proposal(self.f.r.target(alignment_review=review))})
                self.assertEqual(self.f.r.f.path.read_bytes(),before)

    def test_short_but_whole_code_line_is_evidence_not_semantic_verification(self):
        unit=self.f.f.f.f
        unit.text.write_text('x++;\n');unit.save(unit.card(criteria=['Trace increment'],sources=[dict(kind='text',role='assistant',
            notes='Short code example',path=str(unit.text),start_line=1,end_line=1)]))
        review=self.f.review();review['criterion_links'][0].update(criterion_text='Trace increment',quote='x++;')
        self.f.accept(self.f.r.target(alignment_review=review))
        alignment=self.f.r.next()['steps'][0]['targets'][0]['alignment']
        self.assertTrue(alignment['evidence_valid'])
        self.assertEqual(alignment['status'],'unverified')

    def test_real_cli_processes_preserve_partial_progress_for_corrupt_and_future_intent(self):
        r=self.f.r;self.f.accept(r.target(alignment_review=self.f.review()));self.f.solve_map()
        self.f.f.f.save(dict(recap='Map solved',next_step='Review',pending_question=None),checkpoint=True)
        script=Path(intent.__file__).with_name('course.py')
        for raw in ('{broken',json.dumps(dict(schema='future',revision=0,events=[]))):
            r.f.path.write_text(raw);before=r.f.files()
            for command in ('study-next','learn-unit','resume','status'):
                with self.subTest(raw=raw,command=command):
                    argv=[sys.executable,'-B',str(script),command,'--course',str(r.f.args().course),'--profile','default','--session','qa-cli']
                    if command=='learn-unit':argv+=['--unit-id','maps']
                    result=subprocess.run(argv,capture_output=True,text=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr)
                    value=json.loads(result.stdout);progress=value['learning_progress']
                    self.assertFalse(progress['valid']);self.assertIsNone(progress['stopped_at'])
                    self.assertEqual(progress['demonstrated_units'],['maps'])
                    if command=='study-next':self.assertEqual(value['recommendations'],[])
                    if command=='learn-unit':self.assertTrue(value['learning']['all_criteria_demonstrated'])
            self.assertEqual(before,r.f.files())

    def test_reading_existing_v6_substring_review_marks_plan_for_review_without_rewriting(self):
        self.f.accept(self.f.r.target(alignment_review=self.f.review()));self.f.solve_map()
        path=self.f.r.f.path;document=json.loads(path.read_text())
        for event in document['events']:
            proposal=event['changes'].get('proposal')
            if proposal:proposal['step_bases'][0]['targets'][0]['alignment_review']['criterion_links'][0]['quote']='put'
        path.write_text(json.dumps(document));before=self.f.r.f.files()
        view=self.f.r.read()
        self.assertTrue(view['valid']);self.assertTrue(view['plan']['needs_review'])
        self.assertEqual(view['learning_progress']['demonstrated_units'],['maps'])
        self.assertEqual(view['learning_progress']['completed_steps'],[])
        self.assertEqual(before,self.f.r.f.files())

    def test_v1_to_v6_profiles_and_independent_results_are_read_without_migration(self):
        r=self.f.r;original=json.loads(r.f.path.read_text());self.f.solve_map()
        for schema in (intent.SCHEMA,intent.CAREER_SCHEMA,intent.CONTEXT_SCHEMA,intent.BASES_SCHEMA,intent.TARGETS_SCHEMA,intent.ALIGNMENT_SCHEMA):
            with self.subTest(schema=schema):
                r.f.path.write_text(json.dumps(dict(original,schema=schema)));before=r.f.files()
                view=r.read();self.assertTrue(view['valid']);self.assertIsNone(view['plan'])
                self.assertEqual(view['goal']['description'],'Understand maps')
                self.assertEqual(view['learning_progress']['demonstrated_units'],['maps'])
                self.assertEqual(before,r.f.files())


if __name__=='__main__':unittest.main()
