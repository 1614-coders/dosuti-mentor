"""Cross-component acceptance with scripted judgements, never a live model UX test."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_diagnostic_learning as fixtures
import test_diagnostic_assessment as assessment_fixtures
import diagnostic_learning as dl
import learning_tools as lt
import learning_intent as intent
import adaptive_learning as adaptive

PLUGIN = Path(__file__).resolve().parents[1]


class AdaptiveDiagnosticAcceptance(unittest.TestCase):
    def setUp(self):
        self.d = fixtures.DiagnosticLearning()
        self.d.setUp()
        self.addCleanup(self.d.tearDown)
        self.f = self.d.f
        self.grader = assessment_fixtures.DiagnosticAssessment()
        self.competences = ['Java Map replacement', 'Java Backend HTTP']

    def goal(self, description):
        state = intent.view(self.f.a)
        intent.commit(self.f.args(expected_revision=state['revision'], reason='Explicit QA goal'),
                      dict(goal=dict(description=description)))

    def onboard(self, level, subject='Java Backend', text='Я только начинаю изучать Java'):
        answers = self.f.all_onboarding_answers()
        answers.update(subject=dict(value=subject, text=subject, direction='Практические задачи'),
                       self_assessment=dict(value=level, text=text))
        for key, value in answers.items():
            self.assertEqual(lt.preferences(self.f.a)['onboarding_next'], key)
            self.f.onboarding({key: value})
            saved = lt.preferences(self.f.args(session='onboarding-other-chat'))
            self.assertEqual(saved['onboarding_answers'][key]['status'], 'answered')
        self.assertEqual(saved['onboarding'], 'complete')
        self.assertEqual(saved['onboarding_answers']['self_assessment']['text'], text)
        self.assertFalse(self.d.path.exists())

    def accept(self, **kw):
        return self.d.save(dict(action='accept', competences=self.competences,
                                question_policy='fixed_15_v1', selection_policy='adaptive_v1') | kw)

    def ask(self, number, competence=None):
        current = self.d.read()['current']
        competence = competence or current.get('next_competence') or self.competences[0]
        if competence == 'Java Map replacement':
            prompt = f'Map<String,Integer>: put("a",{number}); put("a",{number + 1}). What are get("a") and size(), and why?'
            criteria = ['State replacement value', 'Explain unchanged map size']
        elif competence == 'Java Backend HTTP':
            prompt = f'GET /items/{number} refers to an absent item. Choose an HTTP status and explain its meaning.'
            criteria = ['Choose 404', 'Explain resource absence']
        else:
            prompt = f'Poster {number} uses dark grey letters on black. Identify the issue and suggest a readable alternative.'
            criteria = ['Identify low contrast', 'Propose readable text and background']
        return self.d.save(dict(action='prompt', competence=competence, prompt=prompt,
            difficulty=current['next_difficulty'], format='open',
            rubric=dict(action='Explain the given example', required=criteria, critical_errors=[])))

    def answer(self, result='correct', assistance='independent', next_difficulty='applied', next_competence=None):
        current = self.d.read()['current']
        prompt = current['pending_prompt']
        assessment = self.grader.assessment(result, assistance)
        assessment['critical_errors'] = []
        answers = {
            'Java Map replacement': {'correct':'The last value replaces the previous one; size is 1.',
                'partial':'The last value is stored, but size is 2.', 'incorrect':'The first value remains; size is 2.'},
            'Java Backend HTTP': {'correct':'404: the requested resource does not exist.',
                'partial':'404; it means successful creation.', 'incorrect':'201: the absent item was created.'}}
        answer = answers.get(prompt['competence'], {
            'correct':'Contrast is low; use white letters on the black background.',
            'partial':'Contrast is low; keep both colours unchanged.',
            'incorrect':'Contrast is sufficient; make the text black too.'}).get(result, 'I cannot assess this example yet.')
        evidence = ('confirmed' if result == 'correct' and assistance == 'independent' else
                    'needs_practice' if result in ('correct', 'partial') else 'unknown')
        return self.d.save(dict(action='answer', prompt_revision=prompt['revision'], answer=answer,
            evaluation='Scripted QA comparison against the two saved criteria', evidence=evidence,
            assessment=assessment, next_difficulty=next_difficulty,
            next_competence=next_competence or prompt['competence'],
            selection_reason='Check the remaining criterion or a fresh independent example'))

    def restart(self, action=None):
        before = self.d.path.read_bytes()
        command = [sys.executable, '-B', str(PLUGIN / 'scripts/course.py'), 'diagnostic',
            '--course', str(self.f.a.course), '--profile', self.f.a.profile, '--session', 'fresh-process']
        if action is not None:
            update = self.f.write('restart-action.json', dict(action=action))
            command += ['--update-file', str(update), '--expected-revision', str(self.d.read()['revision']),
                        '--reason', 'Explicit QA continuation in a new process']
        process = subprocess.run(command,
            capture_output=True, encoding='utf-8', timeout=20, env=dict(os.environ, PYTHONUTF8='1'))
        self.assertEqual(process.returncode, 0, process.stderr)
        state = json.loads(process.stdout)
        self.assertEqual(state['summary'], self.d.read()['summary'])
        if action is None: self.assertEqual(self.d.path.read_bytes(), before)
        return state

    def propose(self, state):
        advice = adaptive.study_next(self.f.args(limit=10))['roadmap']['proposal_guidance']
        self.assertTrue(advice['acceptance_required'])
        self.assertEqual(advice['recommendations'], state['summary']['recommendations'])
        self.assertEqual(advice['goal']['description'], intent.view(self.f.a)['goal']['description'])
        card = next(row for row in state['summary']['competence_map'] if row['evidence'])
        refs = [row['answer_revision'] for row in card['evidence']]
        before = self.d.path.read_bytes()
        current = intent.view(self.f.a)
        proposal = dict(summary='Address the recorded diagnostic needs',
            steps=[card['competence']], step_bases=[dict(diagnostic_event_revisions=refs)])
        intent.commit(self.f.args(expected_revision=current['revision'], reason='QA proposal only'), dict(proposal=proposal))
        proposed = intent.view(self.f.args(session='route-other-chat'))
        self.assertIsNone(proposed['plan'])
        self.assertEqual(proposed['proposal']['route_state'], 'proposed')
        self.assertEqual(self.d.path.read_bytes(), before)
        intent.commit(self.f.args(expected_revision=proposed['revision'], reason='Explicit simulated learner consent'),
                      dict(accept_proposal=True))
        self.assertEqual(intent.view(self.f.a)['plan']['route_state'], 'accepted')

    def test_beginner_fifteen_questions_skip_pause_restart_and_route_consent(self):
        self.onboard('zero')
        self.goal('Become a Java Backend developer')
        self.assertEqual(self.accept()['current']['next_difficulty'], 'basic')
        results = ['correct', 'partial', 'incorrect', 'insufficient', 'correct'] * 3
        for i, result in enumerate(results, 1):
            self.ask(i)
            if i == 7:
                self.d.save(dict(action='pause'))
                restored = self.restart()
                self.assertEqual(restored['current']['question_budget']['asked'], 7)
                resumed = self.restart('resume')
                self.assertEqual(resumed['current']['question_budget']['asked'], 7)
            next_competence = self.competences[i % 2]
            if i == 5:
                pending = self.d.read()['current']['pending_prompt']
                state = self.d.save(dict(action='skip', prompt_revision=pending['revision'],
                    skip_reason='Need to review HTTP separately', next_difficulty='applied',
                    next_competence=next_competence, selection_reason='Cover the other competence'))
            else:
                state = self.answer(result, next_competence=next_competence)
            self.assertEqual(state['current']['question_budget']['asked'], i)
            if i == 12:
                self.assertTrue(state['selection_guidance']['near_end'])
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(len(state['current']['answers']), 14)
        self.assertEqual(len(state['current']['skips']), 1)
        self.assertEqual(state['current']['question_budget']['remaining'], 0)
        before = self.d.path.read_bytes()
        with self.assertRaises(ValueError): self.ask(16)
        self.assertEqual(self.d.path.read_bytes(), before)
        self.assertEqual(self.restart()['current'], state['current'])
        self.propose(state)

    def test_experienced_error_then_recheck_preserves_conflict_and_scope(self):
        self.onboard('significant_experience', text='Работаю Java Backend-разработчиком, углубляю HTTP и архитектуру')
        self.goal('Deepen Java Backend HTTP knowledge')
        self.assertEqual(self.accept()['current']['next_difficulty'], 'transfer')
        self.ask(1)
        state = self.answer('incorrect', next_difficulty='applied')
        self.assertEqual(state['selection_guidance']['action'], 'check_prerequisite')
        self.ask(2); state = self.answer(next_difficulty='transfer')
        self.assertEqual(state['selection_guidance']['action'], 'increase_or_change')
        self.ask(3); self.answer(next_competence=self.competences[1])
        state = self.d.save(dict(action='complete'))
        card = state['summary']['competence_map'][0]
        self.assertEqual(card['status'], 'gap')
        self.assertTrue(card['has_conflicts'])
        self.assertNotIn('transfer', card['confirmed_levels'])
        self.assertIn('diagnostic_completed_early', state['summary']['limitations'])
        self.assertEqual(self.restart()['status'], 'completed')
        self.propose(state)

    def test_design_ambiguous_calibration_help_skip_and_early_pending_completion(self):
        self.competences = ['Readable posters', 'Readable headings']
        self.onboard('unknown', subject='Дизайн', text='Где-то разбираюсь хорошо, но свой уровень не знаю')
        self.goal('Make readable poster designs')
        self.assertIsNone(self.d.read()['starting_guidance']['difficulty'])
        with self.assertRaises(ValueError): self.accept()
        self.assertFalse(self.d.path.exists())
        self.accept(next_difficulty='applied')
        self.ask(1, 'Readable posters')
        # Use a domain-specific answer rather than treating a design task as Java.
        self.answer('insufficient')
        self.assertEqual(self.d.read()['summary']['identified_gaps'], [])
        self.ask(2, 'Readable posters')
        state = self.answer(assistance='assisted', next_competence='Readable headings')
        self.assertEqual(state['selection_guidance']['action'], 'independent_recheck')
        self.ask(3, 'Readable headings')
        pending = self.d.read()['current']['pending_prompt']
        self.d.save(dict(action='skip', prompt_revision=pending['revision'], next_difficulty='applied',
            next_competence='Readable posters', selection_reason='Return to the uncertain criterion'))
        self.ask(4, 'Readable posters')
        state = self.d.save(dict(action='complete'))
        self.assertEqual(len(state['current']['answers']), 2)
        self.assertEqual(len(state['current']['skips']), 1)
        self.assertIsNotNone(state['current']['pending_prompt'])
        self.assertEqual(state['summary']['confirmed_knowledge'], [])
        self.assertEqual(state['summary']['identified_gaps'], [])
        self.restart(); self.propose(state)

    def test_mixed_results_recommendations_and_stale_cross_chat_answer(self):
        self.onboard('basic_experience'); self.goal('Use Java maps correctly'); self.accept()
        actions = [('partial', 'probe_gap'), ('insufficient', 'clarify_response'), ('correct', 'increase_or_change')]
        for i, (result, action) in enumerate(actions, 1):
            self.ask(i)
            state = self.answer(result, next_competence=self.competences[i % 2])
            self.assertEqual(state['selection_guidance']['action'], action)
        self.ask(4)
        stale = self.d.read()['revision']
        self.answer('incorrect')
        before = self.d.path.read_bytes()
        with self.assertRaises(ValueError):
            self.d.save(dict(action='pause'), revision=stale, session='stale-chat')
        self.assertEqual(self.d.path.read_bytes(), before)
        self.d.save(dict(action='pause')); self.restart()
        self.d.save(dict(action='resume'), session='another-chat')
        state = self.d.save(dict(action='complete'))
        self.assertEqual({a['assessment']['result'] for a in state['current']['answers']},
                         {'correct', 'partial', 'incorrect', 'insufficient'})
        self.propose(state)

    def test_legacy_journal_read_only_and_new_attempt_preserves_history(self):
        self.f.onboarding(dict(self_assessment='basics'))
        self.d.accept(); self.d.prompt(); self.d.answer('unknown', 'basic')
        self.d.save(dict(action='complete'))
        before = self.d.path.read_bytes()
        old_events = json.loads(before)['events']
        restored = self.restart()
        self.assertEqual(restored['summary']['identified_gaps'], [])
        self.assertIsNone(restored['summary']['competence_map'][0]['evidence'][0]['result'])
        self.assertNotIn('question_budget', restored['current'])
        self.assertEqual(before, self.d.path.read_bytes())
        state = self.accept()
        self.assertEqual(state['current']['question_budget']['asked'], 0)
        self.assertEqual(len(state['attempts']), 2)
        self.assertEqual(json.loads(self.d.path.read_text())['events'][:len(old_events)], old_events)


if __name__ == '__main__': unittest.main()
