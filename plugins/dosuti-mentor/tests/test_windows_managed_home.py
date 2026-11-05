"""Portable contract checks plus native Windows x64/NTFS bootstrap regressions.

Portable ctypes checks do not establish Windows SDK/runtime compatibility.
"""
import ctypes as c
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import managed_storage as storage
import windows_managed_home as w


class WindowsHomeContracts(unittest.TestCase):
    def test_explicit_width_layouts(self):
        if c.sizeof(c.c_void_p) != 8:
            self.skipTest('x64 layout check requires 64-bit host')
        w.check_abi()
        self.assertEqual(c.sizeof(w.I32), 4)
        self.assertLess(w.I32(0xC0000034).value, 0)

    def test_lexical_rejection_does_not_access_filesystem(self):
        for path in ('relative', r'\\server\share\home', r'\\?\C:\home', 'C:home',
                     'C:\\a\\..\\b', 'C:\\a\\.\\b', 'C:\\bad.\\home',
                     'C:\\bad \\home', 'C:\\a:stream', 'C:\\CON.txt', 'C:\\a\x00'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                w.components(path)
        self.assertEqual(w.components('C:/Учёба с пробелами/home'), ('C:', ['Учёба с пробелами', 'home']))

    def test_marker_contract_bom_and_invalid_values(self):
        raw = json.dumps(dict(schema='dosuti_home_v1', id='a' * 32, extra='kept')).encode()
        self.assertEqual(w.marker_value(b'\xef\xbb\xbf' + raw)['extra'], 'kept')
        for raw in (b'[]', b'null', b'{', b'', b'\xff', b'x' * 4097,
                    b'{"schema":"dosuti_home_v1","id":"bad"}'):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError): w.marker_value(raw)

    def test_dispatch_precedes_posix_operations(self):
        with patch.object(storage.sys, 'platform', 'win32'), patch.object(storage, 'safe') as unsafe:
            with patch.object(w, 'initialize', return_value='native') as initialize:
                self.assertEqual(storage.initialize('C:\\home', True), 'native')
                initialize.assert_called_once_with('C:\\home', legacy_runtime=True)
            with patch.object(w, 'validate', return_value='native') as validate:
                self.assertEqual(storage.validate('C:\\home'), 'native')
                validate.assert_called_once_with('C:\\home')
            unsafe.assert_not_called()

    def test_swap_regression_rejects_backend_failure_before_hook(self):
        case = WindowsManagedHome('test_directory_swap_at_rename_is_blocked_or_refused')
        case.setUp()
        self.addCleanup(case.doCleanups)
        with patch.object(case, 'initialize', side_effect=OSError('early backend failure')):
            result = unittest.TestResult()
            unittest.FunctionTestCase(case.test_directory_swap_at_rename_is_blocked_or_refused).run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('competitor must run', result.failures[0][1])

    def test_pending_is_not_success(self):
        native = object.__new__(w.Native)
        native.to_dos = lambda status: 997
        with self.assertRaises(w.NativeError) as caught: native.status(0x103, 'pending')
        self.assertEqual(caught.exception.status, 0x103)

    def test_short_and_zero_write(self):
        native = object.__new__(w.Native)
        chunks = []
        def short(handle, buffer, size, result, overlapped):
            amount = min(size, 2)
            chunks.append(buffer.raw[:amount])
            c.cast(result, c.POINTER(w.U32))[0] = amount
            return 1
        native.write_file = short
        native.write(123, b'abcdefg')
        self.assertEqual(b''.join(chunks), b'abcdefg')
        native.write_file = lambda *args: 1  # Leaves count zero.
        with self.assertRaisesRegex(OSError, 'Incomplete'): native.write(123, b'a')

    def test_rename_buffer_layout_no_replace_relative_name(self):
        native = object.__new__(w.Native)
        def inspect(handle, kind, buffer, length):
            header = w.FILE_RENAME_INFO.from_buffer(buffer)
            self.assertEqual((handle, kind, header.Flags, header.RootDirectory), (123, 3, 0, 456))
            raw = buffer.raw[w.FILE_RENAME_INFO.FileName.offset:][:header.FileNameLength]
            self.assertEqual(raw.decode('utf-16-le'), w.MARKER)
            self.assertEqual(length, c.sizeof(w.FILE_RENAME_INFO) + len(raw))
            self.assertEqual(length, 58)
            self.assertEqual(len(buffer), length)
            expected = bytearray(length)
            expected[8:16] = (456).to_bytes(8, 'little')
            expected[16:20] = len(w.MARKER.encode('utf-16-le')).to_bytes(4, 'little')
            expected[20:20 + len(raw)] = w.MARKER.encode('utf-16-le')
            self.assertEqual(buffer.raw, bytes(expected), 'Fields, name and all padding must match')
            return 1
        native.set_info = inspect
        native.rename(123, 456)


