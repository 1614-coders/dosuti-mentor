"""Approved copy and persisted onboarding. Scripted CLI is not a live Codex dialogue."""
import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_learning_tools
import learning_tools as lt
import diagnostic_learning as diagnostic

PLUGIN = Path(__file__).resolve().parents[1]


class OnboardingDialog(unittest.TestCase):
    def setUp(self):
        self.f = test_learning_tools.LearningTools(); self.f.setUp(); self.addCleanup(self.f.tearDown)

    def save(self, answers):
        return self.f.onboarding(answers)

    def test_clean_start_renders_exact_approved_greeting_and_question_one(self):
        state = lt.preferences(self.f.a)
        expected = json.loads((Path(__file__).with_name('fixtures') / 'onboarding-approved.json').read_text(encoding='utf-8'))
        self.assertEqual(state['onboarding_message'], expected['greeting'] + '\n\n' + expected['questions'][0])

    def test_all_eight_approved_blocks_in_order_numbered_without_emoji(self):
        expected = json.loads((Path(__file__).with_name('fixtures') / 'onboarding-approved.json').read_text(encoding='utf-8'))
        lt.settings(self.f.args(scope='profile',set=[],reset=False,onboarding='in_progress'))
        for index, (name, value) in enumerate(self.f.all_onboarding_answers().items()):
            message = lt.preferences(self.f.a)['onboarding_message']
            self.assertEqual(message, expected['questions'][index])
            self.assertFalse(any(ord(c) > 0xFFFF or c in '🎓🎯🧠✨' for c in message))
            options = [line.split('.', 1)[0] for line in message.splitlines() if line[:1].isdigit()]
            self.assertEqual(options, [str(i) for i in range(1, len(options) + 1)])
            self.save({name: value})
        state = lt.preferences(self.f.a)
        self.assertEqual(state['onboarding'], 'complete'); self.assertIsNone(state['onboarding_message'])
        self.assertIsNone(state['onboarding_next'])

    def test_free_and_mixed_answers_for_every_question_are_preserved(self):
        answers = dict(outcome=dict(value=['topics', 'skill'], text='1, 2 — хочу объяснять решения коллегам'),
            subject=dict(value='Java Backend', text='Java Backend', area='Программирование и разработка.', direction='Java Backend'),
            self_assessment=dict(text='Два года пишу тесты, но с алгоритмами трудно'),
            learning_format=dict(text='Хочу сначала самому объяснять ход решения'),
            assistant_role=dict(value=['teacher', 'partner'], text='1 и 3, только не решай за меня'),
            check_frequency=dict(text='После сложных мест, если я путаюсь'),
            explanation_detail=dict(text='Сначала схема, потом один контрпример'),
            constraints=dict(value=dict(selected=['study_time','free_pace'], details='По вечерам'), text='2, 4 — по часу во вторник и пятницу'))
        saved = self.save(answers)
        for name, value in answers.items():
            row = saved['onboarding_answers'][name]
            self.assertEqual(row['text'], value['text'])
            self.assertEqual(row['value'], value.get('value'))
        self.assertEqual(saved['effective'], lt.DEFAULTS)
        self.assertEqual(lt.preferences(self.f.args(session='another-chat')), saved)
        edited = self.save({'explanation_detail': dict(text='Объясни проще, с бытовыми примерами')})
        self.assertEqual(edited['onboarding_answers']['subject'], saved['onboarding_answers']['subject'])
        self.assertIn('Объясни проще', edited['onboarding_answers']['explanation_detail']['text'])

    def test_broad_subject_stays_on_question_two_across_pause_and_new_process(self):
        self.save({'outcome': ['profession']})
        state = self.save({'subject': dict(value='Программирование и разработка.',
            text='1', area='Программирование и разработка.', pending=True)})
        self.assertEqual(state['onboarding_next'], 'subject')
        self.assertIn('Java', state['onboarding_message']); self.assertIn('Вопрос 2 из 8', state['onboarding_message'])
        path = lt.pref_file(self.f.a, 'profile'); before = path.read_bytes()
        result = subprocess.run([sys.executable, '-B', str(PLUGIN/'scripts/course.py'), 'settings',
            '--course', str(self.f.a.course), '--profile', self.f.a.profile],
            capture_output=True, encoding='utf-8', timeout=20, env=dict(os.environ, PYTHONUTF8='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['onboarding_next'], 'subject')
        self.assertEqual(path.read_bytes(), before)
        state = self.save({'subject': dict(value='Java Backend', text='Хочу Java Backend',
            direction='Java Backend')})
        self.assertEqual(state['onboarding_next'], 'self_assessment')
        self.assertEqual(state['onboarding_answers']['subject']['area'], 'Программирование и разработка.')
        self.assertEqual(state['onboarding_answers']['subject']['text'], '1\nХочу Java Backend')

    def test_concrete_or_declined_specialization_does_not_force_clarification(self):
        self.save({'outcome': ['topics']})
        state = self.save({'subject': 'Java Backend'})
        self.assertEqual(state['onboarding_next'], 'self_assessment')
        state = self.save({'subject': dict(value='Дизайн и творчество.', text='Пока без уточнения', area='Дизайн и творчество.')})
        self.assertEqual(state['onboarding_next'], 'self_assessment')

    def test_clarification_prevents_premature_completion_and_skip_is_not_an_error(self):
        self.save(self.f.all_onboarding_answers())
        state = self.save({'subject': dict(value='Бизнес и управление.', text='4', area='Бизнес и управление.', pending=True)})
        self.assertEqual(state['onboarding'], 'in_progress'); self.assertEqual(state['onboarding_next'], 'subject')
        state = self.f.onboarding(skip=['subject'])
        self.assertEqual(state['onboarding'], 'complete')
        self.assertFalse(diagnostic.path_for(self.f.a).exists())

    def test_own_answer_clarification_keeps_question_seven_and_eight(self):
        self.save(self.f.all_onboarding_answers())
        for name, number, choice in [('explanation_detail', 7, '5'), ('constraints', 8, '6')]:
            with self.subTest(question=name):
                state=self.save({name:dict(text=choice,pending=True)})
                self.assertEqual(state['onboarding_next'],name)
                self.assertIn(f'Вопрос {number} из 8',state['onboarding_message'])
                self.assertIn('своими словами',state['onboarding_message'])
                self.assertEqual(lt.preferences(self.f.args(session='new-chat')),state)
                self.assertEqual(state['onboarding'],'in_progress')
                state=self.save({name:dict(text='Мой конкретный вариант')})
                self.assertEqual(state['onboarding'],'complete')
                self.assertIsNone(state['onboarding_next'])

    def test_invalid_rich_records_preserve_files(self):
        self.save({'subject': 'Java'})
        path = lt.pref_file(self.f.a, 'profile'); before = path.read_bytes()
        for value in ({'text': ''}, {'text': 'x'*4001}, {'text': 'x', 'invented': True},
                      {'value': 42, 'text': 'x'}, {'value': 'Java', 'text': 'x', 'pending': 'yes'}):
            with self.subTest(value=value), self.assertRaises(ValueError): self.save({'subject': value})
            self.assertEqual(path.read_bytes(), before)

    def test_legacy_finished_profile_edit_only_clarifies_chosen_question(self):
        path=lt.pref_file(self.f.a,'profile');path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(dict(values={},onboarding='complete')))
        state=self.save({'subject':dict(value='Бизнес и управление.',text='4',area='Бизнес и управление.',pending=True)})
        self.assertEqual(state['onboarding_next'],'subject')
        self.assertEqual(state['onboarding_pending'],['subject'])
        state=self.save({'subject':dict(value='Маркетинг',text='Маркетинг',direction='Маркетинг')})
        self.assertEqual(state['onboarding'],'complete');self.assertEqual(state['onboarding_pending'],[])
        self.assertEqual(set(state['onboarding_answers']),{'subject'})

    def test_text_is_available_to_real_lesson_context_without_synthetic_preferences(self):
        import course
        state=self.save({'explanation_detail':dict(text='Объясни проще, сначала схема'),
                         'assistant_role':dict(value='examiner',text='Проверяй, но не решай за меня')})
        path=lt.pref_file(self.f.a,'profile');before=path.read_bytes()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):course.status(self.f.a)
        prefs=json.loads(output.getvalue())['preferences']
        self.assertEqual(prefs['onboarding_answers'],state['onboarding_answers'])
        self.assertEqual(prefs['effective'],lt.DEFAULTS)
        self.assertEqual(path.read_bytes(),before)

    def test_installed_skill_routes_to_exact_copy_and_pending_subject_contract(self):
        skill=(PLUGIN/'skills/start-learning/SKILL.md').read_text(encoding='utf-8')
        self.assertIn('onboarding_message',skill);self.assertIn('onboarding-dialog.md',skill)
        self.assertIn('pending=true',skill);self.assertIn('starting_guidance',skill)
        self.assertNotIn('Привет! Я твой помощник в обучении.',skill)


