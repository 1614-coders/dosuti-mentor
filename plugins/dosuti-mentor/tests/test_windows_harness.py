"""Portable tests of the Windows test harness, not native installer acceptance."""
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import test_windows_install_race as races


class RaceFixtureCleanup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = object.__new__(races.RaceFixture)
        self.fixture.base = self.root / 'owned'
        self.fixture.base.mkdir()
        (self.fixture.base / 'evidence').write_text('keep after failure')
        self.foreign = self.root / 'foreign.txt'; self.foreign.write_text('foreign')
        self.fixture.processes = []
        self.fixture.handles = [io.BytesIO(), io.BytesIO()]
        self.fixture.preserve = False
        self.env = patch.dict(os.environ, {'SystemRoot': str(self.root / 'Windows')})
        self.env.start(); self.addCleanup(self.env.stop)

    def worker(self, pid):
        worker = Mock(pid=pid)
        worker.poll.return_value = None
        worker.wait.return_value = 0
        return worker

    def test_one_worker_failure_does_not_abandon_other_workers(self):
        first, second = self.worker(111), self.worker(222)
        self.fixture.processes = [first, second]
        error = subprocess.TimeoutExpired(['fixture taskkill'], 10)
        with patch.object(races.subprocess, 'run', side_effect=[error, subprocess.CompletedProcess([], 0)]) as stop:
            with self.assertRaisesRegex(RuntimeError, 'fixture preserved') as caught:
                self.fixture.close()
        self.assertEqual(stop.call_count, 2)
        self.assertEqual([call.args[0][2] for call in stop.call_args_list], ['111', '222'])
        second.wait.assert_called_once_with(timeout=5)
        self.assertIs(caught.exception.__cause__, error)
        self.assertTrue(all(handle.closed for handle in self.fixture.handles))
        self.assertTrue(self.fixture.preserve)
        self.assertEqual((self.fixture.base / 'evidence').read_text(), 'keep after failure')
        self.assertEqual(self.foreign.read_text(), 'foreign')

    def test_wait_failure_still_attempts_every_live_worker(self):
        first, second = self.worker(111), self.worker(222)
        first.wait.side_effect = subprocess.TimeoutExpired(['fixture worker'], 5)
        self.fixture.processes = [first, second]
        with patch.object(races.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as stop:
            with self.assertRaisesRegex(RuntimeError, 'fixture preserved'):
                self.fixture.close()
        self.assertEqual(stop.call_count, 2)
        second.wait.assert_called_once_with(timeout=5)
        self.assertTrue(self.fixture.base.exists())
        self.assertTrue(all(handle.closed for handle in self.fixture.handles))

    def test_handle_failure_closes_other_handles_and_preserves_evidence(self):
        broken = Mock(); error = OSError('fixture close failed'); broken.close.side_effect = error
        other = io.BytesIO(); self.fixture.handles = [broken, other]
        with self.assertRaisesRegex(RuntimeError, 'fixture preserved') as caught:
            self.fixture.close()
        self.assertIs(caught.exception.__cause__, error)
        self.assertTrue(other.closed)
        self.assertTrue(self.fixture.preserve)
        self.assertTrue((self.fixture.base / 'evidence').exists())

    def test_successful_cleanup_removes_only_owned_directory(self):
        running, exited = self.worker(111), self.worker(222)
        exited.poll.return_value = 0
        self.fixture.processes = [running, exited]
        with patch.object(races.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as stop:
            self.fixture.close()
        stop.assert_called_once()
        running.wait.assert_called_once_with(timeout=5)
        exited.wait.assert_not_called()
        self.assertTrue(all(handle.closed for handle in self.fixture.handles))
        self.assertFalse(self.fixture.base.exists())
        self.assertEqual(self.foreign.read_text(), 'foreign')


class RaceFixtureConstruction(unittest.TestCase):
    def test_failed_archive_creation_removes_only_new_fixture(self):
        import tarfile
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            foreign = root / 'foreign.txt'; foreign.write_text('keep')
            owned = root / 'failed-fixture'
            def allocate(**kwargs):
                owned.mkdir()
                return str(owned)
            with patch.object(races.tempfile, 'mkdtemp', allocate), \
                 patch.object(tarfile, 'open', side_effect=OSError('fixture archive failed')):
                with self.assertRaisesRegex(OSError, 'fixture archive failed'):
                    races.RaceFixture()
            self.assertFalse(owned.exists())
            self.assertEqual(foreign.read_text(), 'keep')

    def test_second_log_open_failure_registers_first_handle_before_launch(self):
        fixture = races.RaceFixture()
        real_open = Path.open
        def opened(path, *args, **kwargs):
            if path.suffix == '.stderr':
                raise OSError('fixture stderr open failed')
            return real_open(path, *args, **kwargs)
        try:
            with patch.object(Path, 'open', opened), \
                 patch.object(races.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(OSError, 'fixture stderr open failed'):
                    fixture.run('initialize', 'init')
            launch.assert_not_called()
            self.assertEqual(len(fixture.handles), 1)
            self.assertFalse(fixture.handles[0].closed)
        finally:
            fixture.close()
        self.assertTrue(all(handle.closed for handle in fixture.handles))
        self.assertFalse(fixture.base.exists())


class PowerShellTransport(unittest.TestCase):
    def test_literal_preserves_quotes_unicode_and_paths(self):
        from powershell_test_support import literal
        for value in ('', "O'Brien", 'данные с пробелами', Path('a b/c'), 'line\nbreak'):
            with self.subTest(value=value):
                quoted = literal(value)
                self.assertEqual(quoted[0], "'"); self.assertEqual(quoted[-1], "'")
                self.assertEqual(quoted[1:-1].replace("''", "'"), str(value))

    def test_command_encodes_exact_body_without_shell_interpolation(self):
        import base64
        from powershell_test_support import powershell_command
        code = "$a='Привет'; Write-Output \"$a O'Brien & `quoted`\"\nexit 0"
        executable = 'C:/данные с пробелами/pwsh.exe'
        with patch.object(subprocess, 'run', side_effect=AssertionError('Transport must not execute code')):
            argv = powershell_command(executable, code)
        self.assertEqual(argv[:-1], [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand'])
        self.assertEqual(base64.b64decode(argv[-1]).decode('utf-16-le'), code)
        self.assertTrue(argv[-1].isascii())

    def test_explicit_engine_never_falls_back_or_skips(self):
        import powershell_test_support as support
        with patch.dict(os.environ, {'DOSUTI_TEST_PWSH': '/missing/explicit/powershell.exe'}), \
             patch.object(support.shutil, 'which', side_effect=AssertionError('Explicit engine must win')):
            self.assertEqual(support.powershell_executable(), '/missing/explicit/powershell.exe')

    def test_engine_discovery_prefers_7_then_legacy_then_unavailable(self):
        import powershell_test_support as support
        for values, expected, calls in [(['seven'], 'seven', ['pwsh']),
                                        ([None, 'legacy'], 'legacy', ['pwsh', 'powershell']),
                                        ([None, None], None, ['pwsh', 'powershell'])]:
            with self.subTest(expected=expected), patch.dict(os.environ, {}, clear=True), \
                 patch.object(support.shutil, 'which', side_effect=values) as which:
                self.assertEqual(support.powershell_executable(), expected)
                self.assertEqual([call.args[0] for call in which.call_args_list], calls)


class WindowsSuiteContract(unittest.TestCase):
    def test_both_engines_use_single_profile_and_keep_native_races(self):
        import windows_test_suite as suite
        workflow = (Path(__file__).resolve().parents[3] / '.github/workflows/tests.yml').read_text()
        self.assertEqual(workflow.count('python -B -m unittest -v windows_test_suite'), 2)
        self.assertEqual(workflow.count('python -B test_windows_install_race.py'), 2)
        self.assertIn('shell: powershell', workflow)
        self.assertIn('shell: pwsh', workflow)
        self.assertEqual(len(suite.WINDOWS_MODULES), len(set(suite.WINDOWS_MODULES)))
        for name in ('test_windows_runtime', 'test_process_safety', 'test_unit_event_input',
                     'test_instruction_contracts', 'test_context', 'test_onboarding_hierarchy',
                     'test_lesson_presentation', 'test_windows_harness',
                     'test_html_content', 'test_html_structure', 'test_html_limits'):
            self.assertIn(name, suite.WINDOWS_MODULES)

    def test_profile_uses_standard_loader_and_does_not_hide_import_errors(self):
        import windows_test_suite as suite
        loader = Mock(); expected = unittest.TestSuite(); loader.loadTestsFromNames.return_value = expected
        self.assertIs(suite.load_tests(loader, unittest.TestSuite(), None), expected)
        loader.loadTestsFromNames.assert_called_once_with(suite.WINDOWS_MODULES)
        # A real unittest loader records an unavailable module as an error, not a skip.
        with patch.object(suite, 'WINDOWS_MODULES', ('dosuti_deliberately_missing_qa_module',)):
            broken = suite.load_tests(unittest.TestLoader(), unittest.TestSuite(), None)
        result = unittest.TestResult(); broken.run(result)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.skipped, [])


if __name__ == '__main__':
    unittest.main()
