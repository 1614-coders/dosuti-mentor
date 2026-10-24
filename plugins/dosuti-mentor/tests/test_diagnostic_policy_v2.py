"""Versioned ordering constraints; semantic relevance remains the mentor's duty."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_diagnostic_assessment as fixtures
import diagnostic_learning as dl


class DiagnosticPolicyV2(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DiagnosticAssessment();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.d=self.f.d;self.d.save(dict(action='complete'));self.number=0

    def accept(self,**kw):
        value=dict(action='accept',question_policy='fixed_15_v1',selection_policy='adaptive_v2',
            competences=['Keys','Maps','Service'],competence_prerequisites={'Maps':['Keys'],'Service':['Maps']},next_difficulty='applied')
        value.update(kw);return self.d.save(value)
    def ask(self,name='Keys',level='applied'):
        self.number+=1
        return self.d.save(dict(action='prompt',competence=name,prompt='Analyse the example '+str(self.number),
            difficulty=level,format='open',rubric=self.f.rubric()))
    def answer(self,result='correct',following='Maps',level='applied',assistance='independent'):
        value=self.f.value(self.f.assessment(result,assistance));value.update(next_competence=following,next_difficulty=level)
        return self.d.save(value)
    def reject(self,operation):
        before=self.d.path.read_bytes();revision=self.d.read()['revision']
        with self.assertRaises(ValueError):operation()
        self.assertEqual(self.d.path.read_bytes(),before);self.assertEqual(self.d.read()['revision'],revision)

    def test_known_level_cannot_silently_be_overridden_with_basic(self):
        self.d.f.onboarding(dict(self_assessment='significant_experience',subject='Java Backend'))
        self.reject(lambda:self.accept(next_difficulty='basic'))
        state=self.accept(next_difficulty='transfer')
        self.assertEqual(state['current']['next_difficulty'],'transfer');self.ask(level='transfer')
        self.assertEqual(state['current']['answers'],[])

    def test_default_self_assessment_and_explained_calibration(self):
        self.d.f.onboarding(dict(self_assessment=dict(value='confident',text='Пишу backend, хочу проверить архитектуру')))
        value=dict(action='accept',question_policy='fixed_15_v1',selection_policy='adaptive_v2',competences=['Keys'],competence_prerequisites={})
        self.assertEqual(self.d.save(value)['current']['next_difficulty'],'applied')
        self.d.save(dict(action='complete'))
        self.assertEqual(self.d.save(dict(value,next_difficulty='basic',starting_reason='Calibrate the reported gap with a narrow prerequisite'))['current']['next_difficulty'],'basic')

    def test_graph_order_and_invalid_graphs(self):
        self.accept();self.reject(lambda:self.ask('Service'))
        self.ask();self.reject(lambda:self.answer(following='Service'))
        self.answer();self.ask('Maps');self.answer(following='Service');self.ask('Service')
        self.d.save(dict(action='complete'))
        for graph in ({'Keys':['Maps'],'Maps':['Keys']},{'Maps':['unknown']},{'Keys':['Keys']},{'Maps':['Keys','Keys']}):
            self.reject(lambda:self.accept(competence_prerequisites=graph))

    def test_free_calibration_and_later_profile_change_do_not_rewrite_active_attempt(self):
        self.d.f.onboarding(dict(self_assessment=dict(text='Не знаю уровень, работаю с backend'),subject='Java Backend'))
        self.reject(lambda:self.d.save(dict(action='accept',question_policy='fixed_15_v1',selection_policy='adaptive_v2',
            competences=['Keys'],competence_prerequisites={})))
        self.accept();self.ask();self.answer('incorrect',following='Keys',level='basic')
        before=self.d.path.read_bytes();self.d.f.onboarding(dict(self_assessment='significant_experience'))
        state=self.d.read();self.assertEqual(state['current']['next_difficulty'],'basic')
        self.assertEqual(self.d.path.read_bytes(),before)

    def test_wrong_prerequisite_success_then_harder_original_skill(self):
        self.accept();self.ask();self.answer();self.ask('Maps')
        self.reject(lambda:self.answer('incorrect',following='Service',level='basic'))
        state=self.answer('incorrect',following='Keys',level='basic')
        self.assertEqual(state['selection_guidance']['action'],'check_prerequisite')
        self.ask('Keys','basic')
        self.reject(lambda:self.answer(following='Maps',level='basic'))
        state=self.answer(following='Maps',level='transfer')
        self.assertEqual(state['selection_guidance']['recovery'][-1]['competence'],'Maps')
        self.assertNotIn('Maps',state['summary']['confirmed_knowledge'])
        self.ask('Maps','transfer');state=self.answer(following='Service',level='applied')
        self.assertEqual(state['selection_guidance']['recovery'],[])

    def test_nested_errors_preserve_the_original_failed_skill(self):
        self.accept();self.ask();self.answer();self.ask('Maps');self.answer(following='Service',level='transfer')
        self.ask('Service','transfer');self.answer('incorrect',following='Maps',level='applied')
        self.ask('Maps');self.answer('incorrect',following='Keys',level='basic')
        self.ask('Keys','basic');self.answer(following='Maps',level='applied')
        self.ask('Maps');self.reject(lambda:self.answer(following='Keys',level='transfer'))
        state=self.answer(following='Service',level='transfer')
        self.assertEqual([x['competence'] for x in state['selection_guidance']['recovery']],['Service'])

    def test_lower_level_errors_do_not_erase_original_transfer_obligation(self):
        self.accept(competences=['Keys'],competence_prerequisites={},next_difficulty='transfer')
        self.ask(level='transfer');self.answer('incorrect',following='Keys',level='applied')
        self.ask();self.answer('incorrect',following='Keys',level='basic');self.ask(level='basic')
        self.reject(lambda:self.answer(following='Keys',level='applied'))
        state=self.answer(following='Keys',level='transfer')
        self.assertEqual(state['selection_guidance']['recovery'][-1]['difficulty'],'transfer')

    def test_insufficient_assisted_and_partial_do_not_resolve_harder_gap(self):
        self.accept();self.ask();self.answer();self.ask('Maps');self.answer('incorrect',following='Keys',level='basic')
        for result,assistance in [('insufficient','independent'),('partial','independent'),('correct','assisted')]:
            self.ask('Keys','basic')
            state=self.answer(result,following='Maps',level='applied',assistance=assistance)
            self.assertEqual(state['selection_guidance']['recovery'][-1]['competence'],'Maps')
            expected={'insufficient':'clarify_response','partial':'probe_gap','correct':'independent_recheck'}[result]
            self.assertEqual(state['selection_guidance']['recovery_action'],expected)
            self.assertNotIn('Maps',state['summary']['confirmed_knowledge'])
            self.ask('Maps');self.answer('incorrect',following='Keys',level='basic')

    def test_fifteen_budget_recovery_rotation_and_completion(self):
        self.accept(competences=['Keys'],competence_prerequisites={})
        for i in range(15):
            self.ask(level='basic' if i else 'applied')
            state=self.answer('incorrect',following='Keys',level='basic')
        self.assertEqual(state['status'],'completed');self.assertEqual(state['current']['question_budget']['asked'],15)
        self.assertEqual(state['selection_guidance']['remaining'],0)
        self.reject(lambda:self.ask(level='basic'))

    def test_end_budget_prioritizes_available_unassessed_topics(self):
        self.accept(competences=['Keys','Maps','Writing'],competence_prerequisites={})
        for i in range(12):
            name='Keys' if i%2==0 else 'Maps'
            following=('Maps' if i%2==0 else 'Keys') if i<11 else 'Writing'
            self.ask(name);state=self.answer(following=following)
        self.assertEqual(state['selection_guidance']['remaining'],3)
        self.assertEqual(state['selection_guidance']['priority_competences'],['Writing'])
        self.ask('Writing');state=self.answer('insufficient',following='Keys')
        self.assertIn('Writing',state['selection_guidance']['unassessed_competences'])
        self.assertNotIn('Writing',state['summary']['identified_gaps'])

    def test_skip_pause_new_process_and_legacy_policy_are_read_only(self):
        self.accept();self.ask()
        pending=self.d.read()['current']['pending_prompt']['revision']
        self.d.save(dict(action='skip',prompt_revision=pending,next_difficulty='applied',next_competence='Maps',selection_reason='Learner skipped prerequisite; do not infer knowledge'))
        self.ask('Maps');self.answer('incorrect',following='Keys',level='basic');self.d.save(dict(action='pause'))
        before=self.d.path.read_bytes();script=Path(dl.__file__).with_name('course.py')
        run=subprocess.run([sys.executable,'-B',str(script),'diagnostic','--course',str(self.d.f.a.course),
            '--profile',self.d.f.a.profile,'--session','new-chat'],capture_output=True,text=True,timeout=20,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout)['selection_guidance']['recovery'][-1]['competence'],'Maps')
        self.assertEqual(self.d.path.read_bytes(),before)
        self.d.save(dict(action='resume'));self.ask('Keys','basic')
        self.d.save(dict(action='complete'));self.d.accept();self.d.prompt();self.d.answer('unknown','basic')
        before=self.d.path.read_bytes();self.assertNotIn('selection_policy',self.d.read()['current'])
        self.assertEqual(self.d.path.read_bytes(),before)

    def test_repeated_errors_can_rotate_without_erasing_unresolved_skill(self):
        self.accept(competences=['Keys','Writing'],competence_prerequisites={})
        for i in range(3):
            self.ask(level='applied' if i==0 else 'basic')
            state=self.answer('incorrect',following='Writing' if i==2 else 'Keys',level='basic')
        self.assertEqual(state['selection_guidance']['recovery'][-1]['competence'],'Keys')
        self.assertEqual(state['selection_guidance']['priority_competences'],[])
        self.ask('Writing','basic')
        self.assertNotIn('Keys',self.d.read()['summary']['confirmed_knowledge'])


if __name__=='__main__':unittest.main()
