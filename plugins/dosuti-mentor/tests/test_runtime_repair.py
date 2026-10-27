from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import setup_local


class RuntimeRepair(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / 'venv'

    def create(self, path):
        path.mkdir(parents=True, exist_ok=True)
        (path / 'pyvenv.cfg').write_text('home = new\nversion = 3.12.15\n')
        python = path / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        python.parent.mkdir(); python.write_bytes(b'new interpreter fixture')

    def old(self):
        self.target.mkdir()
        (self.target / 'pyvenv.cfg').write_text('home = missing-python\nversion = 3.11.9\n')
        (self.target / 'old-extension.pyd').write_bytes(b'old ABI')

    def test_replaces_environment_for_missing_or_different_base(self):
        self.old()
        with patch.object(setup_local.venv, 'EnvBuilder') as builder, patch.object(setup_local.subprocess, 'run') as pip:
            builder.return_value.create.side_effect = self.create
            setup_local.install(self.root)
        self.assertFalse((self.target / 'old-extension.pyd').exists())
        backups = list(self.root.glob('venv-previous-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'old-extension.pyd').read_bytes(), b'old ABI')
        install = next(call for call in pip.call_args_list if 'pip' in call.args[0])
        self.assertIn('--only-binary=:all:', install.args[0])
        self.assertIn('--isolated', install.args[0])
        self.assertEqual(install.kwargs['env']['PIP_CONFIG_FILE'], os.devnull)

    def test_failed_repair_restores_previous_environment(self):
        self.old()
        before = (self.target / 'pyvenv.cfg').read_bytes()
        with patch.object(setup_local.venv, 'EnvBuilder') as builder, \
                patch.object(setup_local.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
            builder.return_value.create.side_effect = self.create
            with self.assertRaises(subprocess.CalledProcessError): setup_local.install(self.root)
        self.assertEqual((self.target / 'pyvenv.cfg').read_bytes(), before)
        self.assertEqual((self.target / 'old-extension.pyd').read_bytes(), b'old ABI')

    def test_same_python_failure_keeps_previous_environment(self):
        self.target.mkdir()
        base = Path(getattr(sys, '_base_executable', sys.executable)).resolve().parent
        (self.target / 'pyvenv.cfg').write_text(f'home = {base}\nversion = {sys.version_info.major}.{sys.version_info.minor}.0\n')
        (self.target / 'old-extension.pyd').write_bytes(b'old ABI')
        with patch.object(setup_local.venv, 'EnvBuilder') as builder, \
                patch.object(setup_local.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
            builder.return_value.create.side_effect = self.create
            with self.assertRaises(subprocess.CalledProcessError): setup_local.install(self.root)
        self.assertEqual((self.target / 'old-extension.pyd').read_bytes(), b'old ABI')
        self.assertFalse(list(self.root.glob('venv-staging-*')))

    def test_unknown_directory_is_preserved(self):
        self.target.mkdir(); (self.target / 'user-file').write_text('keep')
        with self.assertRaises(ValueError): setup_local.install(self.root)
        self.assertEqual((self.target / 'user-file').read_text(), 'keep')

    def test_staged_verification_failure_never_moves_or_executes_previous(self):
        self.old()
        calls=[]
        def run(command, **kwargs):
            calls.append(command)
            if '-c' in command:
                self.assertEqual((self.target/'old-extension.pyd').read_bytes(), b'old ABI')
                raise subprocess.CalledProcessError(1, command)
        with patch.object(setup_local.venv, 'EnvBuilder') as builder, patch.object(setup_local.subprocess, 'run', side_effect=run):
            builder.return_value.create.side_effect=self.create
            with self.assertRaises(subprocess.CalledProcessError): setup_local.install(self.root)
        self.assertTrue(any('-c' in c for c in calls))
        self.assertTrue(all(Path(c[0]).is_relative_to(self.root) and 'venv-staging-' in c[0] for c in calls))
        self.assertEqual((self.target/'old-extension.pyd').read_bytes(), b'old ABI')

    def test_final_verification_failure_or_interrupt_rolls_back_and_retry_preserves_backups(self):
        self.old()
        for error in (subprocess.CalledProcessError(1,'verify'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                def run(command, **kwargs):
                    if '-c' in command and Path(command[0]).is_relative_to(self.target):raise error
                with patch.object(setup_local.venv, 'EnvBuilder') as builder, patch.object(setup_local.subprocess, 'run', side_effect=run):
                    builder.return_value.create.side_effect=self.create
                    with self.assertRaises(type(error)): setup_local.install(self.root)
                self.assertEqual((self.target/'old-extension.pyd').read_bytes(), b'old ABI')
        with patch.object(setup_local.venv, 'EnvBuilder') as builder, patch.object(setup_local.subprocess, 'run'):
            builder.return_value.create.side_effect=self.create
            setup_local.install(self.root)
        backups=list(self.root.glob('venv-previous-*'))
        self.assertTrue(any((p/'old-extension.pyd').exists() for p in backups))

    def test_interruption_between_renames_restores_old_environment(self):
        self.old(); rename=Path.rename
        def fail(path, destination):
            if path.name.startswith('venv-staging-'):raise KeyboardInterrupt()
            return rename(path,destination)
        with patch.object(setup_local.venv, 'EnvBuilder') as builder, patch.object(setup_local.subprocess, 'run'), patch.object(Path,'rename',fail):
            builder.return_value.create.side_effect=self.create
            with self.assertRaises(KeyboardInterrupt):setup_local.install(self.root)
        self.assertEqual((self.target/'old-extension.pyd').read_bytes(), b'old ABI')

    def test_restore_failure_keeps_previous_tree_for_manual_recovery(self):
        self.old(); rename=Path.rename
        def fail(path, destination):
            if path.name.startswith('venv-staging-') or path.name.startswith('venv-previous-'):
                raise PermissionError('fixture rename failure')
            return rename(path,destination)
        with patch.object(setup_local.venv, 'EnvBuilder') as builder, patch.object(setup_local.subprocess, 'run'), patch.object(Path,'rename',fail):
            builder.return_value.create.side_effect=self.create
            with self.assertRaises(PermissionError):setup_local.install(self.root)
        self.assertEqual(len(list(self.root.glob('venv-previous-*'))),1)
        self.assertEqual(next(self.root.glob('venv-previous-*')).joinpath('old-extension.pyd').read_bytes(),b'old ABI')

    def test_interrupted_staged_check_preserves_previous(self):
        self.old()
        def run(command, **kwargs):
            if '-c' in command:raise KeyboardInterrupt()
        with patch.object(setup_local.venv,'EnvBuilder') as builder, patch.object(setup_local.subprocess,'run',side_effect=run):
            builder.return_value.create.side_effect=self.create
            with self.assertRaises(KeyboardInterrupt):setup_local.install(self.root)
        self.assertEqual((self.target/'old-extension.pyd').read_bytes(), b'old ABI')
        self.assertFalse(list(self.root.glob('venv-staging-*')))

    def test_failed_first_install_is_not_left_as_ready_runtime(self):
        def run(command, **kwargs):
            if '-c' in command and Path(command[0]).is_relative_to(self.target):raise RuntimeError('final check failed')
        with patch.object(setup_local.venv,'EnvBuilder') as builder, patch.object(setup_local.subprocess,'run',side_effect=run):
            builder.return_value.create.side_effect=self.create
            with self.assertRaises(RuntimeError):setup_local.install(self.root)
        self.assertFalse(self.target.exists())
