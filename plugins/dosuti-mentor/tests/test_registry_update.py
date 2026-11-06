"""Registry contract runs against the selected native backend, without import pipeline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import ctypes as c
import _thread
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

import course
import registry_update as registry
import safe_zip_import


class RegistryContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve() / 'lesson-01'
        self.directory.mkdir()
        self.path = self.directory / 'lesson.json'
        self.path.write_text(json.dumps(dict(id='lesson-01', revision=0, rows=[], video={'source': 'https://example.test/video'}, code={}, homework={}, resources={'items': []})))
        self.old = self.path.read_bytes()

    def test_both_writers_share_revision_and_serialization(self):
        first = course.update_lesson(self.directory, lambda d: d['rows'].append('course'), expected_revision=0)
        second = safe_zip_import.publish(None, self.directory, None, None, None,
                    change=lambda d: d['rows'].append('safe'), expected_revision=1)
        value = course.read(self.path)
        self.assertEqual(value['rows'], ['course', 'safe'])
        self.assertEqual(value['revision'], 2)
        self.assertEqual((first.revision, second.revision), (1, 2))
        self.assertEqual(registry.recover(self.directory, second), 'committed')

    def test_stale_revision_does_not_mutate_or_create_a_temp(self):
        mutator = unittest.mock.Mock()
        with self.assertRaises(registry.RevisionConflict):
            course.update_lesson(self.directory, mutator, expected_revision=2)
        mutator.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(list(self.directory.glob('*.tmp')), [])

    def test_legacy_course_registry_without_id_keeps_previous_contract(self):
        value = json.loads(self.old)
        value.pop('id')
        self.path.write_text(json.dumps(value))
        course.update_lesson(self.directory, lambda d: d['rows'].append('legacy'))
        before = self.path.read_bytes()
        self.assertEqual(json.loads(before)['rows'], ['legacy'])
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            safe_zip_import.publish(None, self.directory, None, None, None, change=lambda d: None)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_registry_and_mutator_failure_preserve_old(self):
        with self.assertRaises(KeyboardInterrupt):
            registry.update(self.directory, lambda d: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(self.path.read_bytes(), self.old)
        self.path.write_bytes(b'[]')
        with self.assertRaises(ValueError): registry.update(self.directory, lambda d: None)
        self.assertEqual(self.path.read_bytes(), b'[]')

    def test_before_and_after_commit_faults_are_reconciled_without_retry(self):
        for applied in (False, True):
            with self.subTest(applied=applied):
                self.path.write_bytes(self.old)
                store_type = registry.backend_type()
                original = store_type.replace
                calls = []
                def failing(store, stage):
                    calls.append(stage)
                    if applied: original(store, stage)
                    raise KeyboardInterrupt()
                with patch.object(store_type, 'replace', failing):
                    with self.assertRaises(registry.CommitInterrupted) as caught:
                        registry.update(self.directory, lambda d: d['rows'].append('new'))
                self.assertEqual(len(calls), 1, 'Never blindly retry rename')
                expected = 'committed' if applied else 'unchanged'
                self.assertEqual(caught.exception.outcome, expected)
                self.assertEqual(registry.recover(self.directory, caught.exception.receipt), expected)
                value = json.loads(self.path.read_bytes())
                self.assertEqual(value['rows'], ['new'] if applied else [])

    def test_foreign_temporary_is_not_replaced_or_removed(self):
        name = 'lesson.json.fixed.tmp'
        foreign = self.directory / name
        foreign.write_bytes(b'FOREIGN')
        with patch.object(registry, 'temporary_name', return_value=name):
            with self.assertRaises((FileExistsError, OSError)):
                registry.update(self.directory, lambda d: d['rows'].append('new'))
        self.assertEqual(foreign.read_bytes(), b'FOREIGN')
        self.assertEqual(self.path.read_bytes(), self.old)

    def test_symlink_registry_is_refused_without_target_damage(self):
        target = self.directory / 'foreign.json'
        target.write_bytes(self.old)
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            registry.update(self.directory, lambda d: d['rows'].append('bad'))
        self.assertEqual(target.read_bytes(), self.old)

    def test_stage_failure_leaves_old_registry_and_preserves_residue(self):
        store_type = registry.backend_type()
        original = store_type.stage
        def fail(store, name, payload):
            original(store, name, payload)
            raise OSError('flush/readback fault before publication')
        with patch.object(store_type, 'stage', fail):
            with self.assertRaises(OSError): registry.update(self.directory, lambda d: d['rows'].append('bad'))
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(len(list(self.directory.glob('*.tmp'))), 1)

    def test_hardlink_lock_is_rejected_without_foreign_changes(self):
        foreign = self.directory.parent / 'foreign-lock'
        foreign.write_bytes(b'FOREIGN')
        os.link(foreign, self.directory / '.lesson.lock')
        with self.assertRaises((ValueError, OSError)):
            registry.update(self.directory, lambda d: d['rows'].append('bad'))
        self.assertEqual(foreign.read_bytes(), b'FOREIGN')
        self.assertEqual(self.path.read_bytes(), self.old)

    def test_existing_lock_helper_blocks_registry_writer(self):
        from storage_lock import file_lock
        script = r'''
import sys
from pathlib import Path
import course
p=Path(sys.argv[1]); (p.parent/'ready-lock').write_bytes(b'1')
course.update_lesson(p,lambda d:d['rows'].append('child'))
'''
        process = None
        try:
            with file_lock(self.directory / '.lesson.lock'):
                process = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.directory)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                deadline = time.monotonic() + 10
                while not (self.directory.parent / 'ready-lock').exists():
                    self.assertIsNone(process.poll(), 'Child exited before contention')
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                time.sleep(.05)
                self.assertIsNone(process.poll(), 'Writer bypassed the existing lock helper')
                self.assertEqual(self.path.read_bytes(), self.old)
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertEqual(course.read(self.path)['rows'], ['child'])
        finally:
            if process is not None:
                if process.poll() is None: process.kill()
                process.communicate(timeout=5)

    def test_next_writer_reads_while_first_adapter_exit_is_parked(self):
        store_type = registry.backend_type()
        stage, close = store_type.stage, store_type.__exit__
        first_store, parked, release = [], threading.Event(), threading.Event()
        def staged(store, *args):
            value = stage(store, *args)
            if not first_store: first_store.append(store)
            return value
        def closing(store, *args):
            if store is first_store[0]:
                parked.set()
                if not release.wait(10): raise AssertionError('First adapter exit was never released')
            return close(store, *args)
        with patch.object(store_type, 'stage', staged), patch.object(store_type, '__exit__', closing):
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(registry.update, self.directory, lambda d: d['rows'].append('first'))
                try:
                    self.assertTrue(parked.wait(5), 'First writer must release lock and reach adapter exit')
                    second = pool.submit(registry.update, self.directory, lambda d: d['rows'].append('second'))
                    self.assertEqual(second.result(timeout=5).revision, 2,
                                     'Next lock holder must fresh-read without a sharing failure')
                finally: release.set()
                self.assertEqual(first.result(timeout=5).revision, 1)
        self.assertEqual(course.read(self.path)['rows'], ['first', 'second'])

    def test_independent_mixed_writers_and_atomic_readers(self):
        worker = r'''
import json,sys,time
from pathlib import Path
import course,safe_zip_import
p=Path(sys.argv[1]); number=int(sys.argv[2]); gate=p.parent/'go'
(p.parent/('ready'+str(number))).write_bytes(b'1')
deadline=time.monotonic()+15
while not gate.exists():
    if time.monotonic()>deadline: raise TimeoutError('start barrier')
    time.sleep(.01)
for i in range(5):
    def change(d):
        d['rows'].append(str(number)+':'+str(i)); time.sleep(.005)
    if number%2: safe_zip_import.publish(None,p,None,None,None,change=change)
    else: course.update_lesson(p,change)
'''
        processes = []
        try:
            for number in range(4):
                processes.append(subprocess.Popen([sys.executable, '-B', '-c', worker,
                    str(self.directory), str(number)], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + 20
            while not all((self.directory.parent / ('ready' + str(i))).exists() for i in range(4)):
                self.assertLess(time.monotonic(), deadline, 'All writers must reach start barrier')
                self.assertTrue(all(p.poll() is None for p in processes), 'Early child exit')
                time.sleep(.01)
            (self.directory.parent / 'go').write_bytes(b'1')
            while any(p.poll() is None for p in processes):
                self.assertLess(time.monotonic(), deadline, 'Writer deadline')
                value = json.loads(self.atomic_read())
                self.assertEqual(value['revision'], len(value['rows']))
                time.sleep(.001)
            for p in processes:
                _, error = p.communicate(timeout=2)
                self.assertEqual(p.returncode, 0, error.decode())
            value = course.read(self.path)
            self.assertEqual(value['revision'], 20)
            self.assertEqual(len(set(value['rows'])), 20)
        finally:
            for p in processes:
                if p.poll() is None: p.kill()
                p.communicate(timeout=5)

    def atomic_read(self):
        if sys.platform != 'win32': return self.path.read_bytes()
        # A live atomic reader must share DELETE. Normal Path readers may deny
        # replacement on Windows; that is a controlled failure, not torn data.
        import msvcrt
        import windows_managed_home as n
        api = n.Native()
        handle = api.create_root(str(self.path), 0x80000000, 7, None, 3, 0x200000, None)
        if handle == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
        try: fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            api.close(handle)
            raise
        with os.fdopen(fd, 'rb') as stream: return stream.read()


class WindowsRegistryModels(unittest.TestCase):
    """Models only on POSIX; these never count as native NTFS acceptance."""
    def test_replace_buffer_status_and_pending(self):
        import registry_windows as win
        import windows_managed_home as n
        for pending, status in ((False, 0), (True, 0), (False, 0xC0000035),
                                (False, 0xC0000022), (True, 0xC0000043)):
            with self.subTest(pending=pending, status=status):
                api = object.__new__(n.Native)
                api.to_dos = lambda value: 5
                captured = {}
                def call(handle, ios, buffer, length, kind):
                    captured.update(handle=handle, ios=ios, raw=buffer.raw, length=length, kind=kind)
                    return 0x103 if pending else n.I32(status).value
                def wait(*args):
                    captured['waited'] = args
                    c.cast(captured['ios'], c.POINTER(n.IO_STATUS_BLOCK)).contents.Result.Status = n.I32(status).value
                    return 0
                api.rename_info, api.wait_io = call, wait
                if status:
                    with self.assertRaises(n.NativeError) as error: win.replace_relative(api, 123, 456)
                    self.assertEqual(error.exception.status, status)
                else: win.replace_relative(api, 123, 456)
                header = n.FILE_RENAME_INFO.from_buffer_copy(captured['raw'])
                self.assertEqual((captured['handle'], captured['kind']), (123, 10))
                self.assertEqual(captured['length'], c.sizeof(n.FILE_RENAME_INFO) + len('lesson.json'.encode('utf-16-le')))
                self.assertEqual((header.Flags, header.RootDirectory, header.FileNameLength), (1, 456, 22))
                self.assertEqual(captured['raw'][20:42], 'lesson.json'.encode('utf-16-le'))
                self.assertEqual('waited' in captured, pending)

    def test_lock_open_is_relative_and_shares_read_write_not_delete(self):
        import registry_windows as win
        import windows_managed_home as n
        store = object.__new__(win.WindowsRegistry)
        store.parent = 456
        api = object.__new__(n.Native)
        captured = {}
        def create(handle, access, oa, ios, allocation, attrs, share, disposition, options, ea, size):
            captured.update(access=access, share=share, disposition=disposition, options=options,
                parent=c.cast(oa, c.POINTER(n.OBJECT_ATTRIBUTES)).contents.RootDirectory)
            c.cast(handle, c.POINTER(n.HANDLE))[0] = 123
            c.cast(ios, c.POINTER(n.IO_STATUS_BLOCK)).contents.Information = 1
            return 0
        api.create = create
        store.api, store.session = api, SimpleNamespace(inspect=lambda *args: None)
        self.assertEqual(store._open_lock(), 123)
        self.assertEqual((captured['share'], captured['parent'], captured['disposition']), (3, 456, 3))
        self.assertEqual(captured['options'], 0x200000 | 0x20 | 0x40)
        self.assertEqual(captured['access'], 0xC0000000 | n.SYNC | n.ATTR)

    def test_worker_launch_interrupt_never_submits(self):
        import registry_windows as win
        original = threading.Thread.start
        for launched in (False, True):
            calls, workers = [], []
            def start(worker):
                workers.append(worker)
                if launched: original(worker)
                raise KeyboardInterrupt()
            with patch.object(threading.Thread, 'start', start):
                with self.assertRaises(KeyboardInterrupt): win.owned_io(lambda: calls.append(True))
            for worker in workers:
                if launched:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
            self.assertEqual(len(workers), 1)
            self.assertEqual(calls, [])

    def test_main_interrupt_has_commit_receipt_after_worker_finishes(self):
        import registry_windows as win
        receipt = registry.Receipt('directory', 1, 'new', 'identity', 'old', 'previous', 'stage')
        completed = []
        def execute():
            _thread.interrupt_main()
            completed.append(threading.get_ident())
            return receipt
        with self.assertRaises(registry.CommitInterrupted) as error: win.owned_io(execute)
        self.assertEqual(error.exception.receipt, receipt)
        self.assertEqual(error.exception.outcome, 'committed')
        self.assertNotEqual(completed, [threading.get_ident()])
        self.assertEqual(len(completed), 1)

    def test_delayed_sigint_does_not_replace_original_commit_failure_cause(self):
        import registry_windows as win
        receipt = registry.Receipt('directory', 1, 'new', 'identity', 'old', 'previous', 'stage')
        original = OSError('original verification failure')
        failure = registry.CommitInterrupted(receipt, 'unknown')
        def execute():
            _thread.interrupt_main()
            raise failure from original
        with self.assertRaises(registry.CommitInterrupted) as caught: win.owned_io(execute)
        self.assertIs(caught.exception, failure)
        self.assertIs(caught.exception.__cause__, original)

    def test_crt_lock_owns_handle_and_matches_existing_byte_protocol(self):
        import registry_windows as win
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.lesson.lock'
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            calls = []
            fake = SimpleNamespace(open_osfhandle=lambda handle, flags: handle,
                LK_NBLCK=2, LK_UNLCK=0,
                locking=lambda descriptor, mode, count: calls.append((descriptor, mode, count)))
            store = object.__new__(win.WindowsRegistry)
            store.session = SimpleNamespace(check=lambda: None, close=lambda: None, handles=[])
            store.stages = []
            store._open_lock = lambda: fd
            store.api = SimpleNamespace(close=unittest.mock.Mock())
            with patch.dict(sys.modules, msvcrt=fake), patch.object(os, 'O_BINARY', 0, create=True), patch.object(os, 'O_NOINHERIT', 0, create=True):
                with store.lock(): self.assertEqual(os.fstat(fd).st_size, 0)
            self.assertEqual(calls, [(fd, 2, 1), (fd, 0, 1)])
            with self.assertRaises(OSError): os.fstat(fd)
            store.api.close.assert_not_called()
            self.assertEqual(path.read_bytes(), b'')


class RegistryPostCommitFaults(unittest.TestCase):
    def setUp(self):
        RegistryContract.setUp(self)

    def test_verification_unlock_and_adapter_close_keep_receipt_and_cause(self):
        store_type = registry.backend_type()
        for boundary in ('verification', 'unlock', 'close'):
            for kind in (OSError, KeyboardInterrupt, BaseException):
                with self.subTest(boundary=boundary, kind=kind.__name__):
                    self.path.write_bytes(self.old)
                    fault = kind('injected ' + boundary)
                    if boundary == 'verification':
                        original = store_type.read
                        reads = []
                        def read(store):
                            reads.append(True)
                            if len(reads) > 1: raise fault
                            return original(store)
                        hook = patch.object(store_type, 'read', read)
                    elif boundary == 'unlock':
                        original = store_type.lock
                        @contextmanager
                        def lock(store):
                            with original(store): yield
                            raise fault
                        hook = patch.object(store_type, 'lock', lock)
                    else:
                        original = store_type.__exit__
                        def close(store, *args):
                            original(store, *args)
                            raise fault
                        hook = patch.object(store_type, '__exit__', close)
                    with hook:
                        with self.assertRaises(registry.CommitInterrupted) as caught:
                            registry.update(self.directory, lambda d: d['rows'].append('new'))
                    error = caught.exception
                    self.assertIs(error.__cause__, fault)
                    self.assertEqual(error.outcome, 'unknown' if boundary == 'verification' else 'committed')
                    self.assertEqual(error.phase, boundary)
                    self.assertEqual(registry.recover(self.directory, error.receipt), 'committed')
                    self.assertEqual(json.loads(self.path.read_bytes())['rows'], ['new'])

    def test_cleanup_errors_do_not_replace_verification_cause(self):
        store_type = registry.backend_type()
        primary, unlock_fault, close_fault = KeyboardInterrupt('verify'), OSError('unlock'), BaseException('close')
        read, lock, close = store_type.read, store_type.lock, store_type.__exit__
        reads = []
        def failing_read(store):
            reads.append(True)
            if len(reads) > 1: raise primary
            return read(store)
        def failing_close(store, *args):
            close(store, *args)
            raise close_fault
        # finally is required so that cleanup actually fails while unwinding.
        @contextmanager
        def both_lock(store):
            try:
                with lock(store): yield
            finally: raise unlock_fault
        with patch.object(store_type, 'read', failing_read), patch.object(store_type, 'lock', both_lock), patch.object(store_type, '__exit__', failing_close):
            with self.assertRaises(registry.CommitInterrupted) as caught:
                registry.update(self.directory, lambda d: d['rows'].append('new'))
        self.assertIs(caught.exception.__cause__, primary)
        self.assertIn(unlock_fault, caught.exception.cleanup_errors)
        self.assertIn(close_fault, caught.exception.cleanup_errors)
        self.assertEqual(caught.exception.outcome, 'unknown')


class WindowsStageHandoffModels(unittest.TestCase):
    def test_stage_is_closed_before_next_writer_gets_lock(self):
        import registry_windows as win
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.lesson.lock'
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            live, closed, unlocked = {123}, [], []
            def close(handle):
                self.assertIn(handle, live, 'No double close')
                live.remove(handle); closed.append(handle)
            def locking(descriptor, mode, count):
                if mode == 0:
                    unlocked.append(True)
                    self.assertFalse(live, 'Next lock holder must not see a write-sharing stage HANDLE')
            fake = SimpleNamespace(open_osfhandle=lambda h, flags: h, LK_NBLCK=2, LK_UNLCK=0, locking=locking)
            store = object.__new__(win.WindowsRegistry)
            store.stages = [win._Stage('stage', 'identity', 123)]
            store.session = SimpleNamespace(check=lambda: None, handles=[123], close=lambda: None)
            store._open_lock = lambda: fd
            def ntclose(handle):
                close(handle)
                return 0
            store.api = SimpleNamespace(ntclose=ntclose, status=lambda result, operation: None)
            with patch.dict(sys.modules, msvcrt=fake), patch.object(os, 'O_BINARY', 0, create=True), patch.object(os, 'O_NOINHERIT', 0, create=True):
                with store.lock(): pass
            self.assertEqual(closed, [123])
            self.assertEqual(unlocked, [True])
            self.assertEqual(store.session.handles, [])
            self.assertEqual(store.stages, [])

    def test_failed_stage_close_retains_ownership_and_lock_without_retry(self):
        import registry_windows as win
        with tempfile.TemporaryDirectory() as tmp:
            fd = os.open(Path(tmp) / '.lesson.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            live, closed, unlocked = {123}, [], []
            fault = OSError('NtClose returned failure without closing')
            store = object.__new__(win.WindowsRegistry)
            store.stages = [win._Stage('stage', 'identity', 123)]
            store.commit_state = registry._CommitState()
            store.session = SimpleNamespace(check=lambda: None, handles=[123])
            store._open_lock = lambda: fd
            def status(result, operation): raise fault
            attempts = []
            def failed_close(handle):
                attempts.append(handle)
                return -1
            store.api = SimpleNamespace(ntclose=failed_close, status=status)
            def locking(descriptor, mode, count):
                if mode == 0:
                    unlocked.append(True)
            fake = SimpleNamespace(open_osfhandle=lambda h, flags: h, LK_NBLCK=2, LK_UNLCK=0, locking=locking)
            try:
                with patch.dict(sys.modules, msvcrt=fake), patch.object(os, 'O_BINARY', 0, create=True), patch.object(os, 'O_NOINHERIT', 0, create=True):
                    with self.assertRaises(OSError) as caught:
                        with store.lock(): pass
                self.assertIs(caught.exception, fault)
                with self.assertRaises(OSError): store.__exit__(None, None, None)
                self.assertEqual(attempts, [123])
                self.assertEqual(closed, [])
                self.assertEqual(live, {123})
                self.assertEqual(store.session.handles, [123])
                self.assertEqual(unlocked, [], 'Unconfirmed write HANDLE forbids lock handoff')
                self.assertTrue(fault.registry_lock_retained)
                self.assertEqual(store.commit_state.failures, [('stage_close', fault)])
            finally:
                # Dispose only this model's CRT fd; 123 is fictitious, no kernel
                # write HANDLE exists in this test. Production has no such API.
                owned = [item for item in win._QUARANTINED_REGISTRIES if item[0] is store]
                for item in owned:
                    win._QUARANTINED_REGISTRIES.remove(item)
                    item[1].close()

    def test_pending_io_retains_handles_and_lock_until_completion(self):
        import registry_windows as win
        import windows_managed_home as n
        with tempfile.TemporaryDirectory() as tmp:
            fd = os.open(Path(tmp) / '.lesson.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            events, active, errors = [], {}, []
            pending, complete = threading.Event(), threading.Event()
            store = object.__new__(win.WindowsRegistry)
            store.stages = [win._Stage('stage', 'identity', 123)]
            store.session = SimpleNamespace(check=lambda: None, handles=[456, 123])
            store._open_lock = lambda: fd
            def submit(handle, ios, buffer, length, kind):
                active['ios'] = ios
                return 0x103
            def wait(*args):
                events.append('pending'); pending.set()
                if not complete.wait(5): raise AssertionError('Pending model never completed')
                c.cast(active['ios'], c.POINTER(n.IO_STATUS_BLOCK)).contents.Result.Status = 0
                events.append('completed')
                return 0
            def ntclose(handle):
                events.append('stage-close' if handle == 123 else 'parent-close')
                return 0
            def locking(descriptor, mode, count):
                if mode == 0: events.append('unlock')
            store.api = SimpleNamespace(rename_info=submit, wait_io=wait, ntclose=ntclose,
                                         status=lambda result, operation: None)
            fake = SimpleNamespace(open_osfhandle=lambda h, flags: h, LK_NBLCK=2, LK_UNLCK=0, locking=locking)
            def execute():
                try:
                    with store.lock(): win.replace_relative(store.api, 123, 456)
                except BaseException as error: errors.append(error)
            with patch.dict(sys.modules, msvcrt=fake), patch.object(os, 'O_BINARY', 0, create=True), patch.object(os, 'O_NOINHERIT', 0, create=True):
                worker = threading.Thread(target=execute)
                worker.start()
                try:
                    self.assertTrue(pending.wait(2))
                    self.assertEqual(events, ['pending'])
                    self.assertEqual(store.session.handles, [456, 123])
                finally:
                    complete.set(); worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), 'No pending worker may outlive the test')
            self.assertEqual(errors, [])
            self.assertEqual(events, ['pending', 'completed', 'stage-close', 'parent-close', 'unlock'])


class RegistryStrictCleanupModels(unittest.TestCase):
    """Real adapter cleanup and HomeSession ownership; only native calls are fake."""
    def store(self, directory, failures):
        import registry_windows as win
        import windows_managed_home as n
        store = win.WindowsRegistry(directory)
        session = object.__new__(n.HomeSession)
        api = object.__new__(n.Native)
        calls = []
        def ntclose(handle):
            calls.append(handle)
            if handle in failures: raise failures[handle]
            return 0
        api.ntclose = ntclose
        session.api, session.home, session.handles = api, 456, [789, 456, 123]
        session.check = lambda: None
        store.session, store.api, store.parent = session, api, 456
        store.stages = [win._Stage('stage', 'new-identity', 123)]
        return store, session, calls

    def transaction(self, primary=None):
        import registry_windows as win
        import windows_managed_home as n
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            parent_fault, root_fault = OSError('parent NtClose fault'), OSError('root NtClose fault')
            store, session, calls = self.store(directory, {456: parent_fault, 789: root_fault})
            fd = os.open(directory / '.lesson.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            store._open_lock = lambda: fd
            fake = SimpleNamespace(open_osfhandle=lambda h, flags: h, LK_NBLCK=2, LK_UNLCK=0,
                                   locking=lambda *args: None)
            old = json.dumps(dict(revision=0, video={'source': 'https://example.test/video'},
                                  code={}, homework={}, resources={'items': []})).encode()
            payload = []
            def read():
                if not payload: return old, 'old-identity'
                if primary is not None: raise primary
                return payload[0], 'new-identity'
            store.read = read
            store.stage = lambda name, raw: (payload.append(raw) or store.stages[0])
            store.replace = unittest.mock.Mock()
            import io
            from contextlib import redirect_stderr
            stderr = io.StringIO()
            with patch.object(n, 'HomeSession', return_value=session), patch.dict(sys.modules, msvcrt=fake), \
                    patch.object(os, 'O_BINARY', 0, create=True), patch.object(os, 'O_NOINHERIT', 0, create=True), redirect_stderr(stderr):
                with self.assertRaises(registry.CommitInterrupted) as caught:
                    registry.update(directory, lambda data: None, registry=store)
            error = caught.exception
            self.assertIs(error.__cause__, primary if primary is not None else parent_fault)
            for fault in ((parent_fault, root_fault) if primary is not None else (root_fault,)):
                self.assertIn(fault, error.cleanup_errors)
            self.assertEqual(error.outcome, 'unknown' if primary is not None else 'committed')
            self.assertEqual(session.handles, [789, 456], 'Failed close must retain unconfirmed ownership')
            self.assertFalse(store._session_closed)
            self.assertEqual(stderr.getvalue(), '', 'Cleanup evidence must not disappear into stderr')
            self.assertEqual(calls, [123, 456, 789], 'Never retry an unconfirmed close on outer exit')
            self.assertEqual(parent_fault.registry_handle, 456)
            self.assertEqual(parent_fault.registry_close_outcome, 'unknown')
            store.replace.assert_called_once()

    def test_primary_verification_and_parent_close_fault_keep_all_evidence(self):
        self.transaction(OSError('primary verification fault'))

    def test_close_without_primary_keeps_receipt_and_all_close_errors(self):
        self.transaction()

    def test_successful_cleanup_and_repeat_exit_close_each_handle_once(self):
        store, session, calls = self.store(Path('.'), {})
        store._finish_under_lock()
        store.__exit__(None, None, None)
        store.__exit__(None, None, None)
        self.assertEqual(calls, [123, 456, 789])
        self.assertEqual(session.handles, [])
        self.assertEqual(store.stages, [])
        self.assertTrue(store._session_closed)

    def test_ambiguous_close_never_retries_a_reused_numeric_handle(self):
        store, session, calls = self.store(Path('.'), {})
        live = {123, 456, 789}
        fault = OSError('exception after an unobserved native close')
        def close(handle):
            calls.append(handle)
            live.remove(handle)
            if handle == 456: raise fault
            return 0
        store.api.ntclose = close
        with self.assertRaises(OSError): store._finish_under_lock()
        self.assertEqual(session.handles, [456])
        self.assertEqual(fault.registry_close_outcome, 'unknown')
        live.add(456)  # Model the OS reusing the numeric value for another owner.
        with self.assertRaises(OSError): store.__exit__(None, None, None)
        self.assertEqual(calls, [123, 456, 789])
        self.assertEqual(live, {456}, 'Unrelated reused HANDLE must not be closed')


if __name__ == '__main__': unittest.main()
