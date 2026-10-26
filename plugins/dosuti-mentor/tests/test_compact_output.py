"""Compact responses preserve decisions; validated historical evidence is addressable."""
import copy,json,os,subprocess,sys,unittest
from pathlib import Path
import test_unit_learning as units
import test_diagnostic_policy_v2 as diagnoses
import test_adaptive_practice as practice
import test_lesson_context as contexts
PLUGIN=Path(__file__).resolve().parents[1]

class CompactOutput(unittest.TestCase):
    def setUp(self):
        self.f=units.UnitLearning();self.f.setUp();self.addCleanup(self.f.tearDown)
    def cli(self,command,*extra,course=None,ok=True):
        r=subprocess.run([sys.executable,'-B',str(PLUGIN/'scripts/course.py'),command,'--course',str(course or self.f.args().course),*map(str,extra)],capture_output=True,text=True,timeout=30,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        if ok:self.assertEqual(r.returncode,0,r.stderr);return json.loads(r.stdout)
        self.assertNotEqual(r.returncode,0);self.assertNotIn('Traceback',r.stderr);return r
    def files(self):
        root=self.f.args().course
        return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    def test_settings_keep_answers_preferences_and_message_without_duplicate_layers(self):
        full=self.cli('settings');before=self.files();short=self.cli('settings','--compact')
        for key in ('effective','onboarding','onboarding_answers','onboarding_next','onboarding_message','checks_source'):self.assertEqual(short[key],full[key])
        self.assertNotIn('layers',short);self.assertEqual(short['output_view'],'compact_v1')
        self.assertEqual(self.files(),before);self.assertLess(len(json.dumps(short)),len(json.dumps(full)))
    def test_diagnostic_preserves_rubric_assistance_summary_and_selection(self):
        d=diagnoses.DiagnosticPolicyV2();d.setUp();self.addCleanup(d.doCleanups)
        d.accept(competences=['Keys'],competence_prerequisites={});d.ask();d.answer('partial',following='Keys')
        full=self.cli('diagnostic',course=d.d.f.a.course);raw=d.d.path.read_bytes();short=self.cli('diagnostic','--compact',course=d.d.f.a.course)
        self.assertEqual(short['summary'],full['summary']);self.assertEqual(short['selection_guidance'],full['selection_guidance'])
        self.assertEqual(short['current']['next_difficulty'],full['current']['next_difficulty'])
        self.assertNotIn('answers',short['current']);self.assertNotIn('attempts',short)
        self.assertEqual(short['attempt_count'],len(full['attempts']));self.assertEqual(d.d.path.read_bytes(),raw)
        d.ask();full=self.cli('diagnostic',course=d.d.f.a.course);short=self.cli('diagnostic','--compact',course=d.d.f.a.course)
        self.assertEqual(short['current']['pending_prompt'],full['current']['pending_prompt'])
    def test_unit_preserves_sources_scoring_and_read_only_evidence(self):
        self.f.save();full=self.cli('learn-unit','--unit-id','maps','--session','a');before=self.files()
        short=self.cli('learn-unit','--unit-id','maps','--session','a','--compact')
        self.assertEqual(short['unit'],full['unit'])
        for key in ('revision','criteria','adaptive','topic_summary','all_criteria_demonstrated','practice'):self.assertEqual(short['learning'][key],full['learning'][key])
        self.assertNotIn('unit_history',short['learning_progress']);self.assertNotIn('learning_progress',short['learning_intent'])
        self.assertEqual(short['evidence'][0]['value']['answer'],'Keys and values');self.assertEqual(self.files(),before)
    def test_compact_write_keeps_cas_and_explanation_does_not_prove_mastery(self):
        state=self.f.read();p=self.f.f.f.directory/'event.json';p.write_text(json.dumps(self.f.event(stage='explained',question=None,answer=None,criteria=[])))
        args=['--unit-id','maps','--session','a','--event-file',p,'--expected-revision','0','--expected-unit-sha256',state['unit']['content_sha256'],'--compact']
        short=self.cli('learn-unit',*args);self.assertEqual(short['learning']['revision'],1);self.assertFalse(short['learning']['all_criteria_demonstrated'])
        before=self.files();self.assertIn('changed in another chat',self.cli('learn-unit',*args,ok=False).stderr);self.assertEqual(self.files(),before)
    def test_old_evidence_and_paginated_full_history_remain_read_only(self):
        self.f.save();journal=json.loads(self.f.path.read_text());first=journal['events'][0];e=copy.deepcopy(first)
        e['value']=self.f.event(stage='explained',answer=None,question=None,criteria=[])
        journal['events']+=[dict(copy.deepcopy(e),revision=i) for i in range(2,121)];journal['revision']=120
        self.f.path.write_text(json.dumps(journal));before=self.files()
        short=self.cli('learn-unit','--unit-id','maps','--compact')
        self.assertEqual(short['details_required']['event_revisions'],[])
        self.assertEqual(short['evidence'],[first])
        detail=self.cli('learn-unit','--unit-id','maps','--compact','--event-revision','1');self.assertEqual(detail['detail_events'],[first])
        rows=[];upper=121
        while upper:
            page=self.cli('learn-unit','--unit-id','maps','--compact','--history-limit','40','--history-before',upper)
            rows+=page['learning']['history'];upper=page['history_page']['next_before_revision']
        self.assertEqual(sorted(r['revision'] for r in rows),list(range(1,121)));self.assertEqual(self.files(),before)
    def test_detail_options_cannot_mutate_or_accept_invalid_bounds(self):
        state=self.f.read();p=self.f.f.f.directory/'event.json';p.write_text(json.dumps(self.f.event()));before=self.files()
        self.cli('learn-unit','--unit-id','maps','--event-file',p,'--expected-revision','0','--expected-unit-sha256',state['unit']['content_sha256'],'--event-revision','1',ok=False)
        self.assertEqual(self.files(),before)
        self.cli('learn-unit','--unit-id','maps','--compact','--event-revision','0',ok=False)
        self.cli('learn-unit','--unit-id','maps','--compact','--history-before','3',ok=False)
    def test_practice_help_and_solution_provenance_is_not_shortened(self):
        p=practice.AdaptivePractice();p.setUp();self.addCleanup(p.doCleanups);p.task();p.help('solution',request='Show the full solution')
        full=self.cli('learn-unit','--unit-id','maps',course=p.f.args().course);short=self.cli('learn-unit','--unit-id','maps','--compact',course=p.f.args().course)
        self.assertEqual(short['learning']['practice'],full['learning']['practice']);self.assertTrue(short['learning']['practice'][0]['solution_shown']);self.assertFalse(short['learning']['all_criteria_demonstrated'])
    def test_all_lesson_choices_and_selected_context_survive_new_chat(self):
        c=contexts.LessonContext();c.setUp();self.addCleanup(c.doCleanups)
        ids={c.context(c.create())['value']['id'],c.context(c.create())['value']['id']}
        short=self.cli('learn-unit','--unit-id','maps','--session','new-chat','--compact',course=c.f.args().course)
        self.assertEqual({x['id'] for x in short['learning']['lesson_choices']},ids)
        self.assertTrue(short['learning_progress']['continuation_choice_required']);self.assertIsNone(short['learning_progress']['stopped_at'])
        identity=next(iter(ids));selected=self.cli('learn-unit','--unit-id','maps','--lesson-id',identity,'--compact',course=c.f.args().course)
        self.assertEqual(selected['learning']['lesson_context']['event']['value']['id'],identity)
    def test_corrupt_journal_keeps_failure_and_recovery(self):
        self.f.path.parent.mkdir(parents=True,exist_ok=True);self.f.path.write_text('{bad');before=self.files()
        short=self.cli('learn-unit','--unit-id','maps','--compact')
        self.assertFalse(short['learning']['valid']);self.assertIn('recovery',short['learning']);self.assertFalse(short['learning_progress']['valid']);self.assertIsNone(short['learning_progress']['stopped_at']);self.assertEqual(self.files(),before)

class CompactDetailsRegression(unittest.TestCase):
    setUp=CompactOutput.setUp
    cli=CompactOutput.cli
    files=CompactOutput.files
    def test_default_cli_stays_identical_to_domain_output(self):
        import unit_learning,learning_tools
        state=self.f.save();before=self.files()
        self.assertEqual(self.cli('settings'),learning_tools.preferences(self.f.args()))
        full=self.cli('learn-unit','--unit-id','maps')
        self.assertEqual(full,unit_learning.learn_unit(self.f.args()))
        self.assertNotIn('output_view',full);self.assertEqual(self.files(),before)

    def test_all_four_diagnostic_results_keep_actual_answer_and_assessment(self):
        for outcome in ('correct','partial','incorrect','insufficient'):
            with self.subTest(outcome=outcome):
                d=diagnoses.DiagnosticPolicyV2();d.setUp();self.addCleanup(d.doCleanups)
                d.accept(competences=['Keys'],competence_prerequisites={});d.ask()
                d.answer(outcome,following=None,level='transfer' if outcome=='correct' else 'basic')
                full=self.cli('diagnostic',course=d.d.f.a.course)
                short=self.cli('diagnostic','--compact',course=d.d.f.a.course)
                self.assertEqual(short['selection_guidance']['last_response'],full['current']['answers'][-1])
                self.assertEqual(short['summary'],full['summary'])

    def test_large_attempt_history_omitted_without_changing_current_decision(self):
        import cli_output
        d=diagnoses.DiagnosticPolicyV2();d.setUp();self.addCleanup(d.doCleanups)
        d.accept(competences=['Keys'],competence_prerequisites={});d.ask();d.answer('partial',following='Keys')
        full=d.d.read();full['attempts']=full['attempts']*100;before=copy.deepcopy(full)
        short=cli_output.compact('diagnostic',full)
        self.assertEqual(short['summary'],full['summary']);self.assertEqual(short['selection_guidance'],full['selection_guidance'])
        self.assertLess(len(json.dumps(short)),len(json.dumps(full))//3);self.assertEqual(full,before)

    def test_diagnostic_details_cross_attempts_and_compact_skip_resume(self):
        d=diagnoses.DiagnosticPolicyV2();d.setUp();self.addCleanup(d.doCleanups)
        d.accept(competences=['Keys'],competence_prerequisites={});d.ask()
        pending=d.d.read()['current']['pending_prompt']['revision']
        d.d.save(dict(action='skip',prompt_revision=pending,next_difficulty='applied',next_competence=None,
                      selection_reason='QA skip without assuming a gap',skip_reason='Need a break'))
        d.d.save(dict(action='pause'));d.d.save(dict(action='resume'),session='another-chat')
        before=d.d.path.read_bytes()
        short=self.cli('diagnostic','--compact','--event-revision',pending,course=d.d.f.a.course)
        self.assertEqual(short['current']['skip_count'],1);self.assertEqual(short['detail_events'][0]['revision'],pending)
        page=self.cli('diagnostic','--compact','--history-limit','2','--history-before',pending+1,course=d.d.f.a.course)
        self.assertTrue(all(e['revision']<=pending for e in page['history']))
        self.assertEqual(d.d.path.read_bytes(),before)

    def test_legacy_lesson_context_keeps_all_material_and_progress(self):
        import test_learning_tools as old
        f=old.LearningTools();f.setUp();self.addCleanup(f.tearDown)
        full=self.cli('context','--lesson','lesson-01','--time','10',course=f.a.course)
        short=self.cli('context','--lesson','lesson-01','--time','10','--compact',course=f.a.course)
        for key,value in full.items():
            if key not in ('preferences','learning_intent'):self.assertEqual(short[key],value)
        self.assertEqual(short['preferences']['effective'],full['preferences']['effective'])
        self.assertEqual(short['learning_intent']['plan'],full['learning_intent']['plan'])

    def test_explicit_intent_history_is_not_discarded_by_compact(self):
        import learning_intent
        learning_intent.commit(self.f.args(expected_revision=0,reason='QA explicit goal'),
                               dict(goal=dict(description='Learn Java maps')))
        full=self.cli('intent','--history-limit','1')
        short=self.cli('intent','--compact','--history-limit','1')
        self.assertEqual(short['history'],full['history'])
        self.assertEqual(short['revision'],full['revision'])

    def test_invalid_intent_never_becomes_valid_after_projection(self):
        import learning_intent
        p=learning_intent.path_for(self.f.args());p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text('{"schema":"future_v999","events":[]}');before=self.files()
        for command,extra in [('learn-unit',['--unit-id','maps']),('study-next',[]),('resume',[]),('intent',['--history-limit','1'])]:
            state=self.cli(command,*extra,'--compact')
            self.assertFalse(state['learning_progress']['valid']);self.assertIsNone(state['learning_progress']['stopped_at'])
            self.assertIn('reason',state['learning_progress'])
        self.assertEqual(self.files(),before)

if __name__=='__main__':unittest.main()
