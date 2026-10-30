import os
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import bootstrap


class Bootstrap(unittest.TestCase):
    def test_dependency_probe_rejects_missing_pdf_and_incompatible_versions(self):
        versions = {'faster-whisper': '1.2.1', 'imageio-ffmpeg': '0.6.0',
                    'Pillow': '11.3.0', 'certifi': '2026.1.1', 'yt-dlp': '2026.1',
                    'av': '16.0.0', 'pdfplumber': '0.11.9', 'packaging': '26.3'}
        # Execute the actual probe in a child; only dependency boundaries are fixtures.
        for missing, overrides in [(None, {}),
                                   *[(name, {}) for name in ('pdfplumber', 'pypdfium2', 'av', 'certifi',
                                                             'faster_whisper', 'PIL', 'yt_dlp',
                                                             'imageio_ffmpeg', 'packaging')],
                                   *[(None, {name: '0.0.1'}) for name in versions if name != 'certifi'],
                                   (None, {'pdfplumber': '0.12.0'}),
                                   (None, {'pdfplumber': '0.11.9rc1'})]:
            with self.subTest(missing=missing, versions=overrides):
                preamble = '''import sys, types, importlib.abc, importlib.metadata, subprocess
subprocess.run = lambda *args, **kwargs: None
class Missing(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == MISSING:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Missing())
for name in ('av','certifi','faster_whisper','PIL','yt_dlp','imageio_ffmpeg','pdfplumber','pypdfium2'):
    if name != MISSING: sys.modules[name] = types.ModuleType(name)
sys.modules['imageio_ffmpeg'].get_ffmpeg_exe = lambda: sys.executable
importlib.metadata.version = lambda name: VERSIONS[name]
'''.replace('MISSING', repr(missing)).replace('VERSIONS', repr(versions | overrides))
                def run(command, **kwargs):
                    command = list(command)
                    command[-1] = preamble + command[-1]
                    return subprocess.run(command, **kwargs)
                if missing or overrides:
                    with self.assertRaises(subprocess.CalledProcessError):
                        bootstrap.verify_python(Path(sys.executable), runner=run)
                else:
                    bootstrap.verify_python(Path(sys.executable), runner=run)

    def test_outdated_runtime_is_upgraded_then_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = bootstrap.initialize((Path(temporary) / 'data').resolve())
            python = bootstrap.runtime_python(home)
            python.parent.mkdir(parents=True); python.write_bytes(b'old')
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                if 'setup_local.py' in str(command):
                    self.assertIn('--if-needed', command)
                    python.write_bytes(b'updated')
                elif python.read_bytes() == b'old':
                    raise subprocess.CalledProcessError(1, command, stderr=b'missing pdfplumber')
            result = bootstrap.prepare(home, runner=run)
            self.assertTrue(result['verified'])
            self.assertEqual(python.read_bytes(), b'updated')
            self.assertEqual(len(calls), 3)
            bootstrap.prepare(home, runner=run)
            self.assertEqual(len(calls), 4)  # Next launch verifies; it does not reinstall.

    def test_failed_upgrade_preserves_runtime_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = bootstrap.initialize((Path(temporary) / 'data').resolve())
            python = bootstrap.runtime_python(home)
            python.parent.mkdir(parents=True); python.write_bytes(b'old')
            def run(command, **kwargs):
                raise subprocess.CalledProcessError(1, command, stderr=b'fixture failure')
            with self.assertRaisesRegex(ValueError, 'setup failed'):
                bootstrap.prepare(home, runner=run)
            self.assertEqual(python.read_bytes(), b'old')

    def test_check_and_verify_do_not_initialize_or_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = (Path(temporary) / 'data').resolve()
            for mode, expected in (('--check', 0), ('--verify', 2)):
                with self.subTest(mode=mode):
                    result = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'bootstrap.py'),
                                             '--home', str(home), mode], capture_output=True, text=True)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertFalse(home.exists())
                    if expected:
                        self.assertNotIn('Traceback', result.stderr)
                        self.assertNotIn('"verified": true', result.stdout)

    def test_verification_timeout_does_not_trigger_reinstall(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = bootstrap.initialize((Path(temporary) / 'data').resolve())
            python = bootstrap.runtime_python(home)
            python.parent.mkdir(parents=True); python.write_bytes(b'old')
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                raise subprocess.TimeoutExpired(command, 60)
            with self.assertRaises(subprocess.TimeoutExpired):
                bootstrap.prepare(home, runner=run)
            self.assertEqual(len(calls), 1)
            self.assertEqual(python.read_bytes(), b'old')

    def test_successful_setup_exit_without_valid_runtime_is_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = bootstrap.initialize((Path(temporary) / 'data').resolve())
            python = bootstrap.runtime_python(home)
            python.parent.mkdir(parents=True); python.write_bytes(b'old')
            def run(command, **kwargs):
                if '-c' in command:
                    raise subprocess.CalledProcessError(1, command)
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.prepare(home, runner=run)
            self.assertEqual(python.read_bytes(), b'old')

    def test_prepare_keeps_setup_inside_managed_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = (Path(temporary) / 'data').resolve()
            calls = []
            def run(command, **kwargs):
                calls.append((command, kwargs))
                python = home / 'runtime/venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_text('fixture')
            result = bootstrap.prepare(home, runner=run)
            self.assertEqual(result['home'], str(home))
            self.assertTrue(Path(result['python']).is_relative_to(home))
            self.assertEqual(calls[0][1]['env']['DOSUTI_HOME'], str(home))
            self.assertIn('setup_local.py', str(calls[0][0][2]))

    def test_existing_verified_runtime_is_reused_without_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = bootstrap.initialize((Path(temporary) / 'data').resolve())
            python = home / 'runtime/venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            python.parent.mkdir(parents=True); python.write_text('fixture')
            calls = []
            result = bootstrap.prepare(home, runner=lambda command, **kw: calls.append(command))
            self.assertTrue(result['verified'])
            self.assertEqual(len(calls), 1)
            self.assertNotIn('setup_local.py', str(calls))

    def test_failed_verification_cannot_return_success_or_modify_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = bootstrap.initialize((Path(temporary) / 'data').resolve())
            python = home / 'runtime/venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            python.parent.mkdir(parents=True); python.write_text('fixture')
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.verify(home, runner=lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, a[0])))
            self.assertEqual(python.read_text(), 'fixture')

    def test_setup_permission_refusal_does_not_return_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = (Path(temporary) / 'data').resolve()
            with self.assertRaises(PermissionError):
                bootstrap.prepare(home, runner=lambda *a, **kw: (_ for _ in ()).throw(PermissionError('denied')))
            self.assertFalse((home / 'courses').exists())

    def test_default_home_is_platform_specific(self):
        with patch.object(bootstrap.sys, 'platform', 'win32'), patch.dict(os.environ, {'LOCALAPPDATA': 'C:/Users/Test/AppData/Local'}):
            self.assertEqual(bootstrap.default_home(), Path('C:/Users/Test/AppData/Local/DoSutiMentor'))
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
