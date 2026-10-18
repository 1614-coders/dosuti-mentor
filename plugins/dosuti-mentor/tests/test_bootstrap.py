import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import bootstrap


class Bootstrap(unittest.TestCase):
    def test_prepare_keeps_setup_inside_managed_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = (Path(temporary) / 'data').resolve()
            calls = []
            def run(command, **kwargs):
                calls.append((command, kwargs))
                python = home / 'runtime/venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
                python.parent.mkdir(parents=True)
                python.write_text('fixture')
            result = bootstrap.prepare(home, runner=run)
            self.assertEqual(result['home'], str(home))
            self.assertTrue(Path(result['python']).is_relative_to(home))
            self.assertEqual(calls[0][1]['env']['DOSUTI_HOME'], str(home))
            self.assertIn('setup_local.py', str(calls[0][0][2]))

    def test_default_home_is_platform_specific(self):
        with patch.object(bootstrap.sys, 'platform', 'darwin'), patch.object(bootstrap.Path, 'home', return_value=Path('/Users/test')):
            self.assertEqual(bootstrap.default_home(), Path('/Users/test/Library/Application Support/DoSutiMentor'))
        with patch.object(bootstrap.sys, 'platform', 'linux'), patch.dict(os.environ, {'XDG_DATA_HOME': '/data'}, clear=False):
            self.assertEqual(bootstrap.default_home(), Path('/data/DoSutiMentor'))

    def test_platform_check_accepts_target_platforms(self):
        for system, machine in (('win32', 'amd64'), ('darwin', 'arm64'), ('darwin', 'x86_64'), ('linux', 'aarch64')):
            with self.subTest(system=system, machine=machine), \
                    patch.object(bootstrap.sys, 'platform', system), \
                    patch('bootstrap.platform.machine', return_value=machine):
                self.assertEqual(bootstrap.compatibility()['machine'], machine)

    def test_platform_check_rejects_unsupported_architecture(self):
        with patch.object(bootstrap.sys, 'platform', 'win32'), patch('bootstrap.platform.machine', return_value='arm64'):
            with self.assertRaisesRegex(ValueError, 'x64 only'):
                bootstrap.compatibility()
