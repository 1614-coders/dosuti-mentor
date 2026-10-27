"""Exercise the actual embedded probe without creating a real venv or downloading."""
import contextlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv
import ensurepip
import ssl


class PythonProbe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'данные с пробелами'; self.root.mkdir()
        script = Path(__file__).resolve().parents[1] / 'scripts/Windows-Runtime.ps1'
        self.code = script.read_text().split("$code = @'\n",1)[1].split("\n'@",1)[0]

    def run_probe(self, create=None, run=None, version=(3,12,0)):
        with patch.object(sys,'platform','win32'), patch.object(sys,'version_info',version), \
                patch.object(sys,'argv',['probe',str(self.root)]), patch.object(platform,'machine',return_value='AMD64'), \
                patch.object(venv,'EnvBuilder') as builder, patch.object(subprocess,'run') as runner:
            builder.return_value.create.side_effect=create
            runner.side_effect=run
            output=io.StringIO()
            with contextlib.redirect_stdout(output):exec(compile(self.code,'probe','exec'),{})
            return json.loads(output.getvalue()), builder, runner

    def test_venv_and_pip_use_parent_owned_directory(self):
        def create(folder):
            self.assertEqual(Path(folder),self.root/'venv')
            Path(folder).mkdir()
        value,_,runner=self.run_probe(create=create)
        self.assertIn('executable',value)
        self.assertEqual(Path(runner.call_args.args[0][0]),self.root/'venv/Scripts/python.exe')
        self.assertTrue(self.root.exists())  # Only the parent owns removal.

    def test_failure_leaves_only_owned_payload_for_parent_cleanup(self):
        def create(folder):
            self.assertEqual(Path(folder),self.root/'venv')
            Path(folder).mkdir(); (Path(folder)/'partial').write_text('fixture')
            raise OSError('venv failed')
        with self.assertRaisesRegex(OSError,'venv failed'):self.run_probe(create=create)
        self.assertEqual((self.root/'venv/partial').read_text(),'fixture')

    def test_pip_failure_does_not_remove_parent_or_neighbor(self):
        neighbor=self.root.parent/'keep';neighbor.write_text('user data')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_probe(run=lambda *a,**kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1,'pip')))
        self.assertEqual(neighbor.read_text(),'user data')
        self.assertTrue(self.root.exists())

    def test_unsupported_python_does_not_create_environment(self):
        def forbidden(folder):raise RuntimeError('Unsupported Python reached venv creation')
        with self.assertRaises(AssertionError):self.run_probe(create=forbidden,version=(3,10,0))
        self.assertEqual(list(self.root.iterdir()),[])
