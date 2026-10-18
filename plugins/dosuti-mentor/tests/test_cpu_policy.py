import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from cpu_policy import configure_environment, policy


class CPUPolicyTests(unittest.TestCase):
    def test_standard_preserves_existing_limits_and_environment(self):
        with patch.dict(os.environ, {'OMP_NUM_THREADS': '9'}, clear=True):
            before = dict(os.environ)
            self.assertEqual(configure_environment('standard'),
                             dict(mode='standard', asr_threads=4, ffmpeg_threads=None))
            self.assertEqual(dict(os.environ), before)

    def test_economy_overrides_inherited_parallelism(self):
        with patch.dict(os.environ, {'OPENBLAS_NUM_THREADS': '64'}, clear=True):
            self.assertEqual(configure_environment('economy')['ffmpeg_threads'], 1)
            self.assertEqual(os.environ['OPENBLAS_NUM_THREADS'], '2')
            self.assertEqual(os.environ['OMP_NUM_THREADS'], '2')

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            policy('fast')

    def test_cli_rejects_bad_mode_before_loading_runtime(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'lecture.py'),
                                 'missing.mp4', '--output', 'unused', '--cpu-mode', 'fast'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid choice', result.stderr)

    def test_worker_configures_threads_before_importing_numpy(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / 'video.mp4'
            video.write_bytes(b'CLI dispatch only')
            code = '''import os, sys, types
sys.path.insert(0, sys.argv[1])
import lecture
assert 'numpy' not in sys.modules
def check(args, ffmpeg):
    assert 'numpy' not in sys.modules
    assert os.environ['OPENBLAS_NUM_THREADS'] == '2'
    assert args.cpu_mode == 'economy'
sys.modules['processing_job'] = types.SimpleNamespace(run_job=check)
sys.argv = ['lecture.py', sys.argv[2], '--output', sys.argv[3], '--cpu-mode', 'economy']
lecture.main()
'''
            result = subprocess.run([sys.executable, '-c', code, str(SCRIPTS), str(video), temp],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
