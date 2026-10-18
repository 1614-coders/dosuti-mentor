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
        self.assertFalse(list(self.root.glob('venv-previous-*')))
        self.assertIn('--only-binary=:all:', pip.call_args.args[0])
        self.assertIn('--isolated', pip.call_args.args[0])
        self.assertEqual(pip.call_args.kwargs['env']['PIP_CONFIG_FILE'], os.devnull)

    def test_failed_repair_restores_previous_environment(self):
        self.old()
        before = (self.target / 'pyvenv.cfg').read_bytes()
        with patch.object(setup_local.venv, 'EnvBuilder') as builder, \
                patch.object(setup_local.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
            builder.return_value.create.side_effect = self.create
            with self.assertRaises(subprocess.CalledProcessError): setup_local.install(self.root)
        self.assertEqual((self.target / 'pyvenv.cfg').read_bytes(), before)
        self.assertEqual((self.target / 'old-extension.pyd').read_bytes(), b'old ABI')

    def test_unknown_directory_is_preserved(self):
        self.target.mkdir(); (self.target / 'user-file').write_text('keep')
        with self.assertRaises(ValueError): setup_local.install(self.root)
        self.assertEqual((self.target / 'user-file').read_text(), 'keep')