class MarkerProtocolModel(unittest.TestCase):
    """State-machine tests, not evidence about NTFS or Windows sharing."""
    def session(self, outcome='success'):
        state = dict(payload=b'', committed=None)
        api = SimpleNamespace()
        session = object.__new__(w.HomeSession)
        session.api, session.parent, session.home, session.handles = api, 1, 2, []
        session.check, session.sentinel = Mock(), Mock()
        session.classification = Mock(return_value=None)
        def opened(parent, name, **options):
            if options.get('stage'): return 7
            if name == w.MARKER and state['committed'] is not None: return state['committed']
            raise w.NativeError('absent', w.NOT_FOUND, 2)
        def inspect(handle, directory):
            return (1, bytes([handle]) * 16), len(state['payload'] if handle == 7 else winner)
        winner = json.dumps(dict(schema='dosuti_home_v1', id='b' * 32)).encode()
        api.open, api.close = opened, Mock()
        api.write = lambda handle, payload: state.update(payload=payload)
        api.flush = Mock()
        api.read = lambda handle: state['payload'] if handle == 7 else winner
        session.inspect = inspect
        def rename(handle, home):
            if outcome in ('success', 'ambiguous'): state['committed'] = 7
            if outcome == 'collision': state['committed'] = 9
            if outcome != 'success': raise OSError('injected rename result')
        api.rename = Mock(side_effect=rename)
        return session, state

    def test_success_publishes_own_complete_payload(self):
        session, state = self.session()
        session.publish(False)
        self.assertEqual(state['committed'], 7)
        w.marker_value(state['payload'])
        session.api.rename.assert_called_once_with(7, 2)

    def test_ambiguous_committed_result_is_reconciled(self):
        session, state = self.session('ambiguous')
        session.publish(False)
        self.assertEqual(state['committed'], 7)
        session.api.rename.assert_called_once()

    def test_collision_validates_winner_without_retrying_rename(self):
        session, state = self.session('collision')
        session.publish(False)
        self.assertEqual(state['committed'], 9)
        session.api.rename.assert_called_once()

    def test_rename_failure_without_marker_remains_error(self):
        session, state = self.session('failed')
        with self.assertRaisesRegex(OSError, 'injected'): session.publish(False)
        self.assertIsNone(state['committed'])
        session.api.rename.assert_called_once()

    def test_flush_failure_never_publishes(self):
        session, state = self.session()
        session.api.flush.side_effect = OSError('flush failed')
        with self.assertRaisesRegex(OSError, 'flush'): session.publish(False)
        session.api.rename.assert_not_called()
        self.assertIsNone(state['committed'])

    def test_readback_mismatch_never_publishes(self):
        session, state = self.session()
        session.api.read = lambda handle: b'corrupt readback'
        with self.assertRaisesRegex(ValueError, 'readback'): session.publish(False)
        session.api.rename.assert_not_called()

    def test_invalid_winner_is_not_accepted(self):
        session, state = self.session('collision')
        original = session.api.read
        session.api.read = lambda handle: b'[]' if handle == 9 else original(handle)
        with self.assertRaises(ValueError): session.publish(False)
        self.assertEqual(state['committed'], 9)
        session.api.rename.assert_called_once()


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows x64/NTFS required; skip is not acceptance')
class WindowsManagedHome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'ученик с пробелами' / 'home'

    def initialize(self, **kwargs): return storage.initialize(self.home, **kwargs)
    def marker(self): return self.home / w.MARKER

    def test_native_abi_and_first_repeat_preserve_progress(self):
        w.check_abi()
        self.initialize()
        (self.home / 'progress.json').write_bytes(b'learner answers')
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        self.assertEqual(self.initialize(), self.home)
        self.assertEqual(storage.validate(self.home), self.home)
        self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)

    def test_empty_existing_home_and_bom(self):
        self.home.mkdir(parents=True)
        self.initialize()
        raw = b'\xef\xbb\xbf' + self.marker().read_bytes()
        self.marker().write_bytes(raw)
        self.initialize()
        self.assertEqual(self.marker().read_bytes(), raw)

    def test_bad_marker_and_unknown_entries_preserved(self):
        self.home.mkdir(parents=True)
        for raw in (b'[]', b'null', b'{', b'\xff', b'', b'x' * 4097):
            self.marker().write_bytes(raw)
            with self.subTest(raw=raw[:20]), self.assertRaises((OSError, ValueError)): self.initialize()
            self.assertEqual(self.marker().read_bytes(), raw)
        self.marker().unlink()
        keep = self.home / 'hidden'
        keep.write_bytes(b'unknown')
        subprocess.run(['attrib', '+H', '+S', str(keep)], check=True, capture_output=True)
        with self.assertRaises(ValueError): self.initialize()
        self.assertEqual(keep.read_bytes(), b'unknown')

    def test_full_directory_enumeration_includes_last_entry(self):
        self.home.mkdir(parents=True)
        for number in range(700): (self.home / ('entry-' + str(number))).touch()
        with w.HomeSession(self.home) as session:
            self.assertEqual(len(session.api.entries(session.home)), 700)
        with self.assertRaises(ValueError): self.initialize()

    def test_cleanup_sentinel_and_legacy(self):
        (self.home / 'venv').mkdir(parents=True)
        config = self.home / 'venv/pyvenv.cfg'; config.write_bytes(b'legacy')
        with self.assertRaises(ValueError): self.initialize()
        self.initialize(legacy_runtime=True)
        self.assertEqual(config.read_bytes(), b'legacy')
        sentinel = self.home / '.cleanup-pending.json'; sentinel.mkdir()
        before = self.marker().read_bytes()
        with self.assertRaises(ValueError): self.initialize()
        self.assertEqual(self.marker().read_bytes(), before)

    def test_run_parent_sentinel(self):
        self.home = self.root / 'run'
        (self.root / '.cleanup-pending.json').write_bytes(b'{')
        with self.assertRaises(ValueError): self.initialize()
        self.assertFalse(self.marker().exists())

    def test_hardlinked_marker_refused(self):
        self.initialize()
        os.link(self.marker(), self.root / 'marker-link')
        before = self.marker().read_bytes()
        with self.assertRaises(ValueError): self.initialize()
        self.assertEqual(self.marker().read_bytes(), before)

    def test_junction_parent_does_not_write_redirect(self):
        foreign = self.root / 'foreign'; foreign.mkdir()
        (foreign / 'keep').write_bytes(b'foreign')
        alias = self.root / 'junction'
        subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(alias), str(foreign)],
                       check=True, capture_output=True)
        self.home = alias / 'home'
        with self.assertRaises((ValueError, OSError)): self.initialize()
        self.assertEqual(list(foreign.iterdir()), [foreign / 'keep'])

    def test_symlink_marker_refused(self):
        self.home.mkdir(parents=True)
        foreign = self.root / 'foreign'; foreign.write_bytes(b'keep')
        # Privilege failure is a fixture ERROR, not a green security skip.
        self.marker().symlink_to(foreign)
        with self.assertRaises((ValueError, OSError)): self.initialize()
        self.assertEqual(foreign.read_bytes(), b'keep')

    def test_preopened_delete_and_write_marker_handles(self):
        self.initialize()
        api = w.Native()
        for access in (0x10000, 0x40000000):
            h = api.create_root(str(self.marker()), access, 7, None, 3, 0x200000, None)
            if h == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
            try:
                with patch.object(w.time, 'monotonic', side_effect=[0, 31]):
                    with self.assertRaises((ValueError, OSError)): self.initialize()
            finally: api.close(h)
        self.initialize()  # Sharing released, no leaked handle.

    def test_preopened_metadata_handle_does_not_bypass_identity_checks(self):
        self.initialize()
        api = w.Native()
        h = api.create_root(str(self.home), 0x100, 7, None, 3, 0x02200000, None)
        if h == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
        before = self.marker().read_bytes()
        try: self.initialize()
        finally: api.close(h)
        self.assertEqual(self.marker().read_bytes(), before)

    def test_preopened_delete_directory_handle_is_refused(self):
        self.home.mkdir(parents=True)
        api = w.Native()
        h = api.create_root(str(self.home), 0x10000, 7, None, 3, 0x02200000, None)
        if h == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
        try:
            with self.assertRaises((OSError, ValueError)): self.initialize()
        finally: api.close(h)
        self.initialize()

    def test_flush_and_readback_failure_retain_stage_and_retry(self):
        for fault in ('flush', 'read'):
            with self.subTest(fault=fault):
                self.home = self.root / fault
                with patch.object(w.Native, fault, side_effect=OSError('injected ' + fault)):
                    with self.assertRaises(OSError): self.initialize()
                self.assertFalse(self.marker().exists())
                self.initialize()

    def test_native_zero_write_refuses_then_retries(self):
        original = w.Native.write
        def zero(api, handle, payload):
            with patch.object(api, 'write_file', return_value=1): original(api, handle, payload)
        with patch.object(w.Native, 'write', zero):
            with self.assertRaises(OSError): self.initialize()
        self.assertFalse(self.marker().exists())
        self.initialize()

    def test_readback_mismatch_refuses_publication(self):
        with patch.object(w.Native, 'read', return_value=b'wrong bytes'):
            with self.assertRaises(ValueError): self.initialize()
        self.assertFalse(self.marker().exists())
        self.initialize()

    def test_rename_error_without_commit_then_retry(self):
        with patch.object(w.Native, 'rename', side_effect=OSError('rename failed')):
            with self.assertRaises(OSError): self.initialize()
        self.assertFalse(self.marker().exists())
        self.initialize()

    def test_ambiguous_rename_reconciles_own_commit(self):
        original = w.Native.rename
        def committed(api, handle, home):
            original(api, handle, home)
            raise OSError('injected ambiguous result')
        with patch.object(w.Native, 'rename', committed): self.initialize()
        before = self.marker().read_bytes()
        self.initialize()
        self.assertEqual(self.marker().read_bytes(), before)

    def test_valid_and_invalid_winner_at_rename(self):
        for raw in (json.dumps(dict(schema='dosuti_home_v1', id='b'*32)).encode(), b'[]'):
            self.home = self.root / ('valid' if raw != b'[]' else 'invalid')
            original = w.Native.rename
            def competitor(api, stage, home):
                self.marker().write_bytes(raw)
                return original(api, stage, home)
            with patch.object(w.Native, 'rename', competitor):
                if raw == b'[]':
                    with self.assertRaises(ValueError): self.initialize()
                else: self.initialize()
            self.assertEqual(self.marker().read_bytes(), raw)

    def test_directory_swap_at_rename_is_blocked_or_refused(self):
        original = w.Native.rename
        foreign = self.root / 'foreign'; foreign.mkdir()
        (foreign / 'keep').write_bytes(b'foreign')
        attempts = []
        def competitor(api, stage, home):
            attempts.append((stage, home))
            try: self.home.rename(self.root / 'moved-home')
            except OSError as error:
                self.assertIn(error.winerror, (5, 32), 'Expected NTFS access/sharing denial of the attack')
            else: foreign.rename(self.home)
            return original(api, stage, home)
        with patch.object(w.Native, 'rename', competitor):
            try: self.initialize()
            except (OSError, ValueError): pass
        self.assertEqual(len(attempts), 1, 'competitor must run at Native.rename; early refusal is not coverage')
        victim = foreign if foreign.exists() else self.home
        self.assertEqual((victim / 'keep').read_bytes(), b'foreign')
        self.assertFalse((victim / w.MARKER).exists())

    def test_interrupt_after_flush_preserves_orphan_and_retry(self):
        original = w.Native.flush
        def interrupted(api, stage):
            original(api, stage)
            raise KeyboardInterrupt()
        with patch.object(w.Native, 'flush', interrupted):
            with self.assertRaises(KeyboardInterrupt): self.initialize()
        retained = {p: p.read_bytes() for p in self.home.parent.glob('.dosuti-home-*')}
        self.assertTrue(retained)
        self.initialize()
        self.assertEqual({p: p.read_bytes() for p in retained}, retained)

    def test_parent_swap_at_native_create_preserves_redirect(self):
        foreign = self.root / 'redirect'; foreign.mkdir()
        (foreign / 'keep').write_bytes(b'foreign')
        original = w.Native.open
        attempted = []
        def swapping(api, parent, name, **options):
            if options.get('stage') and not attempted:
                attempted.append(True)
                try: self.home.parent.rename(self.root / 'owned-parent')
                except OSError: pass
                else: foreign.rename(self.home.parent)
            return original(api, parent, name, **options)
        with patch.object(w.Native, 'open', swapping):
            try: self.initialize()
            except (OSError, ValueError): pass
        self.assertTrue(attempted)
        victim = foreign if foreign.exists() else self.home.parent
        self.assertEqual((victim / 'keep').read_bytes(), b'foreign')
        self.assertFalse((victim / 'home' / w.MARKER).exists())

    def test_unsupported_information_class_refuses(self):
        original = w.Native.details
        def unavailable(api, handle, directory):
            raise OSError('unsupported information class')
        with patch.object(w.Native, 'details', unavailable):
            with self.assertRaises(OSError): self.initialize()
        self.assertFalse(self.marker().exists())
        self.initialize()

    def test_terminated_stage_writer_then_retry(self):
        code = r"""import sys
from pathlib import Path
import windows_managed_home as w
original=w.Native.flush
def gate(self,stage):
 original(self,stage)
 print('ready',flush=True)
 sys.stdin.readline()
w.Native.flush=gate
w.initialize(Path(sys.argv[1]))
"""
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.home)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, PYTHONPATH=str(Path(w.__file__).parent)))
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            process.kill(); process.communicate(timeout=10)
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr): stream.close()
        retained = {p: p.read_bytes() for p in self.home.parent.glob('.dosuti-home-*')}
        self.assertTrue(retained)
        self.initialize()
        self.assertEqual({p: p.read_bytes() for p in retained}, retained)

    def test_handle_count_does_not_grow_on_repeated_initialization(self):
        api = w.Native()
        get_count = api.k.GetProcessHandleCount
        get_count.argtypes, get_count.restype = [w.HANDLE, c.POINTER(w.U32)], w.I32
        def count():
            value = w.U32()
            api.boolean(get_count(w.HANDLE(-1), c.byref(value)))
            return value.value
        self.initialize()
        before = count()
        for _ in range(20): self.initialize()
        self.assertLessEqual(count(), before + 2)


if __name__ == '__main__': unittest.main()
