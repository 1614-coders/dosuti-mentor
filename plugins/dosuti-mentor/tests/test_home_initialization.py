"""Concurrent initializers must agree on one complete, immutable home marker."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import managed_storage as storage
import safe_material_import as secure

WORKER = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import managed_storage as storage
home = Path(sys.argv[2])
mode = sys.argv[3]
original = Path.mkdir
waiting = True
def wait():
    print('ready', flush=True)
    if sys.stdin.readline().strip() != 'go': raise RuntimeError('Barrier cancelled')
def gate(path, *args, **kwargs):
    global waiting
    result = original(path, *args, **kwargs)
    if path == home and waiting and mode == 'mkdir':
        waiting = False
        wait()
    return result
Path.mkdir = gate
original_fsync = storage.os.fsync
def synced(fd):
    original_fsync(fd)
    if mode == 'staged': wait()
storage.os.fsync = synced
storage.initialize(home)
print(json.loads((home / storage.MARKER).read_text())['id'], flush=True)
'''


class HomeInitialization(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'ученик с пробелами'

    def child(self, home=None, mode='mkdir'):
        process = subprocess.Popen([sys.executable, '-B', '-c', WORKER,
            str(SCRIPTS), str(home or self.home), mode], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill(); process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr): stream.close()
        self.addCleanup(cleanup)
        return process

    def finish(self, process):
        output, error = process.communicate('go\n', timeout=15)
        self.assertEqual(process.returncode, 0, error)
        return output.strip()

    def test_independent_processes_keep_first_published_id(self):
        children = [self.child() for _ in range(4)]
        for process in children:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
        ids = [self.finish(process) for process in children]
        self.assertEqual(len(set(ids)), 1, ids)
        self.assertEqual(json.loads((self.home / storage.MARKER).read_text())['id'], ids[0])

    def test_existing_marker_and_progress_are_unchanged(self):
        storage.initialize(self.home)
        (self.home / 'progress.json').write_bytes(b'{"answer":42}')
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        storage.initialize(self.home)
        self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)

    def test_simultaneous_publication_in_six_processes(self):
        children = [self.child() for _ in range(6)]
        for process in children:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
        for process in children:
            process.stdin.write('go\n'); process.stdin.flush()
        ids = []
        for process in children:
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, error)
            ids.append(output.strip())
        self.assertEqual(len(set(ids)), 1, ids)
        storage.validate(self.home)

    def test_concurrent_staging_does_not_make_home_look_unmanaged(self):
        process = self.child(mode='staged')
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        storage.initialize(self.home)
        winner = (self.home / storage.MARKER).read_bytes()
        self.assertEqual(self.finish(process), json.loads(winner)['id'])
        self.assertEqual((self.home / storage.MARKER).read_bytes(), winner)

    def test_interrupted_staging_is_not_adopted_or_deleted_by_retry(self):
        process = self.child(mode='staged')
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        staged = {p: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.assertEqual(len(staged), 1)
        process.kill(); process.wait(timeout=10)
        storage.initialize(self.home)
        self.assertEqual({p: p.read_bytes() for p in staged}, staged)
        self.assertNotEqual(json.loads(next(iter(staged.values())))['id'],
                            json.loads((self.home / storage.MARKER).read_bytes())['id'])

    def test_fsync_failure_leaves_no_partial_marker(self):
        with patch.object(storage.os, 'fsync', side_effect=OSError('Disk error')):
            with self.assertRaises(OSError): storage.initialize(self.home)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertTrue(all(p == self.home or p.name.startswith('.dosuti-home-')
                            for p in self.root.iterdir()))
        storage.initialize(self.home)

    def test_marker_appearing_at_publication_is_validated_not_overwritten(self):
        for raw in (b'{"schema":"dosuti_home_v1","id":"' + b'a' * 32 + b'"}', b'{'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory(dir=self.root) as folder:
                home = Path(folder) / 'new'; original = secure.move_exclusive
                def publish(source_fd, source, destination_fd, destination):
                    (home / destination).write_bytes(raw)
                    return original(source_fd, source, destination_fd, destination)
                with patch.object(secure, 'move_exclusive', publish):
                    if raw == b'{':
                        with self.assertRaises(ValueError): storage.initialize(home)
                    else: storage.initialize(home)
                self.assertEqual((home / storage.MARKER).read_bytes(), raw)

    def test_marker_directory_is_not_replaced(self):
        (self.home / storage.MARKER).mkdir(parents=True)
        with self.assertRaises(OSError): storage.initialize(self.home)
        self.assertTrue((self.home / storage.MARKER).is_dir())

    def test_legacy_runtime_can_be_adopted_without_rewriting_its_files(self):
        (self.home / 'venv').mkdir(parents=True)
        config = self.home / 'venv/pyvenv.cfg'; config.write_bytes(b'legacy')
        storage.initialize(self.home, legacy_runtime=True)
        self.assertEqual(config.read_bytes(), b'legacy')

    def test_invalid_and_partial_markers_are_never_replaced(self):
        self.home.mkdir()
        marker = self.home / storage.MARKER
        for raw in (b'', b'{', b'[]', b'{"schema":"dosuti_home_v1","id":"bad"}', b'x' * 4097):
            with self.subTest(raw=raw[:40]):
                marker.write_bytes(raw)
                with self.assertRaises((ValueError, OSError)): storage.initialize(self.home)
                self.assertEqual(marker.read_bytes(), raw)

    def test_link_publication_failure_is_explicit_and_retryable(self):
        with patch.object(secure, 'move_exclusive', side_effect=OSError(errno.EPERM, 'No exclusive rename')):
            with self.assertRaises(OSError): storage.initialize(self.home)
        self.assertFalse((self.home / storage.MARKER).exists())
        self.assertTrue(all(p == self.home or p.name.startswith('.dosuti-home-')
                            for p in self.root.iterdir()))
        storage.initialize(self.home)
        storage.validate(self.home)

    def test_unknown_folder_is_not_adopted(self):
        self.home.mkdir(); (self.home / 'user.txt').write_bytes(b'keep')
        with self.assertRaises(ValueError): storage.initialize(self.home)
        self.assertEqual((self.home / 'user.txt').read_bytes(), b'keep')

    def test_symlink_marker_is_refused_without_touching_target(self):
        self.home.mkdir(); target = self.root / 'original'
        target.write_bytes(b'original')
        try: (self.home / storage.MARKER).symlink_to(target)
        except OSError:
            if os.name == 'nt': self.skipTest('Windows symlink privilege is unavailable')
            raise
        with self.assertRaises(ValueError): storage.initialize(self.home)
        self.assertEqual(target.read_bytes(), b'original')

    def test_dangling_marker_and_symlink_home_are_refused(self):
        self.home.mkdir(); missing = self.root / 'missing'
        marker = self.home / storage.MARKER
        try: marker.symlink_to(missing)
        except OSError:
            if os.name == 'nt': self.skipTest('Windows symlink privilege is unavailable')
            raise
        with self.assertRaises(ValueError): storage.initialize(self.home)
        self.assertTrue(marker.is_symlink()); self.assertFalse(missing.exists())
        alias = self.root / 'alias'; alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ValueError): storage.initialize(alias)

    def test_symlink_inserted_at_publication_is_not_followed(self):
        self.home.mkdir(); target = self.root / 'original'
        target.write_bytes(b'keep'); original = secure.move_exclusive
        def publish(source_fd, source, destination_fd, destination):
            try: (self.home / destination).symlink_to(target)
            except OSError:
                if os.name == 'nt': self.skipTest('Windows symlink privilege is unavailable')
                raise
            return original(source_fd, source, destination_fd, destination)
        with patch.object(secure, 'move_exclusive', publish):
            with self.assertRaises(ValueError): storage.initialize(self.home)
        self.assertEqual(target.read_bytes(), b'keep')

    def test_home_swap_during_publication_does_not_mark_foreign_directory(self):
        self.check_swap(parent=False)

    def test_parent_swap_during_publication_does_not_mark_foreign_directory(self):
        self.check_swap(parent=True)

    def check_swap(self, parent):
        container = self.root / 'container'
        container.mkdir()
        home = container / 'home'
        foreign = self.root / 'foreign'
        victim = foreign / 'home' if parent else foreign
        (victim / '.git').mkdir(parents=True)
        (victim / '.git/keep').write_bytes(b'foreign git')
        (victim / 'progress.json').write_bytes(b'foreign progress')
        original = secure.move_exclusive
        def swap(src_fd, src, dst_fd, dst):
            old = container if parent else home
            old.rename(old.with_name(old.name + '-owned'))
            foreign.rename(old)
            return original(src_fd, src, dst_fd, dst)
        with patch.object(secure, 'move_exclusive', swap):
            with self.assertRaises((ValueError, OSError)):
                storage.initialize(home)
        self.assertFalse((home / storage.MARKER).exists())
        self.assertEqual((home / '.git/keep').read_bytes(), b'foreign git')
        self.assertEqual((home / 'progress.json').read_bytes(), b'foreign progress')
        with self.assertRaises((ValueError, OSError)):
            storage.validate(home)

    def test_failed_initializer_does_not_block_another_home(self):
        process = self.child()
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        other = self.root / 'other'
        storage.initialize(other); storage.validate(other)
        process.kill(); process.wait(timeout=10)
        storage.initialize(self.home); storage.validate(self.home)


class UnsupportedHomePublication(unittest.TestCase):
    def test_no_unsafe_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / 'home'
            with patch('safe_zip_import.supported', return_value=False):
                with self.assertRaisesRegex(ValueError, 'no unsafe fallback'):
                    storage.initialize(home)
            self.assertFalse((home / storage.MARKER).exists())


if __name__ == '__main__': unittest.main()
