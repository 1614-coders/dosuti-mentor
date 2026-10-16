"""Exercise argument dispatch at both public entry points, without running ASR."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import lecture


class SamplingCLI(unittest.TestCase):
    def dispatch(self, module, extra):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / 'video.mp4'
            video.write_bytes(b'argument-dispatch-only')
            if module is lecture:
                argv = ['lecture.py', str(video), '--output', str(Path(temp) / 'job')]
                target = 'run_job'
            else:
                argv = ['course.py', 'prepare', '--course', temp, '--lesson', 'sample']
                target = 'prepare'
            with patch.object(sys, 'argv', argv + extra), patch.object(module, target) as handler, patch.dict(os.environ):
                module.main()
            handler.assert_called_once()
            return handler.call_args.args[0]

    def test_both_defaults_reach_processing_as_ten_seconds(self):
        for module in (lecture, course):
            with self.subTest(entry=module.__name__):
                self.assertEqual(self.dispatch(module, []).frame_every, 10)

    def test_explicit_sampling_is_preserved_at_both_entries(self):
        for module in (lecture, course):
            for value in ('0.5', '5', '20'):
                with self.subTest(entry=module.__name__, value=value):
                    self.assertEqual(self.dispatch(module, ['--frame-every', value]).frame_every, float(value))

    def test_legacy_direct_resume_can_keep_twenty_seconds(self):
        args = self.dispatch(lecture, ['--resume', '--frame-every', '20'])
        self.assertTrue(args.resume)
        self.assertEqual(args.frame_every, 20)