class DiagnosticStartingPoint(unittest.TestCase):
    def setUp(self):
        self.f = test_learning_tools.LearningTools(); self.f.setUp(); self.addCleanup(self.f.tearDown)

    def save(self, value):
        return diagnostic.diagnostic(self.f.args(update_file=self.f.write('diagnostic-input.json', value),
            expected_revision=diagnostic.view(self.f.a)['revision'], reason='Explicit QA learner consent'))

    def test_self_assessment_maps_to_existing_difficulty_without_evidence(self):
        for level, expected in [('zero','basic'), ('basics','basic'), ('basic_experience','applied'),
                                ('confident','applied'), ('significant_experience','transfer'), ('unknown','basic')]:
            with self.subTest(level=level):
                self.f.onboarding(dict(subject=dict(value='Java Backend', text='Java Backend', direction='Java Backend'), self_assessment=level))
                state = diagnostic.diagnostic(self.f.a)
                self.assertEqual(state['starting_guidance']['difficulty'], expected)
                self.assertEqual(state['starting_guidance']['subject']['direction'], 'Java Backend')
                self.assertEqual(state['status'], 'not_started')
                self.assertFalse(diagnostic.path_for(self.f.a).exists())

    def test_advanced_accept_uses_transfer_then_actual_error_adapts_without_mastery(self):
        self.f.onboarding(dict(subject='Java', self_assessment='significant_experience'))
        state = self.save(dict(action='accept', competences=['Mutable HashMap keys']))
        self.assertEqual(state['current']['next_difficulty'], 'transfer')
        self.assertEqual(state['current']['answers'], [])
        state = self.save(dict(action='prompt', competence='Mutable HashMap keys', difficulty='transfer',
            format='bug_search', prompt='После изменения поля equals/hashCode ключа HashMap.get возвращает null. Почему и как исправить?'))
        state = self.save(dict(action='answer', prompt_revision=state['current']['pending_prompt']['revision'],
            answer='HashMap теряет элемент случайно', evaluation='Не объяснена связь хеша и поиска ключа', evidence='unknown',
            next_difficulty='applied', next_competence='Mutable HashMap keys', selection_reason='Уточнить изменение хеша до проверки предпосылок'))
        self.assertEqual(state['current']['next_difficulty'], 'applied')
        self.assertEqual(state['summary']['confirmed_knowledge'], [])
        self.save(dict(action='pause'))
        self.assertEqual(diagnostic.diagnostic(self.f.args(session='new-chat'))['current']['next_difficulty'], 'applied')

    def test_free_level_stays_calibration_and_decline_does_not_block_onboarding(self):
        self.f.onboarding(dict(self_assessment=dict(text='Не знаю, разные темы знаю по-разному')))
        state = diagnostic.diagnostic(self.f.a)
        self.assertTrue(state['starting_guidance']['calibration'])
        state = self.save(dict(action='decline'))
        self.assertEqual(state['status'], 'declined')
        self.assertEqual(lt.preferences(self.f.a)['onboarding_next'], 'outcome')

    def test_initial_guidance_uses_saved_goal_and_is_read_only(self):
        import learning_intent
        self.f.onboarding(dict(subject='Java Backend',self_assessment='zero'))
        learning_intent.commit(self.f.args(expected_revision=0,reason='Explicit QA goal'),
                               dict(goal=dict(description='Создать сервер заказов на Java')))
        paths=[lt.pref_file(self.f.a,'profile'),learning_intent.path_for(self.f.a)]
        before={str(p):p.read_bytes() for p in paths}
        state=diagnostic.diagnostic(self.f.a)
        self.assertEqual(state['starting_guidance']['goal']['description'],'Создать сервер заказов на Java')
        self.assertEqual(state['starting_guidance']['difficulty'],'basic')
        self.assertEqual({str(p):p.read_bytes() for p in paths},before)
        self.assertFalse(diagnostic.path_for(self.f.a).exists())


if __name__ == '__main__': unittest.main()
