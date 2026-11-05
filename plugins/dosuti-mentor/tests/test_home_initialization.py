"""Concurrent initializers must agree on one complete, immutable home marker."""
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import queue
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import managed_storage as storage
import safe_material_import as secure

def rename_error(error):
    return dict(exception_type=type(error).__name__, exception_module=type(error).__module__,
                errno=error.errno, winerror=getattr(error, 'winerror', None),
                ntstatus=getattr(error, 'status', None), message=str(error))


WORKER = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]).parent / 'tests'))
from test_home_initialization import rename_error
import managed_storage as storage
home = Path(sys.argv[2])
mode = sys.argv[3]
original = Path.mkdir
waiting = True
def wait(message='ready'):
    print(message, flush=True)
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
if sys.platform == 'win32':
    import windows_managed_home as native
    original_init = native.HomeSession.__init__
    def opened(self, path, **kw):
        original_init(self, path, **kw)
        if Path(path) == home and mode == 'mkdir': wait()
    native.HomeSession.__init__ = opened
    original_flush = native.Native.flush
    def flushed(self, handle):
        original_flush(self, handle)
        if mode == 'staged': wait()
    native.Native.flush = flushed
race = dict(attempts=0, commits=[], failures=[])
if mode == 'rename':
    def contend(operation, payload):
        race['attempts'] += 1
        wait('rename-ready')
        try: operation()
        except OSError as error:
            race['failures'].append(rename_error(error))
            raise
        else: race['commits'].append(payload.hex())
    if sys.platform == 'win32':
        original_rename = native.Native.rename
        def renaming(self, stage, parent):
            payload = self.read(stage)
            contend(lambda: original_rename(self, stage, parent), payload)
        native.Native.rename = renaming
    else:
        import safe_material_import as material
        original_rename = material.move_exclusive
        def renaming(source_fd, source, destination_fd, destination):
            if destination != storage.MARKER:
                return original_rename(source_fd, source, destination_fd, destination)
            import os
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
            try: payload = os.read(fd, 4097)
            finally: os.close(fd)
            contend(lambda: original_rename(source_fd, source, destination_fd, destination), payload)
        material.move_exclusive = renaming
storage.initialize(home)
raw = (home / storage.MARKER).read_bytes()
if mode == 'rename':
    print(json.dumps(dict(race, id=json.loads(raw)['id'], marker=raw.hex())), flush=True)
