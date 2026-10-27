"""Input normalization must precede the locked unit-learning write boundary."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_unit_learning as fixtures
import unit_learning as learning


class UnitEventInput(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.UnitLearning(); self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.input = self.f.f.f.directory / 'event-input.json'

    def snapshot(self):
        root = Path(self.f.args().course)
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def call(self, field, value, **options):
        self.input.write_text(json.dumps(value), encoding='utf-8')
        before = self.snapshot()
        with patch.object(learning, 'file_lock', side_effect=AssertionError('Input error entered write locks')):
            try:
                return learning.learn_unit(self.f.args(**{field: self.input}, **options))
            finally:
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(json.loads(self.input.read_text(encoding='utf-8')), value)

    def test_six_types_reach_existing_validator_before_locks(self):
        cases = [
            ('event_file', 'assessment', self.f.event()),
            ('checkpoint_file', 'checkpoint', dict(recap='Map', next_step='Task', pending_question=None)),
            ('review_file', 'review_decision', dict(choice='skip', criteria=[1], until=None, reason='Reviewed')),
            ('lesson_context_file', 'lesson_context', dict(kind='practice', topic='Java Maps', goal='Trace a key', plan=['Trace', 'Solve'])),
            ('lesson_state_file', 'lesson_state', dict(action='resume')),
            ('practice_file', 'practice', dict(action='task', rationale='Check Map replacement', id='map-1', question='Trace the map', criteria=[1])),
        ]
        for field, kind, value in cases:
            with self.subTest(kind=kind):
                context = kind == 'lesson_context'
                options = dict(session='chat-a', **({} if context else dict(lesson_id='lesson-a')))
                expected = dict(value)
                if kind in ('checkpoint', 'lesson_state', 'practice'): expected['lesson_id'] = 'lesson-a'
                with patch.object(learning, 'validate', side_effect=ValueError('validator sentinel')) as validate, \
                     patch.object(learning, 'validate_lesson_input', side_effect=ValueError('validator sentinel')) as lesson_validate:
                    with self.assertRaisesRegex(ValueError, '^validator sentinel$'):
                        self.call(field, value, **options)
                if context:
                    lesson_validate.assert_called_once_with(expected); validate.assert_not_called()
                else:
                    validate.assert_called_once_with(expected, kind); lesson_validate.assert_not_called()

    def test_conflicting_lesson_bindings_do_not_record(self):
        cases = [
            ('checkpoint_file', dict(lesson_id='other', recap='Map', next_step='Task', pending_question=None)),
            ('lesson_state_file', dict(lesson_id='other', action='resume')),
            ('practice_file', dict(action='task', rationale='Check Map replacement', id='map-1', question='Trace', criteria=[1], lesson_id='other')),
        ]
        for field, value in cases:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '^Conflicting lesson IDs$'):
                self.call(field, value, lesson_id='lesson-a', session='chat-a')

    def test_checkpoint_without_selected_lesson_keeps_embedded_binding(self):
        value = dict(lesson_id='lesson-a', recap='Map', next_step='Task', pending_question=None)
        with patch.object(learning, 'validate', side_effect=ValueError('validator sentinel')) as validate:
            with self.assertRaisesRegex(ValueError, '^validator sentinel$'):
                self.call('checkpoint_file', value, session='chat-a')
        validate.assert_called_once_with(value, 'checkpoint')

    def test_practice_help_is_not_rebound(self):
        value = dict(action='help', task_id='map-1', level='hint', content='Trace a key', request=None)
        with patch.object(learning, 'validate', side_effect=ValueError('validator sentinel')) as validate:
            with self.assertRaisesRegex(ValueError, '^validator sentinel$'):
                self.call('practice_file', value, lesson_id='lesson-a', session='chat-a')
        validate.assert_called_once_with(value, 'practice')

    def test_missing_session_precedes_revision_check(self):
        cases = [
            ('checkpoint_file', dict(recap='Map', next_step='Task', pending_question=None), {}, 'Checkpoint/lifecycle/practice requires --session ID'),
            ('lesson_state_file', dict(action='resume'), dict(lesson_id='lesson-a'), 'Checkpoint/lifecycle/practice requires --session ID'),
            ('practice_file', dict(action='task', rationale='Check Map replacement', id='map-1', question='Trace', criteria=[1]), {}, 'Checkpoint/lifecycle/practice requires --session ID'),
            ('lesson_context_file', dict(kind='new_topic', topic='Map', goal='Trace', plan=['Trace']), {}, 'New lesson requires --session ID'),
        ]
        for field, value, options, message in cases:
            with self.subTest(field=field), self.assertRaises(ValueError) as error:
                self.call(field, value, **options)
            self.assertEqual(str(error.exception), message)

    def test_input_error_precedes_session_check(self):
        with self.assertRaisesRegex(ValueError, '^Invalid unit checkpoint fields$'):
            self.call('checkpoint_file', {})
        with self.assertRaisesRegex(ValueError, '^Lesson state requires --lesson-id$'):
            self.call('lesson_state_file', {})

    def test_multiple_sources_reject_before_file_read(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, '^Choose one unit learning event$'):
            learning.learn_unit(self.f.args(event_file=self.input, checkpoint_file=self.input))
        self.assertEqual(self.snapshot(), before); self.assertFalse(self.input.exists())


if __name__ == '__main__': unittest.main()
