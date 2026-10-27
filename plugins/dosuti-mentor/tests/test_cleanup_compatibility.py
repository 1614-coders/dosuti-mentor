"""Guard validation side effects and compatibility surfaces during dead-code cleanup."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import learning_segments
import material_sources
import preparation_status
import storage_lock
import test_import_rollback as fixtures


class CleanupCompatibility(unittest.TestCase):
    def test_material_validation_precedes_acquisition_and_preserves_files(self):
        fixture = fixtures.ImportRollback(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        before = fixture.files()
        for source, message in [('', 'Source must be a nonempty path or URL'),
                                ('ftp://example.invalid/file', 'Use an HTTP'),
                                ('file:///outside.txt', 'Use an HTTP')]:
            with self.subTest(source=source), patch.object(material_sources, 'acquire') as acquire:
                with self.assertRaisesRegex(ValueError, message):
                    material_sources.attach_material(fixture.f.args(kind='homework', source=source))
                acquire.assert_not_called()
                self.assertEqual(fixture.files(), before)

    def test_validation_error_propagates_before_creating_materials(self):
        fixture = fixtures.ImportRollback(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        before = fixture.files()
        with patch.object(material_sources, 'source_kind', side_effect=ValueError('source validation sentinel')), \
             patch.object(material_sources, 'acquire') as acquire:
            with self.assertRaisesRegex(ValueError, '^source validation sentinel$'):
                material_sources.attach_material(fixture.f.args(kind='homework', source=str(fixture.source)))
        acquire.assert_not_called()
        self.assertEqual(fixture.files(), before)
        self.assertEqual(fixture.source.read_text(), 'user original')

    def test_preparation_step_remains_reachable_through_cli_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            argv = ['course.py', 'preparation-step', '--course', folder, '--lesson', 'lesson-01',
                    '--step', 'frames_review', '--time', '0', '--window', '60']
            output = io.StringIO(); expected = dict(reviewed='frames_review')
            with patch.object(sys, 'argv', argv), patch.object(preparation_status, 'record', return_value=expected) as record, \
                 contextlib.redirect_stdout(output):
                course.main()
            self.assertEqual(json.loads(output.getvalue()), expected)
            args = record.call_args.args[0]
            self.assertEqual((args.lesson, args.step, args.time, args.window), ('lesson-01', 'frames_review', 0, 60))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_legacy_segment_expansion_remains_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'unit-learning.json'
            old = dict(schema='unit_learning_v1', revision=1, events=[dict(type='checkpoint')])
            result = learning_segments.expand(path, old)
            self.assertEqual(result, old)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_locked_stream_compatibility_tracks_only_current_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.lock'
            self.assertIsNone(storage_lock.locked_stream(path))
            with storage_lock.file_lock(path) as stream:
                self.assertIs(storage_lock.locked_stream(path), stream)
            self.assertIsNone(storage_lock.locked_stream(path))
            self.assertTrue(stream.closed)


if __name__ == '__main__':
    unittest.main()
