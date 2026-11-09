"""ZIP destination races use a real second process and no timing sleeps."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile
import test_persistence
import safe_zip_import as secure
import course


class ZipFixture(unittest.TestCase):
    def setUp(self):
        self.f = test_persistence.PersistenceRegression()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.lesson = self.f.path / 'lessons/lesson-01'
        self.archive = self.f.directory / 'code.zip'
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr('victim.py', 'imported code')
        self.foreign = self.f.directory / 'foreign'
        self.foreign.mkdir()
        (self.foreign / 'victim.py').write_text('foreign code')
        (self.foreign / '.git').mkdir()
        (self.foreign / '.git/keep').write_text('foreign Git data')
        self.swaps = 0

    def attach(self):
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(self.f.args(kind='code', source=str(self.archive), none=False,
                                     ref=None, resource_type='file'))

    def snapshot(self, folder):
        return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob('*') if p.is_file()}

    def swap_root(self, archive, member, mode='r', *args, **kwargs):
        if mode == 'r' and not self.swaps:
            self.swaps += 1
            target = next((self.lesson / 'code').iterdir())
            child = ('from pathlib import Path; import sys; p=Path(sys.argv[1]); '
                     'p.rename(p.with_name(p.name+"-owned")); '
                     'p.symlink_to(Path(sys.argv[2]), target_is_directory=True)')
            subprocess.run([sys.executable, '-B', '-c', child, str(target), str(self.foreign)],
                           check=True, capture_output=True, timeout=10)
        return self.original_open(archive, member, mode, *args, **kwargs)

    def attack(self):
        self.original_open = zipfile.ZipFile.open
        before = (self.lesson / 'lesson.json').read_bytes()
        def intercepted(archive, *args, **kwargs):
            return self.swap_root(archive, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', intercepted), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError, OSError)):
                self.attach()
        self.assertEqual(self.swaps, 1, 'The controlled interprocess race must actually run')
        self.assertEqual((self.lesson / 'lesson.json').read_bytes(), before)

@unittest.skipUnless(os.name == 'posix', 'Descriptor-relative ZIP import is unavailable; Windows refusal is tested separately')
class ZipImportSafety(ZipFixture):
    def test_root_swap_never_overwrites_foreign_file(self):
        self.attack()
        self.assertEqual((self.foreign / 'victim.py').read_text(), 'foreign code')

    def test_root_swap_never_deletes_foreign_git(self):
        self.attack()
        self.assertTrue((self.foreign / '.git/keep').is_file())
        self.assertEqual((self.foreign / '.git/keep').read_text(), 'foreign Git data')

    def parent_attack(self, level, link=True):
        before = (self.lesson / 'lesson.json').read_bytes()
        original = zipfile.ZipFile.open
        ran = []
        def intercepted(archive, member, mode='r', *args, **kwargs):
            if mode == 'r' and not ran:
                ran.append(True)
                target = next((self.lesson / 'code').iterdir())
                replaced = target if level == 'root' else self.lesson / 'code' if level == 'code' else self.lesson
                replacement = self.f.directory / 'replacement'
                relative = target.relative_to(replaced)
                foreign_root = replacement / relative
                foreign_root.parent.mkdir(parents=True, exist_ok=True)
                self.foreign.rename(foreign_root)
                if level == 'lesson': (replacement / 'lesson.json').write_bytes(before)
                child = ('from pathlib import Path; import sys; p=Path(sys.argv[1]); '
                         'p.rename(p.with_name(p.name+"-owned")); '
                         'q=Path(sys.argv[2]); '
                         'p.symlink_to(q,target_is_directory=True) if sys.argv[3]=="link" else q.rename(p)')
                subprocess.run([sys.executable, '-B', '-c', child, str(replaced), str(replacement),
                                'link' if link else 'rename'], check=True, capture_output=True, timeout=10)
                self.foreign = replaced / relative
            return original(archive, member, mode, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', intercepted), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError, OSError)): self.attach()
        self.assertEqual(len(ran), 1)
        self.assertEqual((self.foreign / 'victim.py').read_text(), 'foreign code')
        self.assertEqual((self.foreign / '.git/keep').read_text(), 'foreign Git data')
        self.assertEqual((self.lesson / 'lesson.json').read_bytes(), before)

    def test_code_parent_replaced_by_another_process(self):
        self.parent_attack('code')

    def test_lesson_parent_replaced_by_another_process(self):
        self.parent_attack('lesson')

    def test_root_replaced_by_an_ordinary_foreign_directory(self):
        self.parent_attack('root', link=False)

    def test_normal_zip_preserves_bytes_history_and_existing_progress(self):
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr('src/Main.java', b'  class Main {\r\n}\r\n')
            z.writestr('notes/code.py', b'\tif ready:\n\t\twork()\n')
            z.writestr('.git/config', 'must not import Git metadata')
        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(self.f.args(topic='Existing', stage='explained', details='QA', homework_revision=None))
        progress = self.snapshot(self.f.path / 'learners')
        self.attach(); first = self.f.state()['code']
        self.assertEqual(course.verify_snapshot(first), 'snapshot_hashes_match')
        folder = Path(first['snapshot'])
        self.assertEqual((folder / 'src/Main.java').read_bytes(), b'  class Main {\r\n}\r\n')
        self.assertEqual((folder / 'notes/code.py').read_bytes(), b'\tif ready:\n\t\twork()\n')
        self.assertFalse((folder / '.git').exists())
        self.attach(); second = self.f.state()['code']
        self.assertNotEqual(first['snapshot'], second['snapshot'])
        self.assertEqual(second['history'][-1]['snapshot'], first['snapshot'])
        self.assertEqual(self.snapshot(self.f.path / 'learners'), progress)

    def test_foreign_file_injected_before_exclusive_create_is_preserved(self):
        original = zipfile.ZipFile.open
        injected = []
        def intercept(archive, member, mode='r', *args, **kwargs):
            if mode == 'r' and not injected:
                victim = next((self.lesson / 'code').iterdir()) / 'victim.py'
                victim.write_text('not owned by import'); injected.append(victim)
            return original(archive, member, mode, *args, **kwargs)
        before = (self.lesson / 'lesson.json').read_bytes()
        with patch.object(zipfile.ZipFile, 'open', intercept), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError): self.attach()
        self.assertEqual(injected[0].read_text(), 'not owned by import')
        self.assertEqual((self.lesson / 'lesson.json').read_bytes(), before)

    def test_subdirectory_symlink_swap_does_not_redirect_writes(self):
        with zipfile.ZipFile(self.archive, 'w') as z: z.writestr('src/victim.py', 'imported code')
        original = zipfile.ZipFile.open; ran = []
        def intercept(archive, member, mode='r', *args, **kwargs):
            if mode == 'r' and not ran:
                ran.append(True)
                path = next((self.lesson / 'code').iterdir()) / 'src'
                path.rename(path.with_name('src-owned')); path.symlink_to(self.foreign, target_is_directory=True)
            return original(archive, member, mode, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', intercept), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError, OSError)): self.attach()
        self.assertEqual(len(ran), 1)
        self.assertEqual((self.foreign / 'victim.py').read_text(), 'foreign code')
        self.assertTrue((self.foreign / '.git/keep').is_file())

    def test_failed_publication_preserves_residue_and_retry_succeeds(self):
        before = (self.lesson / 'lesson.json').read_bytes(); warning = io.StringIO()
        with patch.object(secure, 'publish', side_effect=OSError('publish failed')), contextlib.redirect_stderr(warning):
            with self.assertRaisesRegex(OSError, 'publish failed'): self.attach()
        self.assertIn('preserved for inspection', warning.getvalue())
        self.assertEqual((self.lesson / 'lesson.json').read_bytes(), before)
        self.assertTrue(list((self.lesson / 'code').iterdir()))
        self.attach()
        self.assertEqual(course.verify_snapshot(self.f.state()['code']), 'snapshot_hashes_match')

    def test_interruption_closes_handles_preserves_files_and_allows_retry(self):
        before = (self.lesson / 'lesson.json').read_bytes()
        with patch.object(zipfile.ZipExtFile, 'read', side_effect=KeyboardInterrupt('cancel')), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt): self.attach()
        self.assertEqual((self.lesson / 'lesson.json').read_bytes(), before)
        self.assertEqual((self.foreign / '.git/keep').read_text(), 'foreign Git data')
        self.attach()
        self.assertEqual(course.verify_snapshot(self.f.state()['code']), 'snapshot_hashes_match')

    def test_unsafe_members_are_refused_before_destination_creation(self):
        before = self.snapshot(self.f.path)
        for name in ('../victim.py', '/absolute', 'C:/victim.py', 'nested\\victim.py'):
            with self.subTest(name=name):
                with zipfile.ZipFile(self.archive, 'w') as z: z.writestr(name, 'bad')
                with self.assertRaises(ValueError): self.attach()
                self.assertEqual(self.snapshot(self.f.path), before)

    def test_symbolic_link_member_is_refused(self):
        info = zipfile.ZipInfo('link'); info.create_system = 3; info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(self.archive, 'w') as z: z.writestr(info, str(self.foreign))
        with self.assertRaises(ValueError): self.attach()
        self.assertEqual((self.foreign / 'victim.py').read_text(), 'foreign code')

    def test_corrupt_zip_is_rejected_without_registration(self):
        self.archive.write_bytes(b'PK invalid archive')
        before = (self.lesson / 'lesson.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'Safe ZIP import failed'): self.attach()
        self.assertEqual((self.lesson / 'lesson.json').read_bytes(), before)

    def test_archive_size_limit_precedes_destination_creation(self):
        import material_sources
        before = self.snapshot(self.f.path)
        with patch.object(material_sources, 'MAX_BYTES', 1):
            with self.assertRaisesRegex(ValueError, '32 MiB'): self.attach()
        self.assertEqual(self.snapshot(self.f.path), before)

    def test_existing_hard_link_is_not_overwritten(self):
        original = zipfile.ZipFile.open; injected = []
        def intercept(archive, member, mode='r', *args, **kwargs):
            if mode == 'r' and not injected:
                path = next((self.lesson / 'code').iterdir()) / 'victim.py'
                os.link(self.foreign / 'victim.py', path); injected.append(path)
            return original(archive, member, mode, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', intercept), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError): self.attach()
        self.assertEqual((self.foreign / 'victim.py').read_text(), 'foreign code')
        self.assertEqual(injected[0].read_text(), 'foreign code')

    def test_two_processes_merge_registrations_without_lost_history(self):
        child = r"""import course,sys,zipfile
