"""Acceptance journey across real local stores; prepared media is synthetic QA evidence."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import compact_lesson as compact
import course
import diagnostic_learning as diagnostic
import learning_intent as intent
import learning_tools as learning
import material_status
import study_units
import unit_learning
import test_compact_lesson


class LearningJourney(unittest.TestCase):
    def setUp(self):
        self.media = test_compact_lesson.Compact()
        self.media.setUp()
        self.addCleanup(self.media.doCleanups)
        self.fixture = self.media.f.f
        self.video = self.media.managed()
        self.base = self.fixture.directory

    def args(self, **values):
        return NS(**(vars(self.fixture.args) | dict(profile='journey', session='chat-a') | values))

    def write(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def settings(self, **values):
        return learning.settings(self.args(scope='profile', set=[], reset=False, onboarding=None, **values))

    def diagnose(self, value):
        return diagnostic.diagnostic(self.args(
            update_file=self.write('diagnostic-input.json', value),
            expected_revision=diagnostic.view(self.args())['revision'], reason='QA learner decision'))

    def decide(self, value):
        intent.commit(self.args(expected_revision=intent.view(self.args())['revision'],
                                reason='QA learner decision'), value)
        return intent.view(self.args())

    def learn(self, value, *, checkpoint=False, session='chat-a'):
        current = unit_learning.learn_unit(self.args(unit_id='example'))
        return unit_learning.learn_unit(self.args(
            unit_id='example', session=session,
            **{'checkpoint_file' if checkpoint else 'event_file': self.write('learning-input.json', value)},
            expected_revision=current['learning']['revision'],
            expected_unit_sha256=current['unit']['content_sha256']))

    def test_full_onboarding_to_shared_learning_and_compact_review(self):
        self.assertEqual(learning.preferences(self.args())['onboarding'], 'not_asked')
        self.assertEqual(diagnostic.view(self.args())['status'], 'not_started')
        answers = dict(outcome=['topics', 'skill'], subject='Programming', self_assessment='basics',
                       learning_format='step_by_step', assistant_role='mentor', check_frequency='adaptive',
                       explanation_detail='detailed', constraints=dict(selected=['study_time'], details='Two evenings'))
        preferences = self.settings(onboarding_file=self.write('onboarding-input.json', dict(answers=answers)))
        self.assertEqual(preferences['onboarding'], 'complete')
        self.assertEqual({key: row['value'] for key, row in preferences['onboarding_answers'].items()}, answers)
        self.assertEqual(preferences['effective'], learning.DEFAULTS)
        self.assertFalse(diagnostic.path_for(self.args()).exists())
        self.assertFalse(intent.path_for(self.args()).exists())

        self.assertEqual(self.diagnose(dict(action='accept', competences=['Read an assignment'],
                                           next_difficulty='basic'))['status'], 'accepted')
        prompted = self.diagnose(dict(action='prompt', competence='Read an assignment',
                                     prompt='What is x after int x = 1?', difficulty='basic', format='code_analysis'))
        self.assertEqual(prompted['current']['answers'], [])
        prompt_revision = prompted['current']['pending_prompt']['revision']
        answered = self.diagnose(dict(action='answer', prompt_revision=prompt_revision, answer='x is 1',
                                     evaluation='Correct for this single assignment', evidence='confirmed',
                                     next_difficulty='applied', next_competence=None,
                                     selection_reason='Enough evidence for the first example; transfer remains untested'))
        answer_revision = answered['revision']
        self.assertIsNone(answered['current']['next_competence'])
        completed = self.diagnose(dict(action='complete'))
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(completed['current']['answers'][0]['answer'], 'x is 1')
        self.assertEqual(completed['current']['answers'][0]['format'], 'code_analysis')

        card = dict(id='example', title='Assignment example', summary='Read one assignment',
                    objectives=['Read the assigned value'], criteria=['Explain a new assignment independently'],
                    prerequisites=[], uncertainties=[], sources=[dict(kind='video', role='teacher',
                    notes='Synthetic prepared transcript and sampled frame', lesson='lesson-01', start=0, end=60)])
        study_units.unit(self.args(unit_file=self.write('unit-input.json', card), expected_revision=0))
        status = material_status.view(self.args(unit_id='example', lesson=None))['material']
        self.assertEqual(status['status'], 'ready')
        self.assertTrue(status['reuse_preparation'])
        unit = unit_learning.learn_unit(self.args(unit_id='example'))
        self.assertFalse(unit['unit']['stale'])
        self.assertEqual(unit['unit']['source_contexts'][0]['context']['speech'][0]['text'], 'test')
        self.assertFalse(unit['learning']['all_criteria_demonstrated'])

        self.decide(dict(goal=dict(description='Read and explain simple assignments')))
        proposed = self.decide(dict(proposal=dict(summary='Start with the prepared example',
            steps=['Study the assignment example'], step_bases=[dict(unit_ids=['example'],
                diagnostic_event_revisions=[answer_revision])])))
        self.assertIsNone(proposed['plan'])
        self.assertEqual(proposed['proposal']['route_state'], 'proposed')
        basis = proposed['proposal']['basis_status']['diagnostic_evidence'][str(answer_revision)]
        self.assertEqual(basis['answer'], 'x is 1')
        accepted = self.decide(dict(accept_proposal=True))
        self.assertEqual(accepted['plan']['route_state'], 'accepted')

        checkpoint_a = dict(recap='Read the example', next_step='Explain a new assignment', pending_question='What is y?')
        checkpoint_b = dict(recap='Reviewing saved result', next_step='Practice another example', pending_question=None)
        self.learn(checkpoint_a, checkpoint=True)
        self.learn(dict(stage='explained', details='Explained the source example', question=None, answer=None,
                        hints=0, criteria=[]))
        assessed = self.learn(dict(stage='solved_independently', details='Correct on a new assignment',
                                  question='What is y after int y = 2?', answer='y is 2', hints=0, criteria=[1]))
        self.assertTrue(assessed['learning']['all_criteria_demonstrated'])
        self.learn(checkpoint_b, checkpoint=True, session='chat-b')
        other = unit_learning.learn_unit(self.args(unit_id='example', session='chat-b', history_limit=100))
        self.assertEqual(other['learning']['attempt_count'], 1)
        actual_answers = [event['value']['answer'] for event in other['learning']['history']
                          if event['type'] == 'assessment' and event['value']['answer'] is not None]
        self.assertEqual(actual_answers, ['y is 2'])
        self.assertTrue(other['learning']['all_criteria_demonstrated'])
        self.assertEqual(other['learning']['checkpoint']['event']['value'], checkpoint_b)
        self.assertEqual(unit_learning.learn_unit(self.args(unit_id='example'))['learning']['checkpoint']['event']['value'], checkpoint_a)
        self.assertEqual(intent.view(self.args(session='chat-b'))['plan'], accepted['plan'])

        preserved = [learning.pref_file(self.args(), 'profile'), diagnostic.path_for(self.args()),
                     intent.path_for(self.args()), self.fixture.path / 'learners/journey/unit-learning.json',
                     self.fixture.path / 'study-units.json']
        before = {path: path.read_bytes() for path in preserved}
        compact.apply(self.media.directory, self.media.plan())
        self.assertFalse(self.video.exists())
        self.assertFalse((self.media.f.pub / 'audio.wav').exists())
        self.assertTrue(self.fixture.video.exists())
        with patch.object(course, 'download_video', side_effect=AssertionError('Unexpected download')), \
             patch.object(course, 'prepare', side_effect=AssertionError('Unexpected preparation')):
            review = unit_learning.learn_unit(self.args(unit_id='example', session='chat-b'))
            reused = material_status.view(self.args(unit_id='example', lesson=None))['material']
        self.assertFalse(review['unit']['stale'])
        self.assertTrue(review['learning']['all_criteria_demonstrated'])
        self.assertEqual(review['learning']['checkpoint']['event']['value'], checkpoint_b)
        context = review['unit']['source_contexts'][0]['context']
        self.assertEqual(context['speech'], unit['unit']['source_contexts'][0]['context']['speech'])
        self.assertTrue(context['lecture_memory']['memory_available'])
        self.assertTrue(reused['reuse_preparation'])
        self.assertEqual({path: path.read_bytes() for path in preserved}, before)

    def test_skipped_onboarding_and_declined_diagnostic_allow_preliminary_proposal(self):
        skipped = self.settings(onboarding_file=self.write('onboarding-input.json', dict(answers=dict(subject='Programming'))))
        self.assertEqual(skipped['onboarding'], 'in_progress')
        skipped = learning.settings(self.args(scope='profile', set=[], reset=False, onboarding='skipped'))
        self.assertEqual(skipped['onboarding_pending'], [])
        self.assertEqual(learning.preferences(self.args(session='chat-b'))['onboarding'], 'skipped')
        self.assertEqual(skipped['onboarding_answers']['subject']['value'], 'Programming')
        declined = self.diagnose(dict(action='decline'))
        self.assertEqual(declined['status'], 'declined')
        self.decide(dict(goal=dict(description='Explore basic programming')))
        proposed = self.decide(dict(proposal=dict(summary='Preliminary route without assessment', steps=['Explore assignments'])))
        self.assertEqual(proposed['proposal']['route_state'], 'preliminary')
        self.assertIsNone(proposed['plan'])
        self.assertEqual(diagnostic.view(self.args())['current']['answers'], [])
        self.assertFalse((self.fixture.path / 'learners/journey/unit-learning.json').exists())

    def test_shared_progress_uses_diagnostic_results_and_advances_accepted_route(self):
        self.diagnose(dict(action='accept', competences=['Explain assignment', 'Trace loop'],
                           next_difficulty='basic'))
        first = self.diagnose(dict(action='prompt', competence='Explain assignment',
                                  prompt='What is assigned to x?', difficulty='basic', format='open'))
        self.diagnose(dict(action='answer', prompt_revision=first['current']['pending_prompt']['revision'],
                           answer='I do not know', evaluation='Cannot explain assignment', evidence='unknown',
                           next_difficulty='basic', next_competence='Trace loop',
                           selection_reason='Assignment is an identified gap'))
        second = self.diagnose(dict(action='prompt', competence='Trace loop',
                                   prompt='How many iterations?', difficulty='basic', format='open'))
        diagnostic = self.diagnose(dict(action='answer', prompt_revision=second['current']['pending_prompt']['revision'],
                                       answer='Two', evaluation='Correct trace', evidence='confirmed',
                                       next_difficulty='applied', next_competence=None,
                                       selection_reason='Enough evidence for this diagnostic'))
        self.diagnose(dict(action='complete'))

        card = dict(id='example', title='Assignment example', summary='Read one assignment',
                    objectives=['Read the assigned value'], criteria=['Explain a new assignment independently'],
                    prerequisites=[], uncertainties=[], sources=[dict(kind='video', role='teacher',
                    notes='Synthetic prepared transcript and sampled frame', lesson='lesson-01', start=0, end=60)])
        study_units.unit(self.args(unit_file=self.write('unit-input.json', card), expected_revision=0))
        answer_revisions = [answer['revision'] for answer in diagnostic['current']['answers']]
        self.decide(dict(goal=dict(description='Read and explain simple assignments')))
        self.decide(dict(proposal=dict(summary='Close assignment gap, then transfer it',
            steps=['Practice assignments', 'Transfer assignment knowledge'], step_bases=[
                dict(unit_ids=['example'], diagnostic_event_revisions=[answer_revisions[0]]),
                dict(prerequisites=[1], diagnostic_event_revisions=[answer_revisions[1]])])))
        self.decide(dict(accept_proposal=True))

        before = intent.view(self.args(session='chat-a'))['learning_progress']
        self.assertEqual(before['next_step']['title'], 'Practice assignments')
        self.assertEqual(before['next_step']['diagnostic_guidance'], 'collect_diagnostic_evidence')
        self.assertEqual(before['diagnostic']['identified_gaps'], [])
        self.assertIn('Explain assignment',before['diagnostic']['unverified_areas'])
        self.assertEqual(before['demonstrated_units'], [])

        self.learn(dict(stage='solved_independently', details='Correct on a new assignment',
                        question='What is y after int y = 2?', answer='y is 2', hints=0, criteria=[1]))
        after = intent.view(self.args(session='chat-b'))['learning_progress']
        self.assertEqual(after['completed_steps'], [1])
        self.assertEqual(after['next_step']['title'], 'Transfer assignment knowledge')
        self.assertEqual(after['demonstrated_units'], ['example'])

        unit_view = unit_learning.learn_unit(self.args(unit_id='example', session='chat-b'))
        self.assertEqual(unit_view['learning_progress'], after)
        self.assertEqual(learning.resume(self.args(session='chat-b'))['learning_progress'], after)
        stream = __import__('io').StringIO()
        with __import__('contextlib').redirect_stdout(stream):
            course.status(self.args(session='chat-b'))
        self.assertEqual(json.loads(stream.getvalue())['learning_progress'], after)
        stream = __import__('io').StringIO()
        legacy_args = NS(**(vars(self.args(session='chat-b')) | dict(
            lesson='lesson-01', topic='Legacy lesson note', stage='explained', details='Explained source')))
        with __import__('contextlib').redirect_stdout(stream):
            course.progress(legacy_args)
        saved=json.loads(stream.getvalue())['learning_progress']
        self.assertEqual(saved['demonstrated_units'],after['demonstrated_units'])
        self.assertEqual(saved['completed_steps'],after['completed_steps'])
        self.assertEqual(saved['studied_lessons'][0]['topic'],'Legacy lesson note')
        self.assertEqual(saved['confirmed_lesson_knowledge'],[])

    def test_acceptance_cycle_ten_questions_route_handoff_and_safe_cleanup(self):
        answers = dict(outcome=['profession'], subject='Java backend', self_assessment='basics',
                       learning_format='step_by_step', assistant_role='mentor', check_frequency='adaptive',
                       explanation_detail='detailed', constraints=dict(selected=['study_time'], details='Three evenings'))
        self.assertEqual(self.settings(onboarding_file=self.write('onboarding-input.json', dict(answers=answers)))['onboarding'], 'complete')
        competences = ['Read assignment', 'Trace condition', 'Explain loop', 'Use a map', 'Read stacktrace',
                       'Write method', 'Test boundary', 'Find null bug', 'Explain OOP', 'Connect HTTP handler']
        self.diagnose(dict(action='accept', competences=competences, next_difficulty='basic'))
        answer_revisions = {}
        difficulty = 'basic'
        for index, competence in enumerate(competences):
            prompted = self.diagnose(dict(action='prompt', competence=competence,
                                          prompt='QA question for ' + competence, difficulty=difficulty, format='open'))
            if index == 4:
                skipped = self.diagnose(dict(action='skip', prompt_revision=prompted['current']['pending_prompt']['revision'],
                                              skip_reason='No time in this session', next_difficulty='basic',
                                              next_competence=competences[index + 1],
                                              selection_reason='Continue with another remaining competence'))
                self.assertEqual(len(skipped['current']['answers']), 4)
                difficulty = 'basic'
                continue
            evidence = 'unknown' if index in (1, 7) else ('needs_practice' if index == 2 else 'confirmed')
            difficulty = 'basic' if evidence == 'unknown' else ('transfer' if index in (3, 8) else 'applied')
            next_competence = competences[index + 1] if index + 1 < len(competences) else None
            diagnosed = self.diagnose(dict(action='answer', prompt_revision=prompted['current']['pending_prompt']['revision'],
                                            answer='QA answer ' + str(index), evaluation='QA evaluation ' + evidence,
                                            evidence=evidence, next_difficulty=difficulty, next_competence=next_competence,
                                            selection_reason='QA adaptive choice from recorded answer'))
            answer_revisions[competence] = diagnosed['revision']
            if index == 2:
                self.assertEqual(self.diagnose(dict(action='pause'))['status'], 'paused')
                self.assertEqual(self.diagnose(dict(action='resume'))['status'], 'accepted')
        finished = self.diagnose(dict(action='complete'))
        summary = finished['summary']
        self.assertEqual(len(finished['current']['answers']), 9)
        self.assertEqual(summary['identified_gaps'], [])
        self.assertEqual(summary['unverified_areas'], ['Trace condition', 'Read stacktrace', 'Find null bug'])
        self.assertEqual(summary['skipped_questions'][0]['skip_reason'], 'No time in this session')

        card = dict(id='example', title='Assignment example', summary='Read one assignment',
                    objectives=['Read the assigned value'], criteria=['Explain a new assignment independently'],
                    prerequisites=[], uncertainties=[], sources=[dict(kind='video', role='teacher',
                    notes='Synthetic prepared transcript and sampled frame', lesson='lesson-01', start=0, end=60)])
        study_units.unit(self.args(unit_file=self.write('unit-input.json', card), expected_revision=0))
        self.assertEqual(material_status.view(self.args(unit_id='example', lesson=None))['material']['status'], 'ready')
        self.decide(dict(goal=dict(description='Become a Java backend developer', purpose='First internship')))
        self.decide(dict(proposal=dict(summary='Close the observed gap, then use it in a transfer task',
            steps=['Practice assignment basics', 'Use the result in a transfer task'], step_bases=[
                dict(unit_ids=['example'], diagnostic_event_revisions=[answer_revisions['Trace condition']]),
                dict(prerequisites=[1], diagnostic_event_revisions=[answer_revisions['Read assignment']])])))
        accepted = self.decide(dict(accept_proposal=True))
        self.assertEqual(accepted['plan']['route_state'], 'accepted')
        before = accepted['learning_progress']
        self.assertEqual(before['next_step']['diagnostic_guidance'], 'collect_diagnostic_evidence')

        checkpoint = dict(recap='Prepared assignment material', next_step='Solve transfer task', pending_question='What is y?')
        self.learn(checkpoint, checkpoint=True)
        self.learn(dict(stage='explained', details='Explained prepared source', question=None, answer=None, hints=0, criteria=[]))
        self.learn(dict(stage='solved_independently', details='Solved new transfer task', question='What is y?',
                        answer='y is 2', hints=0, criteria=[1]))
        other_chat = unit_learning.learn_unit(self.args(unit_id='example', session='chat-b', history_limit=20))
        self.assertTrue(other_chat['learning']['all_criteria_demonstrated'])
        self.assertEqual(other_chat['learning_progress']['completed_steps'], [1])
        self.assertEqual(other_chat['learning_progress']['next_step']['title'], 'Use the result in a transfer task')
        self.assertEqual(unit_learning.learn_unit(self.args(unit_id='example', session='chat-a'))['learning']['checkpoint']['event']['value'], checkpoint)
        self.assertEqual(__import__('adaptive_learning').study_next(self.args(session='chat-b', limit=5))['learning_progress'], other_chat['learning_progress'])

        preserved = [learning.pref_file(self.args(), 'profile'), diagnostic.path_for(self.args()),
                     intent.path_for(self.args()), self.fixture.path / 'learners/journey/unit-learning.json']
        before_cleanup = {path: path.read_bytes() for path in preserved}
        compact.apply(self.media.directory, self.media.plan())
        with patch.object(course, 'download_video', side_effect=AssertionError('Unexpected download')), \
             patch.object(course, 'prepare', side_effect=AssertionError('Unexpected preparation')):
            self.assertTrue(unit_learning.learn_unit(self.args(unit_id='example', session='chat-b'))['learning']['all_criteria_demonstrated'])
        self.assertEqual({path: path.read_bytes() for path in preserved}, before_cleanup)

        changed = self.decide(dict(goal=dict(description='Build a desktop Java application')))
        self.assertTrue(changed['plan']['needs_review'])
        self.assertIsNone(changed['learning_progress']['next_step'])
        self.assertEqual(diagnostic.view(self.args())['summary'], summary)


if __name__ == '__main__':
    unittest.main()
