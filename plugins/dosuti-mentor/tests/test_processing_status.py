"""Status is observational, bounded, and never promotes old or killed jobs to ready."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from processing_status import Reporter, describe
from storage_lock import file_lock


class ProcessingStatus(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = self.root / 'processing-status.json'

    def report(self, stage, status='running'):
        with contextlib.redirect_stdout(io.StringIO()):
            Reporter(self.root).set(stage, status)

    def test_legacy_status_does_not_create_files(self):
        (self.root / 'checkpoint.json').write_text('{"status":"complete"}')
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(describe(self.root), {'status': 'unavailable'})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_stopped_worker_keeps_last_observed_elapsed(self):
        with file_lock(self.root / '.processing.lock'):
            self.report('asr')
        original = self.status.read_bytes()
        with patch('processing_status.time.time', return_value=10**10):
            data = describe(self.root)
        self.assertEqual(data['status'], 'interrupted')
        self.assertLess(data['elapsed_seconds'], 1)
        self.assertEqual(original, self.status.read_bytes())

    def test_unreadable_shapes_are_unknown(self):
        with file_lock(self.root / '.processing.lock'):
            pass
        for value in ('{', '[]', '{}', '{"schema":"future"}', 'x' * 65537):
            self.status.write_text(value)
            self.assertEqual(describe(self.root), {'status': 'unavailable'})

    def test_prepare_only_does_not_claim_recognition(self):
        with file_lock(self.root / '.processing.lock'):
            self.report('prepared', 'complete')
        data = describe(self.root)
        self.assertEqual(data['stage'], 'prepared')
        self.assertIn('ещё не распознана', data['label'])

    def test_status_symlink_is_not_followed(self):
        target = self.root / 'external.json'
        target.write_text('{}')
        try:
            self.status.symlink_to(target)
        except OSError:
            self.skipTest('Symbolic links unavailable')
        self.assertEqual(describe(self.root), {'status': 'unavailable'})
        self.assertEqual(target.read_text(), '{}')