from pathlib import Path
from types import SimpleNamespace as NS
original=zipfile.ZipFile.open
ready=False
def gate(self,*args,**kwargs):
 global ready
 if not ready:
  ready=True;print('ready',flush=True)
  if sys.stdin.readline().strip()!='go':raise RuntimeError('not released')
 return original(self,*args,**kwargs)
zipfile.ZipFile.open=gate
course.attach(NS(course=Path(sys.argv[1]),lesson='lesson-01',kind='code',source=sys.argv[2],none=False,ref=None,resource_type='file'))
"""
        env = dict(os.environ, PYTHONPATH=str(Path(course.__file__).parent))
        processes = []
        try:
            for _ in range(2):
                p = subprocess.Popen([sys.executable, '-B', '-c', child, str(self.f.path), str(self.archive)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                processes.append(p); self.assertEqual(p.stdout.readline().strip(), 'ready')
            for p in processes:
                p.stdin.write('go\n'); p.stdin.flush()
            for p in processes:
                _, error = p.communicate(timeout=15)
                self.assertEqual(p.returncode, 0, error)
        finally:
            for p in processes:
                if p.poll() is None: p.kill(); p.wait()
                for stream in (p.stdin, p.stdout, p.stderr): stream.close()
        code = self.f.state()['code']
        self.assertEqual(len(code['history']), 1)
        for item in [code, *code['history']]:
            self.assertEqual(course.verify_snapshot(item), 'snapshot_hashes_match')


class ZipPlatformRefusal(ZipFixture):
    def test_missing_primitives_refuses_without_creating_files(self):
        before = self.snapshot(self.f.path)
        with patch.object(secure, 'supported', return_value=False):
            with self.assertRaisesRegex(ValueError, 'Safe ZIP import is unavailable'):
                self.attach()
        self.assertEqual(self.snapshot(self.f.path), before)

    def test_windows_policy_has_no_fallback(self):
        if os.name != 'nt':
            with patch.object(secure.os, 'name', 'nt'): self.assertFalse(secure.supported())
        else:
            self.assertFalse(secure.supported())
            with self.assertRaisesRegex(ValueError, 'Safe ZIP import is unavailable'): self.attach()


if __name__ == '__main__':
    unittest.main()
