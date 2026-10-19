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


if __name__ == '__main__':
    unittest.main()