else: print(json.loads(raw)['id'], flush=True)
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

    def barrier_line(self, process, timeout=15):
        # Windows pipes cannot be select()ed. A daemon reader plus a bounded
        # queue wait keeps failure cleanup able to terminate the child.
        lines = queue.Queue()
        def read():
            try: lines.put(process.stdout.readline())
            except (OSError, ValueError) as error: lines.put(error)
        threading.Thread(target=read, daemon=True).start()
        try: result = lines.get(timeout=timeout)
        except queue.Empty: self.fail('Process did not reach the rename barrier before timeout')
        self.assertIsInstance(result, str, 'Barrier pipe failed')
        return result.strip()

    def publication_race(self, count):
        children = [self.child(mode='rename') for _ in range(count)]
        reached = 0
        deadline = time.monotonic() + 30
        for process in children:
            self.assertEqual(self.barrier_line(process, max(0.01, deadline - time.monotonic())),
                             'rename-ready', 'Every contender must reach the actual publication operation')
            reached += 1
        self.assertEqual(reached, count)
        self.assertFalse((self.home / storage.MARKER).exists(), 'No publication before barrier release')
        # All final classifications finished while home was empty. This fixture
        # then checks preservation of independent progress during publication.
        progress = self.home / 'progress.json'
        progress.write_bytes(b'{"answer":"existing learner work"}')
        before = progress.read_bytes()
        for process in children:
            process.stdin.write('go\n'); process.stdin.flush()
        results = []
        for process in children:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error)
            results.append(json.loads(output))
        self.assertEqual([row['attempts'] for row in results], [1] * count,
                         'Every participant must invoke the publication operation exactly once')
        commits = [raw for row in results for raw in row['commits']]
        self.assertEqual(len(commits), 1, 'Exactly one no-replace rename must commit')
        self.assertEqual(sum(len(row['failures']) for row in results), count - 1,
                         'All other prepared publishers must encounter an occupied destination')
        for index, row in enumerate(results):
            with self.subTest(publisher=index):
                if row['commits']:
                    self.assertEqual(len(row['commits']), 1)
                    self.assertEqual(row['failures'], [])
                else:
                    self.assertEqual(len(row['failures']), 1)
                    self.assert_collision(row['failures'][0])
        winner = bytes.fromhex(commits[0])
        winner_id = json.loads(winner)['id']
        self.assertEqual([row['id'] for row in results], [winner_id] * count)
        self.assertEqual([row['marker'] for row in results], [winner.hex()] * count)
        self.assertEqual((self.home / storage.MARKER).read_bytes(), winner)
        self.assertEqual(progress.read_bytes(), before)
        storage.initialize(self.home)
        self.assertEqual((self.home / storage.MARKER).read_bytes(), winner)
        self.assertEqual(progress.read_bytes(), before)

    def assert_collision(self, error, platform_name=None):
        self.assertIsInstance(error, dict, 'Rename failure must include structured error data')
        self.assertEqual(set(error), {'exception_type', 'exception_module', 'errno', 'winerror',
                                     'ntstatus', 'message'})
        self.assertTrue(error['exception_type']); self.assertTrue(error['exception_module'])
        self.assertTrue(error['message'])
        if (platform_name or sys.platform) == 'win32':
            # Native.rename uses SetFileInformationByHandle -> GetLastError ->
            # ctypes.WinError, NOT Native.status/RtlNtStatusToDosError.
            self.assertIn(error['winerror'], (80, 183),
                          'Wrong rename failure reason: expected ERROR_FILE_EXISTS/ERROR_ALREADY_EXISTS')
        else:
            self.assertEqual(error['errno'], errno.EEXIST,
                             'Wrong rename failure reason: expected EEXIST')
            self.assertIsNone(error['winerror'])

    def test_independent_processes_keep_first_published_id(self):
        self.publication_race(4)

    def test_existing_marker_and_progress_are_unchanged(self):
        storage.initialize(self.home)
        (self.home / 'progress.json').write_bytes(b'{"answer":42}')
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        storage.initialize(self.home)
        self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)

    def test_simultaneous_publication_in_six_processes(self):
        self.publication_race(6)

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

    @unittest.skipIf(os.name == 'nt', 'POSIX operation hook; Windows-native equivalents in test_windows_managed_home')
    def test_fsync_failure_leaves_no_partial_marker(self):
        with patch.object(storage.os, 'fsync', side_effect=OSError('Disk error')):
            with self.assertRaises(OSError): storage.initialize(self.home)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertTrue(all(p == self.home or p.name.startswith('.dosuti-home-')
                            for p in self.root.iterdir()))
        storage.initialize(self.home)

    @unittest.skipIf(os.name == 'nt', 'POSIX operation hook; Windows-native equivalents in test_windows_managed_home')
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

    @unittest.skipIf(os.name == 'nt', 'POSIX operation hook; Windows-native equivalents in test_windows_managed_home')
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
        with self.assertRaises((ValueError, OSError)): storage.initialize(self.home)
        self.assertEqual(target.read_bytes(), b'original')

    def test_dangling_marker_and_symlink_home_are_refused(self):
        self.home.mkdir(); missing = self.root / 'missing'
        marker = self.home / storage.MARKER
        try: marker.symlink_to(missing)
        except OSError:
            if os.name == 'nt': self.skipTest('Windows symlink privilege is unavailable')
            raise
        with self.assertRaises((ValueError, OSError)): storage.initialize(self.home)
        self.assertTrue(marker.is_symlink()); self.assertFalse(missing.exists())
        alias = self.root / 'alias'; alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)): storage.initialize(alias)

    @unittest.skipIf(os.name == 'nt', 'POSIX operation hook; Windows-native equivalents in test_windows_managed_home')
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
            with self.assertRaises((ValueError, OSError)): storage.initialize(self.home)
        self.assertEqual(target.read_bytes(), b'keep')

    @unittest.skipIf(os.name == 'nt', 'POSIX operation hook; Windows-native equivalents in test_windows_managed_home')
    def test_home_swap_during_publication_does_not_mark_foreign_directory(self):
        self.check_swap(parent=False)

    @unittest.skipIf(os.name == 'nt', 'POSIX operation hook; Windows-native equivalents in test_windows_managed_home')
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
            with (patch('windows_managed_home.Native', side_effect=ValueError('no unsafe fallback'))
                  if sys.platform == 'win32' else patch('safe_zip_import.supported', return_value=False)):
                with self.assertRaisesRegex(ValueError, 'no unsafe fallback'):
                    storage.initialize(home)
            self.assertFalse((home / storage.MARKER).exists())

    def test_unavailable_backend_preserves_existing_id_and_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            marker = home / storage.MARKER
            marker.write_text(json.dumps(dict(schema='dosuti_home_v1', id='a' * 32)))
            (home / 'progress.json').write_bytes(b'{"answer":"independent work"}')
            before = {p.name: p.read_bytes() for p in home.iterdir()}
            with (patch('windows_managed_home.Native', side_effect=ValueError('no unsafe fallback'))
                  if sys.platform == 'win32' else patch('safe_zip_import.supported', return_value=False)):
                with self.assertRaisesRegex(ValueError, 'no unsafe fallback'):
                    storage.initialize(home)
            self.assertEqual({p.name: p.read_bytes() for p in home.iterdir()}, before)

    def test_unavailable_backend_does_not_repair_corrupt_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            marker = home / storage.MARKER
            marker.write_bytes(b'[]')
            with (patch('windows_managed_home.Native', side_effect=ValueError('no unsafe fallback'))
                  if sys.platform == 'win32' else patch('safe_zip_import.supported', return_value=False)):
                with self.assertRaisesRegex(ValueError, 'no unsafe fallback'):
                    storage.initialize(home)
            self.assertEqual(marker.read_bytes(), b'[]')
            self.assertEqual(list(home.iterdir()), [marker])


