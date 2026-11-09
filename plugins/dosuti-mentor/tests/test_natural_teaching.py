"""Instruction reachability and state/provenance guarantees, not live teaching quality."""
import json
from pathlib import Path
import unittest
import test_lesson_presentation as presentation
import test_learning_tools as onboarding
import test_adaptive_practice as practice
import learning_tools

PLUGIN=Path(__file__).resolve().parents[1]


class NaturalTeachingInstructions(unittest.TestCase):
    def test_shared_delivery_has_explicit_level_confusion_and_feedback_sections(self):
        # Documentation lint only: presence does not prove that a model obeys it.
        text=(PLUGIN/'references/lesson-presentation.md').read_text(encoding='utf-8')
        for heading in ('## Адаптация к уровню','## Если ученик запутался','## Обратная связь без шаблона'):
            with self.subTest(heading=heading):self.assertIn(heading,text)

    def test_feedback_skills_link_to_shared_delivery_and_offer_contract(self):
        for name in ('teach-unit','teach-lecture','teach-project','review-homework','review-learning'):
            text=(PLUGIN/f'skills/{name}/SKILL.md').read_text(encoding='utf-8')
            with self.subTest(skill=name):self.assertIn('../../references/lesson-presentation.md',text)
        text=(PLUGIN/'references/adaptive-practice.md').read_text(encoding='utf-8')
        self.assertIn('## Когда полный разбор предлагает наставник',text)


class NaturalPresentationState(unittest.TestCase):
    def setUp(self):
        self.p=presentation.LessonPresentation();self.p.setUp();self.addCleanup(self.p.doCleanups)
        self.u=self.p.u

    def save_answers(self,**patch):
        answers=onboarding.LearningTools().all_onboarding_answers()|patch
        path=self.u.f.f.directory/'natural-preferences.json'
        path.write_text(json.dumps(dict(answers=answers)),encoding='utf-8')
        return learning_tools.settings(self.u.f.args(scope='profile',set=[],reset=False,onboarding=None,onboarding_file=path))

    def test_saved_levels_and_free_self_report_reach_lesson_without_repeating_onboarding(self):
        for level,assessment,text in (
            ('beginner','zero','Only starting Java'),
            ('intermediate','basic_experience','I use collections but find concurrency confusing'),
            ('advanced','significant_experience','I build backend services; explain the memory-model boundary')):
            with self.subTest(level=level):
                self.save_answers(self_assessment=dict(value=assessment,text=text))
                self.p.settings(['level='+level])
                before=self.p.files();state=self.u.read(session='another-chat')
                prefs=state['preferences'];self.assertEqual(prefs['effective']['level'],level)
                self.assertEqual(prefs['onboarding_answers']['self_assessment']['text'],text)
                self.assertEqual(prefs['onboarding'],'complete');self.assertIsNone(prefs['onboarding_next'])
                self.assertEqual(state['learning']['attempt_count'],0)
                self.assertEqual(state['learning_progress']['demonstrated_units'],[])
                self.assertEqual(self.p.files(),before)

    def test_confusion_and_pace_request_survive_checkpoint_without_becoming_assessments(self):
        self.p.settings(['checks=each_block','detail=detailed'])
        identity=self.p.f.context(self.p.f.create())['value']['id']
        recap='Learner: I do not understand why the value changes. Explain continuously using another example.'
        self.u.save(dict(recap=recap,next_step='Trace two equal keys with different values',pending_question=None),
                    checkpoint=True,lesson_id=identity,session='old-chat')
        before=self.p.files();state=self.u.read(lesson_id=identity,session='new-chat')
        self.assertEqual(state['learning']['lesson_checkpoint']['event']['value']['recap'],recap)
        self.assertEqual(state['preferences']['effective']['checks'],'each_block')
        self.assertEqual(state['preferences']['effective']['detail'],'detailed')
        self.assertEqual(state['learning']['attempt_count'],0)
        self.assertEqual(state['learning']['criteria'],{'1':'not_assessed'})
        self.assertEqual(self.p.files(),before)

    def test_temporary_requests_do_not_leak_preferences_to_another_profile_or_course(self):
        self.p.settings(['level=advanced','detail=concise'])
        other=presentation.LessonPresentation();other.setUp();self.addCleanup(other.doCleanups)
        before=other.files();state=other.u.read(session='new-chat')
        self.assertEqual(state['preferences']['effective']['level'],learning_tools.DEFAULTS['level'])
        self.assertEqual(other.files(),before)
        friend=self.u.read(profile='friend')['preferences']
        self.assertEqual(friend['effective']['level'],learning_tools.DEFAULTS['level'])
        self.assertEqual(friend['effective']['detail'],learning_tools.DEFAULTS['detail'])


class NaturalPracticeProvenance(unittest.TestCase):
    def setUp(self):
        self.p=practice.AdaptivePractice();self.p.setUp();self.addCleanup(self.p.doCleanups)
        self.u=self.p.f

    def test_error_correction_with_help_needs_a_fresh_task_for_independent_evidence(self):
        self.p.task();self.p.attempt(stage='needs_review',answer='A map is only a list of values',
            details='The answer omits keys',error_tags=['concept'])
        self.assertEqual(self.u.read()['learning']['practice'][0]['next_action'],'explain_error_then_retry')
        self.p.help();self.p.attempt(stage='practice_with_help',hints=1,answer='Keys identify the values')
        state=self.u.read()['learning'];self.assertFalse(state['all_criteria_demonstrated'])
        self.assertEqual(state['practice'][0]['last_attempt']['stage'],'practice_with_help')
        with self.assertRaises(ValueError):self.p.attempt(hints=0)
        self.p.task('fresh-map','How does replacing an equal key affect size?')
        self.u.save(self.u.event(task_id='fresh-map',question='How does replacing an equal key affect size?',
            answer='Size stays unchanged when an existing equal key gets a new value'))
        self.assertTrue(self.u.read()['learning']['all_criteria_demonstrated'])

    def test_gradual_help_is_saved_in_order_and_does_not_create_a_correct_answer(self):
        self.p.task()
        for level in ('hint','concrete','explanation'):self.p.help(level)
        task=self.u.read()['learning']['practice'][0]
        self.assertEqual([h['level'] for h in task['help']],['hint','concrete','explanation'])
        self.assertIsNone(task['last_attempt']);self.assertFalse(task['solution_shown'])
        self.assertEqual(self.u.read()['learning']['attempt_count'],0)

    def test_direct_request_and_accepted_offer_allow_solution_without_forced_hints(self):
        for identity,question,request in (
            ('map-1','What does a map store?','Show the full solution now'),
            ('offered','Explain replacement step by step','Yes, show the worked solution you offered')):
            with self.subTest(request=request):
                self.p.task(identity,question);self.p.help('solution',request,identity)
                task=next(t for t in self.u.read()['learning']['practice'] if t['id']==identity)
                self.assertEqual([h['level'] for h in task['help']],['solution'])
                self.assertEqual(task['help'][0]['request'],request)
                self.assertIsNone(task['last_attempt']);self.assertTrue(task['solution_shown'])
        self.assertFalse(self.u.read()['learning']['all_criteria_demonstrated'])

    def test_teacher_judgement_is_not_a_fabricated_request_or_new_grade(self):
        self.p.task();before=self.u.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'explicit request'):self.p.help('solution')
        self.assertEqual(self.u.path.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'another chat'):
            self.p.write(dict(action='help',task_id='map-1',level='hint',content='Trace keys',request=None),revision=0)
        self.assertEqual(self.u.path.read_bytes(),before)
