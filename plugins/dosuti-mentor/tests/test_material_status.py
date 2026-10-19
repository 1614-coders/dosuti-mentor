"""Lifecycle reads reuse verified evidence without running preparation or changing storage."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import material_status as ms
import test_study_units
import test_unit_sources
from preparation_status import tracked, record


class MaterialStatus(unittest.TestCase):
    def setUp(self):
        self.f = test_study_units.StudyUnits(); self.f.setUp()
    def tearDown(self): self.f.tearDown()
    def view(self, **kw): return ms.view(self.f.args(**kw))
    def lesson(self): return self.view(lesson='lesson-01', time=0, window=20)['material']
    def unit(self): return self.view(unit_id='maps', lesson=None)['material']
    def prepare(self):
        self.f.f.add('a', 0, 60, [(5, 10, 'Keys')])
        @tracked
        def finish(args): pass
        finish(self.f.f.args)
    def review(self):
        record(self.f.args(step='frames_review', time=0, window=20))
        record(self.f.args(step='ready', time=0, window=20))

    def test_old_empty_course_and_selected_source_are_read_only(self):
        before = {p: p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file()}
        self.assertEqual(self.lesson()['status'], 'selected')
        result = self.view(lesson=None)
        self.assertEqual(result['status'], 'selected')
        self.assertFalse((self.f.f.path / 'study-units.json').exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file()})
        (self.f.f.lesson / 'lesson.json').unlink()
        self.assertEqual(self.view(lesson=None)['status'], 'materials_missing')

    def test_repeated_unit_reuses_same_hash_without_asr_or_ocr(self):
        self.f.save()
        before = {p: p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file()}
        with patch.object(course, 'prepare', side_effect=AssertionError('ASR forbidden')), \
             patch('screen_ocr.view', side_effect=AssertionError('OCR forbidden')):
            first, second = self.unit(), self.unit()
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'ready'); self.assertTrue(first['reuse_preparation'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file()})
        self.assertEqual(self.f.get()['material_status'], first)

    def test_uncertain_unit_and_changed_prerequisite(self):
        self.f.save(self.f.card(uncertainties=['Example needs clarification']))
        self.assertEqual(self.unit()['status'], 'limited')
        self.f.save(self.f.card('child', prerequisites=['maps']))
        self.f.save(self.f.card(title='New explanation'))
        result = self.view(unit_id='child', lesson=None)['material']
        self.assertEqual(result['status'], 'stale'); self.assertFalse(result['reuse_preparation'])

    def test_changed_video_invalidates_unit_and_lesson(self):
        self.prepare()
        self.f.save(self.f.card(sources=[dict(kind='video', role='teacher', notes='Reviewed', lesson='lesson-01', start=0, end=20)]))
        self.f.f.video.write_bytes(b'changed video')
        for result in (self.unit(), self.lesson()):
            self.assertEqual(result['status'], 'stale'); self.assertFalse(result['reuse_preparation'])

    def test_coverage_is_reusable_but_readiness_requires_scope_review(self):
        self.prepare()
        result = self.lesson()
        self.assertEqual(result['status'], 'limited'); self.assertTrue(result['reuse_preparation'])
        self.review()
        self.assertEqual(self.lesson()['status'], 'ready')
        result = self.view(lesson='lesson-01', time=30, window=20)['material']
        self.assertEqual(result['status'], 'limited'); self.assertTrue(result['reuse_preparation'])
        result = self.view(lesson='lesson-01', time=50, window=20)['material']
        self.assertEqual(result['status'], 'limited'); self.assertFalse(result['reuse_preparation'])
        self.assertTrue(result['gaps'])

    def test_changed_review_dependencies_require_review(self):
        self.prepare(); self.review()
        course.update_lesson(self.f.f.lesson, lambda data: data.update(title='Changed title'))
        self.assertEqual(self.lesson()['status'], 'stale')
        self.assertFalse(self.lesson()['reuse_preparation'])

    def test_live_and_cancelled_attempts_never_claim_ready(self):
        seen = []
        @tracked
        def cancel(args):
            seen.append(self.lesson())
            raise KeyboardInterrupt('cancelled')
        with self.assertRaises(KeyboardInterrupt): cancel(self.f.f.args)
        self.assertEqual(seen[0]['status'], 'preparing')
        self.assertEqual(self.lesson()['status'], 'limited')
        self.assertEqual(self.lesson()['preparation_status']['status'], 'interrupted')

    def test_cli_reads_without_creating_profile(self):
        result = subprocess.run([sys.executable, str(course.SCRIPT_DIR / 'course.py'), 'material-status',
                                 '--course', str(self.f.f.path), '--lesson', 'lesson-01', '--time', '0', '--window', '20'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['material']['status'], 'selected')

    def test_changed_current_code_and_homework_invalidate_unit(self):
        fixture = test_unit_sources.UnitSources(); fixture.setUp()
        try:
            fixture.attach('code', 'class A {\n}\n', 'streams'); fixture.save(fixture.code())
            fixture.attach('code', 'class B {\n}\n', 'streams')
            result = ms.view(fixture.args(unit_id='maps', lesson=None))['material']
            self.assertEqual(result['status'], 'stale'); self.assertFalse(result['reuse_preparation'])
            fixture.attach('homework', 'Task A\nExplain\n')
            fixture.save(dict(kind='homework', role='teacher', notes='Reviewed', lesson='lesson-01', start_line=1, end_line=2))
            fixture.attach('homework', 'Task B\nImplement\n')
            self.assertEqual(ms.view(fixture.args(unit_id='maps', lesson=None))['material']['status'], 'stale')
        finally: fixture.tearDown()
