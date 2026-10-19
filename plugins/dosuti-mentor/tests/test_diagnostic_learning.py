"""Voluntary diagnostics preserve concrete evidence without other learning writes."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import diagnostic_learning as dl
import learning_intent
import test_learning_tools


class DiagnosticLearning(unittest.TestCase):
    def setUp(self):
        self.f = test_learning_tools.LearningTools(); self.f.setUp()
        self.path = dl.path_for(self.f.a)
    def tearDown(self): self.f.tearDown()
    def read(self, **kw): return dl.diagnostic(self.f.args(**kw))
    def save(self, value, revision=None, **kw):
        if revision is None: revision = self.read()['revision']
        return dl.diagnostic(self.f.args(update_file=self.f.write('diagnostic-input.json', value),
            expected_revision=revision, reason='Actual QA learner decision', **kw))
    def accept(self, difficulty='basic'):
        return self.save(dict(action='accept', competences=['Trace a loop', 'Explain a map'], next_difficulty=difficulty))
    def prompt(self, competence='Trace a loop', difficulty='basic', format='open'):
        return self.save(dict(action='prompt', competence=competence, prompt='What does this loop print?', difficulty=difficulty, format=format))
    def answer(self, evidence='confirmed', next_difficulty='applied', next_competence='Trace a loop'):
        revision = self.read()['current']['pending_prompt']['revision']
        return self.save(dict(action='answer', prompt_revision=revision, answer='It prints 0 then 1',
            evaluation='Both iterations traced; only this example was checked', evidence=evidence, next_difficulty=next_difficulty, next_competence=next_competence,
            selection_reason=('Enough evidence for the agreed starting step; remaining areas are untested'
                              if next_competence is None else
                              'Check another example because only one trace was demonstrated')))

    def test_each_supported_prompt_format_is_preserved_with_answer(self):
        for format in ('single_choice', 'multiple_choice', 'open', 'practical',
                       'code_analysis', 'bug_search', 'explanation'):
            with self.subTest(format=format):
                self.accept(); self.prompt(format=format)
                self.assertEqual(self.read()['current']['pending_prompt']['format'], format)
                answer = self.answer()['current']['answers'][0]
                self.assertEqual(answer['format'], format)
                self.save({'action': 'complete'})

    def test_next_selection_constrains_prompt_across_pause_and_resume(self):
        self.accept(); self.prompt()
        current = self.answer(next_competence='Explain a map')['current']
        self.assertEqual(current['next_competence'], 'Explain a map')
        self.assertEqual(current['selection_reason'], current['answers'][0]['selection_reason'])
        self.assertEqual(current['answers'][0]['next_competence'], 'Explain a map')
        self.save({'action': 'pause'}); self.save({'action': 'resume'}, session='other-chat')
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): self.prompt(difficulty='applied')
        self.assertEqual(self.path.read_bytes(), before)
        self.prompt(competence='Explain a map', difficulty='applied')
        self.answer(next_difficulty='basic', next_competence='Trace a loop')
        self.prompt()  # Returning to a competence must follow the recorded selection too.

    def test_terminal_answer_can_complete_without_inventing_next_target(self):
        self.accept(); self.prompt()
        current = self.answer(next_competence=None)['current']
        self.assertIsNone(current['next_competence'])
        self.assertIsNone(current['answers'][-1]['next_competence'])
        self.assertTrue(current['selection_reason'])
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): self.prompt(difficulty='applied')
        self.assertEqual(self.path.read_bytes(), before)
        result = self.save({'action': 'complete'})
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(result['current']['pending_prompt'])
        self.assertEqual(len(result['current']['answers']), 1)
        with self.assertRaises(ValueError): self.prompt(difficulty='applied')
        self.accept(); self.prompt()  # A new attempt can ask questions again.

    def test_terminal_answer_cannot_resume_questions_and_requires_completion_reason(self):
        self.accept(); self.prompt()
        answer = dict(action='answer', prompt_revision=2, answer='It prints 0 then 1',
            evaluation='Both iterations traced; this checks only this example', evidence='confirmed',
            next_difficulty='basic', next_competence=None, selection_reason='')
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): self.save(answer)
        self.assertEqual(self.path.read_bytes(), before)
        self.save(answer | {'selection_reason': 'Learner chose to end after this answer; map remains untested'})
        self.save({'action': 'pause'}); self.save({'action': 'resume'})
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): self.prompt()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.save({'action': 'complete'})['status'], 'completed')

    def test_new_fields_invalid_or_missing_do_not_write(self):
        self.accept()
        prompt = dict(action='prompt', competence='Trace a loop', prompt='Q', difficulty='basic')
        before = self.path.read_bytes()
        for value in (prompt, prompt | {'format': 'quiz'}, prompt | {'format': []}, prompt | {'format': None}):
            with self.subTest(value=value), self.assertRaises(ValueError): self.save(value)
            self.assertEqual(self.path.read_bytes(), before)
        self.prompt()
        answer = dict(action='answer', prompt_revision=2, answer='I do not know',
                      evaluation='Insufficient evidence', evidence='unknown', next_difficulty='basic',
                      next_competence='Explain a map', selection_reason='Loop evidence is unknown; check map basics')
        before = self.path.read_bytes()
        invalid = [answer | {key: value} for key, value in (
            ('next_competence', 'Unknown'), ('next_competence', []),
            ('selection_reason', ''), ('selection_reason', None),
            ('selection_reason', ' '), ('selection_reason', 'x' * 4001))]
        invalid += [{k: v for k, v in answer.items() if k != key}
                    for key in ('next_competence', 'selection_reason')]
        invalid.append({k: v for k, v in answer.items() if k not in ('next_competence', 'selection_reason')})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError): self.save(value)
            self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_journal_keeps_exact_projection_and_bytes(self):
        self.accept(); self.prompt(); self.answer()
        journal = json.loads(self.path.read_text())
        for event in journal['events']:
            for key in ('format', 'next_competence', 'selection_reason'):
                event['value'].pop(key, None)
        self.path.write_text(json.dumps(journal))
        before = self.path.read_bytes()
        result = self.read(history_limit=100)
        expected_answer = dict(revision=3, competence='Trace a loop', prompt='What does this loop print?',
            difficulty='basic', **journal['events'][2]['value'])
        self.assertEqual(result['current'], dict(id=1, status='accepted',
            competences=['Trace a loop', 'Explain a map'], next_difficulty='applied',
            pending_prompt=None, answers=[expected_answer], last_action='answer', revision=3))
        self.assertEqual(result['history'], journal['events'])
        self.assertEqual(self.path.read_bytes(), before)
        self.prompt(competence='Explain a map', difficulty='applied')
        self.answer(next_difficulty='basic')
        self.assertEqual(self.read()['current']['next_competence'], 'Trace a loop')

    def test_invalid_extension_in_history_blocks_writes(self):
        self.accept(); self.prompt(); self.answer()
        original = json.loads(self.path.read_text())
        for index, key, value in ((1, 'format', 'quiz'), (2, 'next_competence', 'Unknown'),
                                   (2, 'selection_reason', ''), (2, 'selection_reason', None)):
            with self.subTest(key=key, value=value):
                journal = copy.deepcopy(original)
                journal['events'][index]['value'][key] = value
                self.path.write_text(json.dumps(journal)); before = self.path.read_bytes()
                self.assertFalse(self.read()['valid'])
                with self.assertRaises(ValueError): self.save({'action': 'complete'}, revision=3)
                self.assertEqual(self.path.read_bytes(), before)
        journal = copy.deepcopy(original)
        del journal['events'][2]['value']['selection_reason']
        self.path.write_text(json.dumps(journal)); before = self.path.read_bytes()
        self.assertFalse(self.read()['valid'])
        with self.assertRaises(ValueError): self.save({'action': 'complete'}, revision=3)
        self.assertEqual(self.path.read_bytes(), before)

    def test_old_profile_read_is_not_started_without_migration(self):
        value = self.read()
        self.assertTrue(value['valid']); self.assertEqual(value['revision'], 0)
        self.assertEqual(value['status'], 'not_started'); self.assertIsNone(value['current'])
        self.assertFalse(self.path.exists())

    def test_question_and_answer_cannot_start_diagnostic_implicitly(self):
        for value in [dict(action='prompt', competence='Trace a loop', prompt='Q', difficulty='basic', format='open'),
                      dict(action='answer', prompt_revision=1, answer='A', evaluation='E',
                           evidence='confirmed', next_difficulty='basic', next_competence='Trace a loop',
                           selection_reason='Check the untested competence')]:
            with self.assertRaises(ValueError): self.save(value, revision=0)
            self.assertFalse(self.path.exists())
            self.assertEqual(self.read()['status'], 'not_started')

    def test_completion_without_answers_preserves_unknowns(self):
        self.accept(); self.prompt()
        pending = self.read()['current']['pending_prompt']
        result = self.save({'action': 'complete'})['current']
        self.assertEqual(result['answers'], [])
        self.assertEqual(result['pending_prompt'], pending)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['competences'], ['Trace a loop', 'Explain a map'])

    def test_decline_does_not_prevent_learning_or_future_acceptance(self):
        self.assertEqual(self.save({'action': 'decline'})['status'], 'declined')
        learning_intent.commit(self.f.args(reason='User chose a plan', expected_revision=0),
                               {'goal': {'description': 'Learn loops'}})
        self.assertEqual(learning_intent.view(self.f.a)['goal']['description'], 'Learn loops')
        self.assertEqual(self.accept()['status'], 'accepted')
        self.assertEqual(len(self.read()['attempts']), 2)

    def test_beginner_and_experienced_answer_difficulty_control(self):
        for initial in ('basic', 'transfer'):
            self.accept(initial); self.prompt(difficulty=initial)
            value = self.answer('needs_practice', 'basic')['current']
            answer = value['answers'][0]
            self.assertEqual(answer['competence'], 'Trace a loop')
            self.assertEqual(answer['difficulty'], initial)
            self.assertEqual(answer['prompt'], 'What does this loop print?')
            self.assertEqual(answer['answer'], 'It prints 0 then 1')
            self.assertEqual(answer['evidence'], 'needs_practice')
            self.assertIn('only this example', answer['evaluation'])
            self.assertEqual(value['next_difficulty'], 'basic')
            with self.assertRaises(ValueError): self.prompt(difficulty='applied')
            self.prompt(difficulty='basic'); self.answer('unknown', 'applied')
            self.save({'action': 'complete'})

    def test_pause_resume_in_another_chat_preserves_pending_question(self):
        self.accept(); self.prompt(); pending = self.read()['current']['pending_prompt']
        self.assertEqual(self.save({'action': 'pause'}, session='chat-a')['status'], 'paused')
        with self.assertRaises(ValueError): self.answer()
        value = self.save({'action': 'resume'}, session='chat-b')
        self.assertEqual(value['current']['pending_prompt'], pending)
        self.answer(); self.assertIsNone(self.read()['current']['pending_prompt'])
        self.assertEqual(self.read(history_limit=100)['history'][-2]['session'], 'chat-b')

    def test_complete_with_unknowns_and_repeat_preserves_previous_diagnostic(self):
        self.accept(); self.prompt(); self.answer(); self.save({'action': 'complete'})
        old = copy.deepcopy(self.read()['current'])
        self.accept('transfer'); self.prompt(difficulty='transfer'); self.answer('needs_practice', 'basic')
        result = self.save({'action': 'complete'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'][0], old)
        self.assertEqual(result['current']['answers'][0]['evidence'], 'needs_practice')
        self.assertEqual(old['competences'][1], 'Explain a map')
        self.assertEqual(len(old['answers']), 1)  # Untested competence has no fabricated evidence.
        with self.assertRaises(ValueError): self.prompt()

    def test_stale_revision_and_invalid_transitions_leave_journal_unchanged(self):
        self.accept(); before = self.path.read_bytes()
        for value, revision in [({'action': 'decline'}, 1), ({'action': 'accept', 'competences': ['X'], 'next_difficulty': 'basic'}, 1),
                                ({'action': 'resume'}, 1), ({'action': 'pause'}, 0)]:
            with self.subTest(value=value), self.assertRaises(ValueError): self.save(value, revision)
            self.assertEqual(self.path.read_bytes(), before)
        self.prompt(); before = self.path.read_bytes()
        with self.assertRaises(ValueError): self.save(dict(action='answer', prompt_revision=1,
            answer='real', evaluation='real', evidence='confirmed', next_difficulty='basic',
            next_competence='Trace a loop', selection_reason='Check another trace'))
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError): self.prompt()

    def test_invalid_data_cannot_be_persisted(self):
        self.accept(); before = self.path.read_bytes()
        invalid = [None, [], {}, {'action': []}, {'action': 'pause', 'score': 10},
            {'action': 'accept', 'competences': ['X', 'X'], 'next_difficulty': 'basic'},
            {'action': 'prompt', 'competence': 'Unknown', 'prompt': 'Q', 'difficulty': 'basic', 'format': 'open'},
            {'action': 'prompt', 'competence': 'Trace a loop', 'prompt': '', 'difficulty': 'basic', 'format': 'open'},
            {'action': 'prompt', 'competence': 'Trace a loop', 'prompt': 'Q', 'difficulty': 5, 'format': 'open'}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError): self.save(value)
            self.assertEqual(self.path.read_bytes(), before)
        self.prompt(); before = self.path.read_bytes()
        baseline = dict(action='answer', prompt_revision=2, answer='A', evaluation='E', evidence='confirmed', next_difficulty='applied',
                        next_competence='Trace a loop', selection_reason='Check another trace')
        for key, value in [('answer', ''), ('evaluation', None), ('evidence', 'expert'), ('evidence', []),
                           ('prompt_revision', True), ('next_difficulty', 'expert')]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.save(baseline | {key: value})
            self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_or_unknown_journal_is_not_overwritten(self):
        self.accept(); original = json.loads(self.path.read_text())
        variants = [dict(original, schema='future'), dict(original, revision=True), {'schema': dl.SCHEMA}, []]
        bad = copy.deepcopy(original); bad['events'][0]['value']['next_difficulty'] = 'expert'; variants.append(bad)
        bad = copy.deepcopy(original); bad['events'][0]['at'] = 'yesterday'; variants.append(bad)
        for value in variants:
            self.path.write_text(json.dumps(value)); before = self.path.read_bytes()
            self.assertFalse(self.read()['valid'])
            with self.assertRaises(ValueError): self.save({'action': 'complete'}, revision=1)
            self.assertEqual(self.path.read_bytes(), before)

    def test_linked_storage_and_oversized_write_rejected(self):
        self.accept(); before = self.path.read_bytes()
        with patch.object(dl, 'MAX_BYTES', 10), self.assertRaises(ValueError): self.save({'action': 'complete'})
        self.assertEqual(self.path.read_bytes(), before)
        target = self.f.base / 'target'; self.path.rename(target); self.path.symlink_to(target)
        self.assertFalse(self.read()['valid'])
        with self.assertRaises(ValueError): self.save({'action': 'complete'}, revision=1)
        self.path.unlink(); os.link(target, self.path)
        self.assertFalse(self.read()['valid']); self.assertEqual(target.read_bytes(), before)

    def test_concurrent_writers_only_one_wins(self):
        self.accept()
        incoming = self.f.write('pause.json', {'action': 'pause'})
        def save(_):
            try:
                dl.diagnostic(self.f.args(update_file=incoming, expected_revision=1, reason='Pause')); return True
            except ValueError: return False
        with ThreadPoolExecutor(2) as pool: self.assertEqual(sorted(pool.map(save, range(2))), [False, True])
        self.assertEqual(self.read()['revision'], 2)

    def test_profile_course_isolation_portability_and_no_other_writes(self):
        self.f.prefs(values=['style=partner'])
        learning_intent.commit(self.f.args(reason='Goal', expected_revision=0), {'goal': {'description': 'Learn'}})
        intent = learning_intent.path_for(self.f.a).read_bytes()
        prefs = self.f.prefs()
        self.accept(); self.prompt(); self.answer(); self.save({'action': 'complete'})
        self.assertEqual(learning_intent.path_for(self.f.a).read_bytes(), intent)
        self.assertEqual(self.f.prefs(), prefs)
        self.assertFalse((self.path.parent / 'unit-learning.json').exists())
        self.assertEqual(self.read(profile='other')['status'], 'not_started')
        with course.db(self.f.a) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 0)
        other = self.f.base / 'other-course'
        with contextlib.redirect_stdout(io.StringIO()): course.init(self.f.args(course=other, title='Other'))
        self.assertEqual(self.read(course=other)['status'], 'not_started')
        moved = self.f.base / 'moved'; shutil.move(self.f.a.course, moved); self.f.a.course = moved
        self.assertEqual(self.read()['status'], 'completed')

    def test_cli_read_and_write(self):
        incoming = self.f.write('decline.json', {'action': 'decline'})
        output = io.StringIO()
        with patch.object(sys, 'argv', ['course.py', 'diagnostic', '--course', str(self.f.a.course),
             '--profile', 'qa', '--session', 'chat-a', '--update-file', str(incoming),
             '--expected-revision', '0', '--reason', 'User declined', '--history-limit', '1']), contextlib.redirect_stdout(output):
            course.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'declined'); self.assertEqual(result['history'][0]['session'], 'chat-a')


if __name__ == '__main__': unittest.main()
