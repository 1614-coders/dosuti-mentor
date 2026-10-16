import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pilot

class Pilot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'Тест курса с пробелами'
    def event(self, **kw):
        return dict(step='environment', status='blocked', observation='Ошибка окружения', tester_feedback='', **kw)
    def test_start_isolated_and_refuses_existing(self):
        result = pilot.start(self.root)
        self.assertEqual(result['profile'], 'pilot')
        self.assertTrue((self.root / 'course/course.json').is_file())
        self.assertTrue(all(v['status'] == 'not_checked' for v in result['steps'].values()))
        before = (self.root/'course/course.json').read_bytes()
        with self.assertRaises(FileExistsError): pilot.start(self.root)
        self.assertEqual(before, (self.root/'course/course.json').read_bytes())
    def test_revision_history_feedback_and_export(self):
        pilot.start(self.root)
        pilot.record(self.root, 0, self.event())
        before = (self.root/'pilot.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'conflict'): pilot.record(self.root, 0, self.event())
        self.assertEqual(before, (self.root/'pilot.json').read_bytes())
        value = self.event(); value.update(status='passed', tester_feedback='Теперь работает')
        pilot.record(self.root, 1, value)
        result = pilot.export(self.root)
        report = Path(result['report']).read_text(encoding='utf-8')
        self.assertIn('resume: not_checked', report)
        self.assertIn('Теперь работает', report)
        self.assertIn('environment / blocked', report)
        self.assertFalse(result['automatically_sent'])
        self.assertEqual(len(pilot.load(self.root)['events']), 2)
    def test_invalid_and_failure_preserve_journal(self):
        pilot.start(self.root)
        before = (self.root/'pilot.json').read_bytes()
        for value in ({}, dict(step='practice',status='passed',observation='',tester_feedback=''),dict(step='unknown',status='passed',observation='x',tester_feedback='')):
            with self.assertRaises(ValueError): pilot.record(self.root, 0, value)
        with patch('pilot.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): pilot.record(self.root, 0, self.event())
        self.assertEqual(before, (self.root/'pilot.json').read_bytes())
    def test_report_survives_missing_course(self):
        pilot.start(self.root)
        (self.root/'course/course.json').unlink()
        self.assertTrue(Path(pilot.export(self.root)['report']).exists())
    def test_corrupt_history_is_refused(self):
        pilot.start(self.root)
        path=self.root/'pilot.json'
        data=json.loads(path.read_text()); data['events']=[{}]
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError): pilot.export(self.root)
    def test_legacy_pilot_keeps_history_after_rename(self):
        pilot.start(self.root)
        pilot.record(self.root,0,self.event())
        path=self.root/'pilot.json'
        data=json.loads(path.read_text());data['schema']='lecture_tutor_pilot_v1'
        path.write_text(json.dumps(data))
        before=path.read_bytes()
        pilot.export(self.root)
        self.assertEqual(before,path.read_bytes())
        pilot.record(self.root,1,self.event())
        self.assertEqual(pilot.load(self.root)['revision'],2)
    def test_missing_record_does_not_create_directory(self):
        with self.assertRaises(ValueError): pilot.record(self.root,0,self.event())
        self.assertFalse(self.root.exists())
    def test_simultaneous_writers_keep_one_event(self):
        from concurrent.futures import ThreadPoolExecutor
        pilot.start(self.root)
        def write(_):
            try:
                pilot.record(self.root, 0, self.event())
                return 'saved'
            except ValueError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(write, range(2))), ['saved', 'conflict'])
        self.assertEqual(pilot.load(self.root)['revision'], 1)
    def test_linked_report_is_not_overwritten(self):
        pilot.start(self.root)
        target=self.root/'keep.txt';target.write_text('keep')
        (self.root/'TEST_REPORT.md').symlink_to(target)
        with self.assertRaises(ValueError): pilot.export(self.root)
        self.assertEqual(target.read_text(), 'keep')
