"""Scripted learner, real CLI processes. Not Windows or live Codex UX acceptance."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_windows_onboarding import ANSWERS
from test_educational_acceptance import QUESTIONS, MAP

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
MATERIAL = Path(__file__).with_name('fixtures') / 'java-map-pilot.md'


class FullUXCycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'Учёба с пробелами'
        self.project.mkdir()
        self.root = self.project / '.dosuti-course'
        self.session = 'chat-a'
        self.cli('init', '--title', 'Java QA')
        self.connection('connect', '--scope', 'project', '--course', self.root,
                        '--profile', 'qa', '--expected-revision', 0)

    def process(self, script, *args):
        result = subprocess.run([sys.executable, '-B', str(SCRIPTS / script), *map(str, args)],
                                capture_output=True, encoding='utf-8', timeout=20,
                                env=dict(os.environ, PYTHONUTF8='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout if args[0] == 'init' else json.loads(result.stdout)

    def connection(self, action, *options):
        return self.process('connection.py', '--project', self.project, '--chat', self.session,
                            '--action', action, *options)

    def cli(self, command, *options):
        args = [command, '--course', self.root]
        if command in ('settings', 'intent', 'diagnostic', 'learn-unit', 'study-next', 'resume', 'status'):
            args += ['--profile', 'qa']
        if command in ('intent', 'diagnostic', 'learn-unit', 'study-next', 'resume', 'status'):
            args += ['--session', self.session]
        return self.process('course.py', *args, *options)

    def payload(self, value):
        path = Path(self.temp.name) / 'turn.json'
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def change(self, command, value):
        revision = self.cli(command)['revision']
        return self.cli(command, '--update-file', self.payload(value), '--expected-revision', revision,
                        '--reason', 'Explicit scripted QA learner action')

    def onboarding(self):
        self.assertEqual(self.cli('settings')['onboarding_next'], 'outcome')
        for index, (name, value) in enumerate(ANSWERS.items()):
            self.assertEqual(self.cli('settings')['onboarding_next'], name)
            state = self.cli('settings', '--onboarding-file', self.payload({'answers': {name: value}}))
            self.assertEqual(len(state['onboarding_answers']), index + 1)
        self.assertEqual(state['onboarding'], 'complete')
        self.assertEqual(self.cli('diagnostic')['status'], 'not_started')

    def unit(self):
        card = dict(id='map-pilot', title='Замена значения HashMap', summary='Ключи, значения и размер',
                    objectives=['Объяснить замену по существующему ключу'],
                    criteria=['Самостоятельно вычислить значения и размер после повторного ключа'],
                    prerequisites=[], uncertainties=[], sources=[dict(kind='text', role='external',
                        notes='Oracle Java SE 21 paraphrase with separately labelled tutor code',
                        path=str(MATERIAL), start_line=1, end_line=len(MATERIAL.read_text(encoding='utf-8').splitlines()))])
        return self.cli('unit', '--unit-file', self.payload(card), '--expected-revision', 0)

    def learn(self, value, checkpoint=False):
        state = self.cli('learn-unit', '--unit-id', 'map-pilot')
        return self.cli('learn-unit', '--unit-id', 'map-pilot',
                        '--checkpoint-file' if checkpoint else '--event-file', self.payload(value),
                        '--expected-revision', state['learning']['revision'],
                        '--expected-unit-sha256', state['unit']['content_sha256'])

    def test_onboarding_to_material_practice_and_cross_chat_processes(self):
        self.onboarding()
        self.change('intent', dict(goal=dict(description='Научиться пользоваться Java HashMap')))
        self.change('diagnostic', dict(action='accept',
            competences=list(dict.fromkeys(q[0] for q in QUESTIONS)), next_difficulty='basic'))
        revisions = {}
        for index, (competence, difficulty, question, answer, evidence, next_difficulty) in enumerate(QUESTIONS):
            prompted = self.change('diagnostic', dict(action='prompt', competence=competence,
                difficulty=difficulty, prompt=question, format='code_analysis'))
            revision = prompted['current']['pending_prompt']['revision']
            if index == 3:
                self.change('diagnostic', dict(action='pause'))
                self.session = 'chat-b'
                self.assertEqual(self.connection('status')['binding']['profile'], 'qa')
                restored = self.cli('diagnostic')
                self.assertEqual(restored['status'], 'paused')
                self.assertEqual(restored['current']['pending_prompt']['revision'], revision)
                self.change('diagnostic', dict(action='resume'))
            decision = dict(action='skip' if answer is None else 'answer', prompt_revision=revision,
                next_competence=QUESTIONS[index + 1][0] if index + 1 < len(QUESTIONS) else None,
                next_difficulty=next_difficulty, selection_reason='Scripted choice based on recorded answer')
            if answer is None:
                decision['skip_reason'] = 'Ещё не изучал'
            else:
                decision.update(answer=answer, evidence=evidence, evaluation='Scripted Java QA: ' + evidence)
            result = self.change('diagnostic', decision)
            if answer is not None:
                revisions[competence] = result['revision']
        summary = self.change('diagnostic', dict(action='complete'))['summary']
        self.assertIn(MAP, summary['identified_gaps'])
        self.assertEqual(self.cli('diagnostic')['summary'], summary)
        self.unit()
        unit = self.cli('learn-unit', '--unit-id', 'map-pilot')['unit']
        target = dict(competence=MAP, rationale='Ученик считает вызовы put вместо разных ключей',
            unit_id='map-pilot', unit_sha256=unit['content_sha256'], criteria=[1],
            diagnostic_event_revisions=[revisions[MAP]], alignment_review=dict(verdict='supported',
                competence=MAP, diagnostic_event_revisions=[revisions[MAP]],
                diagnostic_reason='Ответ о размере показывает непонимание замены существующего ключа',
                criterion_links=[dict(criterion=1, criterion_text=unit['criteria'][0], source=1,
                    quote='его прежнее значение заменяется. size считает пары ключ–значение, а не вызовы put.',
                    explanation='Новая трассировка проверяет замену значения без увеличения числа ключей')]))
        proposal = dict(summary='Разобрать замену ключа, затем решить новую задачу',
            steps=['Замена значения и размер HashMap'], step_bases=[dict(unit_ids=['map-pilot'],
                diagnostic_event_revisions=[revisions[MAP]], targets=[target])])
        state = self.change('intent', dict(proposal=proposal))
        self.assertIsNone(state['plan'])  # No implicit Roadmap consent.
        self.assertEqual(self.cli('study-next')['learning_intent']['plan'], None)
        self.change('intent', dict(accept_proposal=True))
        self.assertEqual(self.cli('material-status', '--unit-id', 'map-pilot')['material']['status'], 'ready')
        state = self.cli('learn-unit', '--unit-id', 'map-pilot')
        self.assertEqual(state['preferences']['onboarding_answers']['learning_format']['value'], 'step_by_step')
        self.assertTrue(state['unit']['source_contexts'])
        self.learn(dict(stage='explained', details='Разобран пример 5 7 2', question=None,
                        answer=None, hints=0, criteria=[]))
        self.assertFalse(self.cli('learn-unit', '--unit-id', 'map-pilot')['learning']['all_criteria_demonstrated'])
        for stage, question, answer, hints, action in (
                ('needs_review', 'put(a,2);put(b,7);put(a,5): size?',
                 'Размер 3: три вызова put', 0, 'review_error'),
                ('practice_with_help', 'put(a,2);put(b,7);put(a,5): size? Подсказка: посчитай разные ключи.',
                 'Размер 2 после подсказки о ключах', 1, 'fresh_independent_task'),
                ('solved_independently', 'put(x,4);put(y,8);put(x,9): значения и размер?',
                 '9, 8, 2: x заменён, y сохранён', 0, 'continue_or_wait')):
            learned = self.learn(dict(stage=stage, details='Оценка конкретного ответа QA',
                question=question, answer=answer, hints=hints, criteria=[1]))
            self.assertEqual(learned['learning']['adaptive']['criteria'][0]['action'], action)
            queue = self.cli('study-next')
            expected = action if stage != 'solved_independently' else 'review_route'
            self.assertIn(expected, [row['action'] for row in queue['roadmap']['recommendations']])
        self.assertEqual(queue['learning_progress']['criterion_completed_steps'], [1])
        self.assertEqual(queue['learning_progress']['completed_steps'], [])
        checkpoint = dict(recap='Самостоятельно разобрана замена ключа',
                          next_step='Проверить добавление нового ключа', pending_question='Что изменится после put(z,1)?')
        self.learn(checkpoint, checkpoint=True)
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.session = 'chat-c'
        self.assertEqual(self.connection('status')['binding']['profile'], 'qa')
        self.assertEqual(self.cli('settings')['onboarding'], 'complete')
        new_chat = self.cli('learn-unit', '--unit-id', 'map-pilot')
        self.assertTrue(new_chat['learning']['all_criteria_demonstrated'])
        self.assertIsNone(new_chat['learning']['checkpoint']['event'])  # Local cursor is deliberately session-specific.
        shared = self.cli('resume')['learning_progress']
        self.assertEqual(shared['stopped_at']['checkpoint'], checkpoint)
        self.assertFalse(shared['continuation_choice_required'])
        self.assertEqual(self.cli('diagnostic')['summary'], summary)
        self.assertEqual({str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_refusal_does_not_block_material_or_unaccepted_route(self):
        self.onboarding()
        self.change('diagnostic', dict(action='decline'))
        self.unit()
        self.change('intent', dict(goal=dict(description='Изучить HashMap без диагностики')))
        self.change('intent', dict(proposal=dict(summary='Предварительный план', steps=['Разобрать HashMap'])))
        self.assertIsNone(self.cli('intent')['plan'])
        self.change('intent', dict(proposal=None))  # Explicit rejection, no hidden acceptance.
        self.assertIsNone(self.cli('intent')['plan'])
        self.assertTrue(self.cli('learn-unit', '--unit-id', 'map-pilot')['learning']['valid'])
        self.session = 'chat-new'
        self.assertEqual(self.cli('diagnostic')['status'], 'declined')
        self.assertEqual(self.cli('settings')['onboarding_pending'], [])

    def test_new_chat_keeps_ambiguous_continuations_without_choosing(self):
        self.unit()
        for session in ('chat-a', 'chat-b'):
            self.session = session
            self.learn(dict(recap='Обсуждали ключи в ' + session,
                            next_step='Продолжить практику', pending_question='Что хранит ключ?'), checkpoint=True)
        self.session = 'chat-c'
        state = self.cli('learn-unit', '--unit-id', 'map-pilot')
        self.assertIsNone(state['learning']['checkpoint']['event'])
        progress = state['learning_progress']
        self.assertTrue(progress['continuation_choice_required'])
        self.assertIsNone(progress['stopped_at'])
        self.assertEqual({row['session'] for row in progress['continuations']}, {'chat-a', 'chat-b'})

    @unittest.skipUnless(shutil.which('java'), 'Java unavailable; material execution unverified')
    def test_real_java_material_output(self):
        source = MATERIAL.read_text(encoding='utf-8').split('```java\n', 1)[1].split('```', 1)[0]
        path = Path(self.temp.name) / 'MapPilot.java'
        path.write_text(source, encoding='utf-8')
        result = subprocess.run(['java', str(path)], capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '5 7 2')


if __name__ == '__main__':
    unittest.main()
