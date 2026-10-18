import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asr_quality import diagnose


def rows(texts, **metrics):
    return [dict(start=i * 3, end=i * 3 + 3, text=t, **metrics) for i, t in enumerate(texts)]


class ASRQualityTests(unittest.TestCase):
    def test_empty_and_blank_are_not_ready(self):
        for data in ([], rows(['  ', '...'])):
            self.assertIn('no_speech_recognized_requires_review', diagnose(data)['warnings'])

    def test_loop_has_source_interval(self):
        data = rows(['Вводная'] + ['Ничего.'] * 30)
        result = diagnose(data)
        self.assertIn('asr_mass_repetition_requires_review', result['warnings'])
        self.assertEqual(result['repeated_runs'], [dict(start=3, end=93, segments=30)])

    def test_case_and_punctuation_do_not_hide_loop(self):
        self.assertTrue(diagnose(rows(['Ничего.', 'НИЧЕГО!'] * 5))['warnings'])

    def test_sparse_fillers_and_repeated_java_terms_are_allowed(self):
        data = rows([text for i in range(30) for text in
                     ('Окей.', f'HashMap пример {i}: ключ и значение')])
        # Nonconsecutive fillers are under half of all segments.
        data += rows(['Итог темы'])
        self.assertEqual(diagnose(data)['warnings'], [])

    def test_short_repetition_is_allowed(self):
        self.assertEqual(diagnose(rows(['Да'] * 7))['warnings'], [])

    def test_pauses_do_not_form_continuous_run(self):
        data = rows(['Да'] * 8)
        for i, row in enumerate(data):
            row.update(start=i * 30, end=i * 30 + 3)
        self.assertEqual(diagnose(data)['repeated_runs'], [])

    def test_metric_warning_needs_multiple_segments_and_share(self):
        data = rows([f'Текст {i}' for i in range(12)])
        for row in data[:3]:
            row['compression_ratio'] = 2.5
        self.assertIn('asr_suspicious_metrics_requires_review', diagnose(data)['warnings'])
        data.append(dict(start=36, end=39, text='Ещё'))
        self.assertEqual(diagnose(data)['warnings'], [])

    def test_no_speech_probability_alone_is_not_failure(self):
        data = rows([f'Текст {i}' for i in range(10)], no_speech_prob=.9)
        self.assertEqual(diagnose(data)['warnings'], [])
        for row in data:
            row['avg_logprob'] = -1.2
        self.assertTrue(diagnose(data)['warnings'])

    def test_missing_or_nonfinite_metrics_do_not_verify_text(self):
        data = rows(['Нормальный текст'], compression_ratio=float('nan'))
        original = copy.deepcopy(data)
        result = diagnose(data)
        self.assertEqual(result['status'], 'unverified')
        self.assertFalse(result['meaning_verified'])
        self.assertEqual(data[0]['text'], original[0]['text'])

    def test_diagnostic_detail_is_bounded(self):
        data = rows([t for i in range(30) for t in ([f'Цикл {i}'] * 8 + ['Переход'])])
        result = diagnose(data)
        self.assertEqual(result['repeated_run_count'], 30)
        self.assertEqual(len(result['repeated_runs']), 20)
