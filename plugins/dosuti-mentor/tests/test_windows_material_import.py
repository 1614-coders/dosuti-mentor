"""Single-file models and native NTFS security cases; mocks are not native PASS."""
from contextlib import contextmanager
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import course
import registry_update as registry
import windows_material_import as win
import windows_managed_home as n


class MaterialModels(unittest.TestCase):
    def native_open(self, status=0, pending=False):
        store = win.SingleFile(Path.cwd())
        captured = {}
        api = object.__new__(n.Native)
        api.to_dos = lambda code: 5
        api.close = Mock()
        def create(handle, access, attributes, ios, allocation, attrs, share, disposition, options, ea, size):
            oa = c.cast(attributes, c.POINTER(n.OBJECT_ATTRIBUTES)).contents
            text = oa.ObjectName.contents
            captured.update(parent=oa.RootDirectory, descriptor=oa.SecurityDescriptor,
                attributes=oa.Attributes, access=access, share=share, disposition=disposition,
                options=options, name=c.string_at(text.Buffer, text.Length), ios=ios)
            c.cast(handle, c.POINTER(n.HANDLE))[0] = 123
            block = c.cast(ios, c.POINTER(n.IO_STATUS_BLOCK)).contents
            block.Information = 2 if disposition == 2 else 1
            block.Result.Status = 0x103 if pending else n.I32(status).value
            return 0x103 if pending else n.I32(status).value
        api.create = Mock(side_effect=create)
        api.wait_io = Mock()
        store.api = api
        @contextmanager
        def descriptor(text):
            captured['sddl'] = text
            try: yield n.HANDLE(456)
            finally: captured['freed'] = True
        store.security = NS(sid='S-1-5-21-1', descriptor=descriptor, verify=Mock())
        session = NS(handles=[], inspect=Mock())
        def own(handle): session.handles.append(handle); return handle
        session.own = own
        return store, session, captured

    def test_descriptor_readback_refuses_wrong_acl_or_missing_protection(self):
        for protected, actual in ((False, b'expected'), (True, b'foreign')):
            with self.subTest(protected=protected, actual=actual):
                security = object.__new__(win.Security)
                security.api = NS(boolean=lambda value: self.assertTrue(value))
                @contextmanager
                def descriptor(text): yield n.HANDLE(456)
                security.descriptor = descriptor
                security.acl = Mock(side_effect=[(None, b'expected'), (None, actual)])
                def get(handle, kind, info, owner, group, acl, sacl, value):
                    c.cast(value, c.POINTER(n.HANDLE))[0] = 789
                    return 0
                def control(value, flags, revision):
                    c.cast(flags, c.POINTER(c.c_uint16))[0] = 0x1000 if protected else 0
                    return 1
                security.get, security.control, security.free = get, control, Mock()
                with self.assertRaisesRegex(ValueError, 'readback mismatch'):
                    security.verify(123, 'expected SDDL')
                security.free.assert_called_once()

    def test_file_seal_sets_readonly_without_normal_and_uses_protected_dacl(self):
        security = object.__new__(win.Security); security.sid = 'S-1-5-21-1'
        @contextmanager
        def descriptor(text): yield n.HANDLE(456)
        security.descriptor = descriptor
        security.acl = Mock(return_value=(n.HANDLE(789), b'ACL'))
        security.set, security.verify_seal = Mock(return_value=0), Mock()
        captured = {}
        def info(handle, kind, value, size):
            c.cast(value, c.POINTER(win.BASIC_INFO)).contents.FileAttributes = 0x80
            return 1
        def set_info(handle, kind, value, size):
            captured.update(kind=kind, size=size,
                attributes=c.cast(value, c.POINTER(win.BASIC_INFO)).contents.FileAttributes)
            return 1
        security.api = NS(boolean=lambda value: self.assertTrue(value), info=info, set_info=set_info)
        security.seal(123, False)
        self.assertEqual(security.set.call_args.args[:3], (123, 1, 0x80000004))
        self.assertEqual(captured, dict(kind=0, size=40, attributes=1))
        security.verify_seal.assert_called_once_with(123, False)

    def test_seal_readback_requires_file_readonly_attribute(self):
        security = object.__new__(win.Security); security.sid = 'S-1-5-21-1'
        security.verify = Mock()
        security.api = NS(boolean=lambda value: self.assertTrue(value), info=Mock(return_value=1))
        with self.assertRaisesRegex(ValueError, 'readonly attribute'):
            security.verify_seal(123, False)

    def test_native_write_handles_short_write_and_refuses_zero_write(self):
        api = object.__new__(n.Native)
        calls = []
        def write(handle, buffer, length, count, overlapped):
            calls.append(c.string_at(buffer, length))
            c.cast(count, c.POINTER(n.U32))[0] = min(length, 2)
            return 1
        api.write_file = write
        api.write(123, b'abcde')
        self.assertEqual(calls, [b'abcde', b'cde', b'e'])
        def zero(handle, buffer, length, count, overlapped):
            c.cast(count, c.POINTER(n.U32))[0] = 0
            return 1
        api.write_file = zero
        with self.assertRaisesRegex(OSError, 'Incomplete'): api.write(123, b'data')

    def test_publish_refuses_live_writable_handle_before_registry(self):
        store = win.SingleFile(Path.cwd()); store.writers.add(123)
        with patch.object(win.registry, 'update') as update:
            with self.assertRaisesRegex(ValueError, 'Writable material HANDLE'):
                store.publish(NS(kind='code'), Path('input.py'))
            update.assert_not_called()

    def test_creation_descriptor_root_options_and_exact_disposition(self):
        store, session, seen = self.native_open()
        self.assertEqual(store.open(session, 789, 'пример.py', False, win.FILE_WRITE, create=True), 123)
        self.assertEqual((seen['parent'], seen['descriptor'], seen['attributes']), (789, 456, 0x1040))
        self.assertEqual((seen['disposition'], seen['options'], seen['share']), (2, 0x200060, 1))
        self.assertEqual(seen['name'], 'пример.py'.encode('utf-16-le'))
        self.assertEqual(seen['sddl'], 'D:P(A;;0x001f01ff;;;S-1-5-21-1)')
        self.assertTrue(seen['freed'])
        self.assertEqual(seen['access'] & n.SYNC, n.SYNC)
        self.assertEqual(session.handles, [123])
        store.api.wait_io.assert_not_called()
        store.security.verify.assert_called_once()

    def test_collision_access_and_unsupported_have_no_retry_or_fallback(self):
        for status in (0xC0000035, 0xC0000022, 0xC0000043, 0xC00000BB):
            with self.subTest(status=status):
                store, session, seen = self.native_open(status)
                with self.assertRaises(n.NativeError) as caught:
                    store.open(session, 789, 'input.py', False, win.FILE_WRITE, create=True)
                self.assertEqual(caught.exception.status, status)
                store.api.create.assert_called_once()
                store.api.wait_io.assert_not_called()
                self.assertEqual(session.handles, [])
                self.assertTrue(seen['freed'])

    def test_real_pending_retains_descriptor_and_tracks_handle_before_interrupt(self):
        store, session, seen = self.native_open(pending=True)
        calls = []
        def wait(*args):
            self.assertNotIn('freed', seen)
            calls.append(args)
            if len(calls) == 1: raise KeyboardInterrupt('pending wait interrupted')
            c.cast(seen['ios'], c.POINTER(n.IO_STATUS_BLOCK)).contents.Result.Status = 0
            return 0
        store.api.wait_io.side_effect = wait
        with self.assertRaises(KeyboardInterrupt):
            store.open(session, 789, 'input.py', False, win.FILE_WRITE, create=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(session.handles, [123])
        self.assertTrue(seen['freed'])
        store.retire(session, 123)
        store.api.close.assert_called_once_with(123)

    def test_seal_requires_access_denied_not_sharing_failure(self):
        store = win.SingleFile(Path.cwd())
        store.lesson = NS()
        for status in (0xC0000043, 0xC0000034):
            with patch.object(store, 'open', side_effect=n.NativeError('probe', status, 5)):
                with self.assertRaises(n.NativeError): store.denied(123, 'file.py', False)
        with patch.object(store, 'open', side_effect=n.NativeError('probe', 0xC0000022, 5)):
            store.denied(123, 'file.py', False)
        with patch.object(store, 'open', return_value=456), patch.object(store, 'retire') as close:
            with self.assertRaisesRegex(ValueError, 'did not deny'):
                store.denied(123, 'file.py', False)
            close.assert_called_once_with(store.lesson, 456)

    def test_masks_do_not_grant_data_append_or_child_mutation(self):
        for mask in (win.SEALED_FILE, win.SEALED_DIRECTORY):
            self.assertEqual(mask & (2 | 4 | 0x40), 0)
            self.assertEqual(mask & win.READ_CONTROL, win.READ_CONTROL)
        self.assertEqual(c.sizeof(win.BASIC_INFO), 40)
        self.assertEqual(win.BASIC_INFO.FileAttributes.offset, 32)

    def test_close_failure_retains_ownership_never_retries_numeric_handle(self):
        store = win.SingleFile(Path.cwd())
        first, second = NS(handles=[1]), NS(handles=[2])
        store.sessions = [first, second]
        error = OSError('NtClose failed')
        store.api = NS(close=Mock(side_effect=[error, None]))
        with self.assertRaises(OSError): store.close()
        self.assertEqual(first.handles, [])
        self.assertEqual(second.handles, [2])
        store.close()
        self.assertEqual(store.api.close.call_count, 2)
        win._QUARANTINED.remove(store)

    def test_reused_successfully_closed_handle_is_owned_again(self):
        store = win.SingleFile(Path.cwd())
        session = NS(handles=[123])
        store.api = NS(close=Mock())
        store.retire(session, 123)
        session.handles.append(123)  # A new open returned the same numeric value.
        store.retire(session, 123)
        self.assertEqual(store.api.close.call_count, 2)
        self.assertEqual(session.handles, [])

    def test_bound_registry_refuses_other_lesson_before_transaction(self):
        fixture = NativeSingleFile('test_round_trip_all_single_file_kinds_and_history')
        fixture.setUp()  # Files/JSON only; no native Windows operations.
        self.addCleanup(fixture.doCleanups)
        encoded = course.anchored(course.read(fixture.registry), fixture.registry)
        self.assertEqual(encoded['video']['source'], 'https://example.test/video')
        store = win.BoundRegistry(Path.cwd(), ('expected', b'id'))
        store.session, store.parent = NS(inspect=Mock(return_value=(('foreign', b'id'), 0))), 123
        with patch.object(win.WindowsRegistry, '__enter__', return_value=store), \
                patch.object(win.WindowsRegistry, '__exit__') as close:
            with self.assertRaisesRegex(ValueError, 'lesson identity'):
                store.__enter__()
        close.assert_called_once()

    def test_unsafe_components_and_unsupported_inputs_fail_before_native_work(self):
        for name in ('..', 'a:b', 'x\\y', 'x/y', 'CON', 'AUX.txt', 'file.', 'file ', 'x\0y'):
            with self.subTest(name=name), self.assertRaises(ValueError): win.component(name)
        for source in ('file.zip', 'file.pdf', 'file.html'):
            with patch.object(win, 'owned_io') as work:
                with self.assertRaises(ValueError):
                    win.attach_single(NS(kind='code', source=source), Path.cwd())
                work.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Requires native Windows x64/local NTFS; models are separate')
class NativeSingleFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.directory = self.root / 'lesson-01'
        self.directory.mkdir()
        self.registry = self.directory / 'lesson.json'
        self.registry.write_text(json.dumps(dict(id='lesson-01', revision=0, code={}, homework={},
            resources={'items': []}, video={'source': 'https://example.test/video'})), encoding='utf-8')
        self.original = self.registry.read_bytes()
        self.source = self.root / 'пример with spaces.py'
        self.payload = (b'\tprint("example")\r\n' * 400) + b'# no final newline'
        self.source.write_bytes(self.payload)
        self.progress = self.directory / 'progress.bin'
        self.progress.write_bytes(b'EXISTING LEARNER DATA')
        self.foreign = self.root / 'foreign'
        self.foreign.mkdir()
        (self.foreign / self.source.name).write_bytes(b'FOREIGN DATA')

    def args(self, kind='code'):
        return NS(kind=kind, source=str(self.source), resource_type='file')

    def attach(self, kind='code'):
        # Public material dispatch + ownership wrapper, without course bootstrap.
        import safe_material_import as safe
        from material_import import _CURRENT, cleanup
        state = dict(directory=self.directory, owned=[])
        token = _CURRENT.set(state)
        try:
            safe.attach(self.args(kind), self.directory)
            cleanup(state)
        finally:
            if state.get('safe') is not None: state['safe'].close()
            _CURRENT.reset(token)

    def intact(self):
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(self.progress.read_bytes(), b'EXISTING LEARNER DATA')
        self.assertEqual((self.foreign / self.source.name).read_bytes(), b'FOREIGN DATA')

    def test_round_trip_all_single_file_kinds_and_history(self):
        for kind in ('code', 'homework', 'resources', 'code'):
            with self.subTest(kind=kind): self.attach(kind)
        value = course.read(self.registry)
        code = value['code']
        path = Path(code['snapshot']) / self.source.name
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(code['inventory'][self.source.name]['sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(Path(value['homework']['file']).read_bytes(), self.payload)
        self.assertEqual(Path(value['resources']['items'][0]['source']).read_bytes(), self.payload)
        self.assertEqual(len(code['history']), 1)
        self.assertEqual(value['revision'], 4)
        self.intact()

    def test_seal_native_rights_and_writable_handles_closed_before_registration(self):
        original = win.SingleFile.publish
        reached = []
        def publish(store, *args):
            reached.append(True)
            # Only read/pin handles remain; WRITE and add-child opens must yield
            # ACCESS_DENIED, rather than merely a sharing conflict.
            self.assertEqual(store.sessions[0].inspect(store.file, False)[0], store.file_id)
            self.assertEqual(store.writers, set())
            self.assertTrue((store.folder / store.name).stat().st_file_attributes & 1)
            with self.assertRaises(OSError): (store.folder / 'unexpected.txt').write_bytes(b'bad')
            with self.assertRaises(OSError): (store.folder / store.name).write_bytes(b'bad')
            return original(store, *args)
        with patch.object(win.SingleFile, 'publish', publish): self.attach()
        self.assertEqual(reached, [True])
        self.intact()

    def test_file_and_snapshot_replacement_before_registration_are_refused(self):
        original = win.SingleFile.verify
        for directory in (False, True):
            with self.subTest(directory=directory):
                attacked = []
                def attack(store):
                    original(store)  # Require successful preparation/sealing first.
                    if not attacked:
                        attacked.append(True)
                        target = store.folder if directory else store.folder / store.name
                        with self.assertRaises(OSError) as blocked:
                            target.rename(target.with_name(target.name + '-moved'))
                        self.assertIn(blocked.exception.winerror, (5, 32))
                    return original(store)
                with patch.object(win.SingleFile, 'verify', attack): self.attach()
                self.assertEqual(attacked, [True], 'The attack must actually run')
                value = course.read(self.registry)['code']
                self.assertEqual((Path(value['snapshot']) / self.source.name).read_bytes(), self.payload)
                self.intact()

    def test_source_symlink_and_junction_are_refused_without_foreign_writes(self):
        link = self.root / 'linked.py'
        os.symlink(self.source, link)  # Lack of privilege is an error, never a security skip.
        args = self.args(); args.source = str(link)
        with self.assertRaises((ValueError, OSError)): win.attach_single(args, self.directory)
        junction = self.root / 'junction'
        result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(junction), str(self.foreign)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args.source = str(junction / self.source.name)
        with self.assertRaises((ValueError, OSError)): win.attach_single(args, self.directory)
        self.assertEqual(self.registry.read_bytes(), self.original)
        self.intact()

    def test_write_and_registration_failures_preserve_existing_and_foreign_data(self):
        with patch.object(n.Native, 'write', side_effect=OSError('injected write failure')):
            with self.assertRaisesRegex(OSError, 'injected write failure'): self.attach()
        self.assertEqual(self.registry.read_bytes(), self.original)
        calls = []
        def fail(*args, **kwargs):
            calls.append(True)
            raise OSError('injected registration failure')
        with patch.object(win.registry, 'update', fail):
            with self.assertRaisesRegex(OSError, 'injected registration failure'): self.attach()
        self.assertEqual(calls, [True])
        self.assertEqual(self.registry.read_bytes(), self.original)
        self.intact()
        self.attach()  # Explicit new import; old residue is never adopted/deleted.
        self.intact()

    def test_lesson_binding_is_held_through_registry_commit(self):
        original = win.SingleFile.publish
        reached = []
        def publish(store, *args):
            store.verify()
            reached.append(True)
            with self.assertRaises(OSError) as blocked:
                self.directory.rename(self.root / 'moved-lesson')
            self.assertIn(blocked.exception.winerror, (5, 32))
            return original(store, *args)
        with patch.object(win.SingleFile, 'publish', publish): self.attach()
        self.assertEqual(reached, [True])
        self.assertEqual(course.read(self.registry)['revision'], 1)
        self.intact()

    def test_flush_and_readback_failure_never_register(self):
        with patch.object(n.Native, 'flush', side_effect=OSError('injected flush failure')):
            with self.assertRaisesRegex(OSError, 'injected flush failure'): self.attach()
        self.assertEqual(self.registry.read_bytes(), self.original)
        original = win.SingleFile.read
        def corrupt(store, handle):
            value = original(store, handle)
            return value + b'wrong' if store.writers else value
        with patch.object(win.SingleFile, 'read', corrupt):
            with self.assertRaisesRegex(OSError, 'readback mismatch'): self.attach()
        self.assertEqual(self.registry.read_bytes(), self.original)
        self.intact()

    def test_unconfirmed_native_seal_never_reaches_registry(self):
        with patch.object(win.Security, 'seal', side_effect=ValueError('native seal unavailable')), \
                patch.object(win.registry, 'update') as update:
            with self.assertRaisesRegex(ValueError, 'native seal unavailable'): self.attach()
            update.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), self.original)
        self.intact()

    def test_post_commit_verification_failure_retains_receipt_and_material(self):
        original = win.SingleFile.verify
        calls = []
        def fail_after_commit(store):
            calls.append(True)
            if store.receipt is not None: raise ValueError('injected post-commit verification failure')
            return original(store)
        with patch.object(win.SingleFile, 'verify', fail_after_commit):
            with self.assertRaises(registry.CommitInterrupted) as caught: self.attach()
        self.assertEqual(caught.exception.outcome, 'committed')
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertGreaterEqual(len(calls), 3)
        value = course.read(self.registry)
        self.assertEqual(value['revision'], 1)
        self.assertEqual((Path(value['code']['snapshot']) / self.source.name).read_bytes(), self.payload)
        self.intact()

    def test_successful_close_retires_all_owned_handles_and_is_idempotent(self):
        original = win.SingleFile.close
        observations = []
        def close(store):
            result = original(store)
            observations.append(([list(s.handles) for s in store.sessions], set(store.writers)))
            return result
        with patch.object(win.SingleFile, 'close', close): self.attach()
        self.assertGreaterEqual(len(observations), 2)
        for handles, writers in observations:
            self.assertTrue(all(not values for values in handles))
            self.assertEqual(writers, set())
        self.intact()

    def test_unexpected_child_is_preserved_but_never_registered(self):
        original = win.Security.seal
        created = []
        def extra(security, handle, directory):
            if directory:
                folder = next((self.directory / 'code').iterdir())
                path = folder / 'foreign-extra.txt'
                path.write_bytes(b'FOREIGN CHILD')
                created.append(path)
            return original(security, handle, directory)
        with patch.object(win.Security, 'seal', extra):
            with self.assertRaisesRegex(ValueError, 'inventory changed'): self.attach()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].read_bytes(), b'FOREIGN CHILD')
        self.assertEqual(self.registry.read_bytes(), self.original)
        self.intact()

    def test_invalid_lesson_is_rejected_before_snapshot_creation(self):
        self.registry.write_bytes(b'[]')
        with self.assertRaisesRegex(ValueError, 'registry identity'): self.attach()
        self.assertFalse((self.directory / 'code').exists())
        self.assertEqual(self.registry.read_bytes(), b'[]')
        self.intact()

    def test_collision_preserves_unowned_snapshot_and_no_retry(self):
        parent = self.directory / 'code'; parent.mkdir()
        collision = parent / ('a' * 12); collision.mkdir()
        (collision / 'keep').write_bytes(b'FOREIGN COLLISION')
        with patch.object(win.uuid, 'uuid4', return_value=NS(hex='a' * 32)):
            with self.assertRaises(n.NativeError) as caught: self.attach()
        self.assertEqual(caught.exception.status, 0xC0000035)
        self.assertEqual((collision / 'keep').read_bytes(), b'FOREIGN COLLISION')
        self.assertEqual(self.registry.read_bytes(), self.original)
        self.intact()


if __name__ == '__main__': unittest.main()
