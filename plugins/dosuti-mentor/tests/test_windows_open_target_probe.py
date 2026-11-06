"""Probe instrumentation models only; no Windows kernel claim on any platform."""
import copy
import ctypes as c
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_windows_open_target as probe


class OpenTargetProbeModels(unittest.TestCase):
    def test_x64_buffer_for_both_information_classes(self):
        for flags in (1, 3):
            value = probe.buffer_for(456, flags)
            header = probe.n.FILE_RENAME_INFO.from_buffer(value)
            self.assertEqual(c.sizeof(probe.n.FILE_RENAME_INFO), 24)
            self.assertEqual(len(value), 46)
            self.assertEqual((header.Flags, header.RootDirectory, header.FileNameLength), (flags, 456, 22))
            self.assertEqual(value.raw[20:42], 'lesson.json'.encode('utf-16-le'))
            self.assertEqual(value.raw[42:], b'\0' * 4)

    def test_one_call_pending_retains_buffer_until_completed(self):
        for kind, flags in ((10, 1), (65, 3)):
            active, rows = {}, []
            def submit(handle, ios, value, size, information_class):
                active.update(ios=ios, value=value, bytes=value.raw, args=(handle, size, information_class))
                return probe.PENDING
            def wait(*args):
                active['wait_args'] = args
                active['observed'] = active['value'].raw
                c.cast(active['ios'], c.POINTER(probe.n.IO_STATUS_BLOCK)).contents.Result.Status = 0
                return 0
            api = Mock(rename_info=Mock(side_effect=submit), wait_io=Mock(side_effect=wait))
            with patch.object(probe, 'emit', rows.append):
                result = probe.rename_once(api, 123, 456, kind, flags)
            self.assertEqual(active['args'], (123, 46, kind))
            self.assertEqual(active['observed'], active['bytes'])
            self.assertEqual(active['wait_args'], (123, False, None))
            self.assertEqual(result['completion_status'], 0)
            self.assertEqual([row['phase'] for row in rows], ['api_return', 'api_completed'])
            self.assertEqual(rows[0]['call']['ntstatus'], probe.PENDING)
            api.rename_info.assert_called_once(); api.wait_io.assert_called_once()

    def test_terminal_status_is_authoritative_even_with_untouched_iosb(self):
        rows = []
        for status in (0, 0xC0000022, 0xC0000003):
            api = Mock(rename_info=Mock(return_value=probe.n.I32(status).value))
            with patch.object(probe, 'emit', rows.append):
                result = probe.rename_once(api, 123, 456, 65, 3)
            self.assertEqual(result['completion_status'], status)
            self.assertEqual(result['iosb_status'], probe.UNTOUCHED)
            api.rename_info.assert_called_once(); api.wait_io.assert_not_called()

    def test_reader_open_masks_root_and_independent_handle(self):
        captured = {}
        def create(handle, access, oa, ios, allocation, attrs, share, disposition, options, ea, length):
            captured.update(access=access, share=share, disposition=disposition, options=options,
                parent=c.cast(oa, c.POINTER(probe.n.OBJECT_ATTRIBUTES)).contents.RootDirectory)
            c.cast(handle, c.POINTER(probe.n.HANDLE))[0] = 789
            c.cast(ios, c.POINTER(probe.n.IO_STATUS_BLOCK)).contents.Result.Status = 0
            c.cast(ios, c.POINTER(probe.n.IO_STATUS_BLOCK)).contents.Information = 1
            return 0
        api = Mock(create=Mock(side_effect=create))
        metadata = dict(attributes=0x80, directory=False, file_type=1, links=1)
        with patch.object(probe, 'describe', return_value=metadata), patch.object(probe, 'emit'):
            with probe.reader_handle(api, 456, 'lesson.json') as handle:
                self.assertEqual(handle, 789)
                api.close.assert_not_called()
        self.assertEqual(captured, dict(access=probe.READER_ACCESS, share=7, disposition=1,
                                      options=0x200000 | 0x20 | 0x40, parent=456))
        self.assertEqual(captured['access'], 0x80000000 | probe.n.SYNC)
        self.assertEqual(captured['access'] & probe.n.SYNC, probe.n.SYNC)
        api.create.assert_called_once(); api.close.assert_called_once_with(789)

    def test_reader_failure_records_ntstatus_and_iosb_before_exception(self):
        for writes_iosb in (False, True):
            with self.subTest(writes_iosb=writes_iosb):
                api = object.__new__(probe.n.Native)
                api.to_dos = lambda status: 87
                api.close = Mock()
                events = []
                def create(handle, access, oa, ios, *args):
                    if writes_iosb:
                        block = c.cast(ios, c.POINTER(probe.n.IO_STATUS_BLOCK)).contents
                        block.Result.Status = probe.n.I32(0xC000000D).value
                        block.Information = 17
                    return probe.n.I32(0xC000000D).value
                def status(result, operation):
                    events.append(('status', result))
                    probe.n.Native.status(api, result, operation)
                api.create, api.status = Mock(side_effect=create), status
                with patch.object(probe, 'emit', lambda row: events.append(('emit', row))), \
                        patch.object(probe, 'describe') as describe:
                    with self.assertRaises(probe.n.NativeError) as caught:
                        probe.open_reader(api, 456, 'lesson.json')
                self.assertTrue(events and events[0][0] == 'emit', 'Record API result before raising status error')
                row = events[0][1]
                self.assertEqual(row['phase'], 'reader_open_return')
                self.assertEqual(row['call']['ntstatus'], 0xC000000D)
                self.assertEqual(row['call']['iosb_status'], 0xC000000D if writes_iosb else probe.UNTOUCHED)
                self.assertEqual(row['call']['iosb_information'], 17 if writes_iosb else 0)
                self.assertEqual(caught.exception.status, 0xC000000D)
                api.create.assert_called_once(); api.close.assert_not_called(); describe.assert_not_called()

    def test_failed_pending_wait_is_not_completed_or_freed_as_normal_return(self):
        class ProcessExit(BaseException): pass
        rows = []
        api = Mock(rename_info=Mock(return_value=probe.PENDING), wait_io=Mock(return_value=-1))
        with patch.object(probe, 'emit', rows.append), patch.object(probe.os, '_exit', side_effect=ProcessExit) as terminate:
            with self.assertRaises(ProcessExit): probe.rename_once(api, 123, 456, 65, 3)
        terminate.assert_called_once_with(3)
        self.assertEqual(rows[0]['phase'], 'api_return')
        self.assertFalse(rows[-1]['diagnostic_complete'])
        api.rename_info.assert_called_once()

    def test_old_reader_and_new_binding_are_both_verified(self):
        before = dict(source={'identity': [1, 'new']}, target={'identity': [1, 'old']}, foreign={'bytes': 'same'})
        after = dict(target=before['source'], target_bytes=probe.NEW.hex(), source_bytes=probe.NEW.hex(),
                     foreign=before['foreign'], entries=['foreign.bin', 'lesson.json'],
                     reader=before['target'], reader_bytes=probe.OLD.hex())
        probe.check_effects(before, after, {'completion_status': 0}, True)
        for key in ('reader_bytes', 'target_bytes', 'foreign'):
            bad = copy.deepcopy(after); bad[key] = 'changed'
            with self.assertRaises(AssertionError): probe.check_effects(before, bad, {'completion_status': 0}, True)

    def test_successful_class10_open_target_refutes_hypothesis(self):
        def rows(statuses):
            return [dict(diagnostic_complete=True, call={'completion_status': status}) for status in statuses]
        self.assertIn('refuted', probe.interpretation(rows([0, 0, 0])))
        self.assertIn('consistent', probe.interpretation(rows([0, 0xC0000043, 0])))
        self.assertIn('inconclusive', probe.interpretation(rows([0, 0xC0000043, 0xC0000003])))
        self.assertEqual(probe.interpretation([]), 'incomplete')

    def test_non_windows_refuses_before_any_native_call(self):
        with patch.object(probe.os, 'name', 'posix'), patch.object(sys, 'argv', ['probe']), \
                patch.object(probe.n, 'Native') as native, patch.object(sys, 'stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as error: probe.main()
        self.assertEqual(error.exception.code, 2)
        native.assert_not_called()


if __name__ == '__main__': unittest.main()