class PublicationRaceEvidence(unittest.TestCase):
    def assert_rejected(self, method, ready, metrics):
        case = HomeInitialization(method)
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.home.mkdir()
        def child(**kwargs):
            return SimpleNamespace(stdout=io.StringIO('' if ready is None else ready + '\n'), stdin=io.StringIO(),
                                   returncode=0, communicate=lambda **kw: (json.dumps(metrics), ''))
        result = unittest.TestResult()
        with patch.object(case, 'child', child):
            unittest.FunctionTestCase(getattr(case, method)).run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        return result.failures[0][1]

    def test_concurrent_tests_fail_without_reaching_rename_barrier(self):
        for method in ('test_independent_processes_keep_first_published_id',
                       'test_simultaneous_publication_in_six_processes'):
            with self.subTest(method=method):
                failure = self.assert_rejected(method, 'not-at-rename', {})
                self.assertIn('Every contender must reach', failure)

    def test_concurrent_tests_fail_without_actual_publication_attempt(self):
        metrics = dict(attempts=0, commits=[], failures=[], id='a' * 32, marker='')
        for method in ('test_independent_processes_keep_first_published_id',
                       'test_simultaneous_publication_in_six_processes'):
            with self.subTest(method=method):
                failure = self.assert_rejected(method, 'rename-ready', metrics)
                self.assertIn('Every participant must invoke', failure)

    def unrelated_rename_control(self, method, winerror):
        # A deterministic negative control only. It does NOT simulate a passing
        # Windows race: the actual race assertion must reject this evidence.
        import windows_managed_home as native
        case = HomeInitialization(method)
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.home.mkdir()
        winner = json.dumps(dict(schema='dosuti_home_v1', id='a' * 32)).encode()
        marker = case.home / storage.MARKER
        created = []
        observed = []
        def child(**kwargs):
            index = len(created)
            created.append(index)
            def communicate(**kwargs):
                if index == 0:
                    marker.write_bytes(winner)
                    commits, failures = [winner.hex()], []
                else:
                    self.assertEqual(marker.read_bytes(), winner)
                    try: native.Native.rename(None, 123, 456)
                    except OSError as error:
                        observed.append(error)
                        commits, failures = [], [rename_error(error)]
                    else: raise AssertionError('Negative control did not inject a rename error')
                return json.dumps(dict(attempts=1, commits=commits, failures=failures,
                                       id='a' * 32, marker=winner.hex())), ''
            return SimpleNamespace(stdout=io.StringIO('rename-ready\n'), stdin=io.StringIO(),
                                   returncode=0, communicate=communicate)
        error = OSError(errno.EACCES if winerror == 5 else errno.EIO, 'injected unrelated rename error')
        error.winerror = winerror
        validate = lambda failure: HomeInitialization.assert_collision(case, failure, platform_name='win32')
        result = unittest.TestResult()
        with patch.object(case, 'child', child), patch.object(case, 'assert_collision', validate), \
                patch.object(native.Native, 'rename', side_effect=error) as rename:
            unittest.FunctionTestCase(getattr(case, method)).run(result)
        self.assertTrue(observed, 'Native.rename negative control must actually execute')
        self.assertEqual(rename.call_count, len(created) - 1)
        self.assertEqual(marker.read_bytes(), winner)
        return result

    def test_wrong_rename_error_with_valid_winner_fails_for_its_reason(self):
        for method in ('test_independent_processes_keep_first_published_id',
                       'test_simultaneous_publication_in_six_processes'):
            for code in (5, 32, 31):
                with self.subTest(method=method, winerror=code):
                    result = self.unrelated_rename_control(method, code)
                    self.assertEqual(len(result.failures), 1)
                    self.assertEqual(result.errors, [])
                    self.assertIn('Wrong rename failure reason', result.failures[0][1])

    def test_collision_codes_are_explicit_and_platform_specific(self):
        case = HomeInitialization()
        for code in (80, 183):
            error = OSError(errno.EEXIST, 'existing destination'); error.winerror = code
            case.assert_collision(rename_error(error), platform_name='win32')
        case.assert_collision(rename_error(FileExistsError(errno.EEXIST, 'exists')), platform_name='darwin')
        # A raw NTSTATUS or errno alone does not establish a Win32 rename collision.
        error = OSError(errno.EEXIST, 'no Win32 error'); error.status = 0xC0000035
        with self.assertRaisesRegex(AssertionError, 'Wrong rename failure reason'):
            case.assert_collision(rename_error(error), platform_name='win32')

    def test_early_child_exit_does_not_count_as_barrier(self):
        for method in ('test_independent_processes_keep_first_published_id',
                       'test_simultaneous_publication_in_six_processes'):
            with self.subTest(method=method):
                failure = self.assert_rejected(method, None, {})
                self.assertIn('Every contender must reach', failure)

    def test_barrier_timeout_is_a_failure(self):
        case = HomeInitialization()
        process = SimpleNamespace(stdout=io.StringIO('rename-ready\n'))
        with patch.object(queue.Queue, 'get', side_effect=queue.Empty):
            result = unittest.TestResult()
            unittest.FunctionTestCase(lambda: case.barrier_line(process, timeout=0.01)).run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('did not reach the rename barrier before timeout', result.failures[0][1])


if __name__ == '__main__': unittest.main()
