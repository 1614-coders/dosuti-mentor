"""Instrumentation self-checks only. These do NOT execute Windows kernel APIs."""
import copy
import ctypes as c
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagnose_windows_home_rename as d


def initial(existing=False, tag='own'):
    def entry(raw, name): return dict(identity=[1, tag + name], size=len(raw), bytes_hex=raw.hex())
    files = {d.STAGE: entry(d.PAYLOAD, 'stage'), 'home/' + d.PROGRESS: entry(d.PROGRESS_BYTES, 'progress')}
    names = [d.PROGRESS]
    if existing:
        files['home/' + d.w.MARKER] = entry(d.EXISTING, 'marker')
        names.append(d.w.MARKER)
    return dict(parent_entries=[d.STAGE, 'fixture home'], home_entries=sorted(names), files=files)


def observation(kind, status):
    if kind == 'win32': return dict(api=kind, information_class=3, **{'return': int(status == 0)}, winerror=status)
    return dict(api=kind, information_class=10, returned_status=status)


def successful_after(before):
    after = copy.deepcopy(before)
    after['parent_entries'].remove(d.STAGE)
    after['home_entries'] = sorted(after['home_entries'] + [d.w.MARKER])
    after['files']['home/' + d.w.MARKER] = after['files'].pop(d.STAGE)
    return after


def records():
    rows = []
    for index, (kind, length, existing) in enumerate((k, n, e) for k, n in [('win32', 56), ('native', 56), ('native', 58)] for e in (False, True)):
        before = initial(existing, str(index))
        call = observation(kind, (183 if kind == 'win32' else d.w.COLLISION) if existing else 0)
        after = copy.deepcopy(before) if existing else successful_after(before)
        rows.append(dict(api=kind, buffer_size=length, variant=d.variant(kind, length), preexisting_marker=existing,
                         fixture_root='fixture-' + str(index), environment=dict(filesystem='NTFS', pointer_size=8, windows='model', python='model', volume_identity=1, volume_mapping=['model', 'model']), parameters=d.parameters(d.build_buffer(100 + index, length), 100 + index),
                         before=before, after=after, call=call, checks=d.check_effects(before, after, call, existing),
                         diagnostic_complete=True, handles_released=True))
    return rows


class DiagnosticLogic(unittest.TestCase):
    def test_56_58_layout_and_handle_role_normalization(self):
        a, b = d.parameters(d.build_buffer(123, 56), 123), d.parameters(d.build_buffer(456, 58), 456)
        self.assertEqual(a['fields'], b['fields'])
        self.assertEqual(a['fields']['sizeof'], 24)
        self.assertEqual(a['fields']['name_offset'], 20)
        self.assertEqual(a['fields']['filename_length'], 34)
        self.assertEqual(a['native_size_reference']['documented_minimum'], 58)
        self.assertEqual(a['native_size_reference']['relation'], 'below')
        self.assertEqual(b['native_size_reference']['relation'], 'meets')
        self.assertTrue(a['native_size_reference']['acceptance_is_not_documented_contract_guarantee'])
        self.assertEqual(len(bytes.fromhex(a['buffer_hex'])), 56)
        self.assertEqual(len(bytes.fromhex(b['buffer_hex'])), 58)
        self.assertEqual(bytes.fromhex(a['normalized_buffer_hex']), bytes.fromhex(b['normalized_buffer_hex'])[:56])
        self.assertEqual(bytes.fromhex(b['normalized_buffer_hex'])[56:], b'\0\0')
        with self.assertRaises(ValueError): d.build_buffer(123, 54)

    def test_immediate_status_overrides_untouched_iosb(self):
        result = observation('native', 0xC000000D)
        result['ios_status_observed'] = 0
        self.assertEqual(d.outcome(result), 'other_failure')
        result = observation('native', 0)
        result['ios_status_observed'] = 0xDEADBEEF
        self.assertEqual(d.outcome(result), 'success')

    def test_pending_requires_confirmed_completion(self):
        result = observation('native', d.PENDING)
        with self.assertRaises(ValueError): d.outcome(result)
        result.update(completion_confirmed=True, completion_status=d.w.COLLISION)
        self.assertEqual(d.outcome(result), 'collision')

    def test_other_error_does_not_become_collision_in_negative_control(self):
        before = initial(True)
        for kind, status in [('win32', 87), ('win32', 5), ('win32', 32), ('native', 0xC0000022)]:
            with self.subTest(kind=kind, status=status):
                checked = d.check_effects(before, copy.deepcopy(before), observation(kind, status), True)
                self.assertTrue(checked['existing_marker_unchanged'])
                self.assertFalse(checked['no_replace_verified'])
                self.assertEqual(checked['no_replace_control'], 'inconclusive_failure')
                self.assertFalse(checked['collision_observed'])
        for kind, status in [('win32', 183), ('native', d.w.COLLISION)]:
            self.assertTrue(d.check_effects(before, copy.deepcopy(before), observation(kind, status), True)['collision_observed'])

    def test_side_effects_on_failed_call_are_rejected(self):
        before = initial(True)
        for change in ('progress', 'marker', 'identity', 'stage', 'unknown'):
            after = copy.deepcopy(before)
            if change == 'progress': after['files']['home/' + d.PROGRESS]['bytes_hex'] = 'ff'
            elif change == 'marker': after['files']['home/' + d.w.MARKER]['bytes_hex'] = 'ff'
            elif change == 'identity': after['files']['home/' + d.w.MARKER]['identity'] = [1, 'other']
            elif change == 'stage': after['files'].pop(d.STAGE)
            else: after['files']['unknown'] = after['files'][d.STAGE]
            with self.subTest(change=change), self.assertRaises(ValueError):
                d.check_effects(before, after, observation('win32', 87), True)

    def test_only_expected_successful_move_is_allowed(self):
        before = initial()
        self.assertTrue(d.check_effects(before, successful_after(before), observation('native', 0), False)['effects_verified'])
        with self.assertRaises(ValueError): d.check_effects(before, before, observation('native', 0), False)
        before = initial(True)
        with self.assertRaises(ValueError): d.check_effects(before, before, observation('native', 0), True)

    def test_comparison_rejects_missing_reused_or_incomplete_evidence(self):
        good = records()
        result = d.compare(good)
        self.assertTrue(result['semantic_fields_equal'])
        self.assertFalse(result['windows_p1_fixed'])
        native = [r for r in result['observations'] if r['api'] == 'native']
        for row in native:
            self.assertEqual(row['variant']['native_documented_minimum'], 58)
            self.assertEqual(row['variant']['meets_native_documented_size'], row['size'] == 58)
            self.assertIn('does not establish conformance', row['variant']['caveat'])
        bad_label = copy.deepcopy(good); bad_label[2].pop('variant')
        with self.assertRaisesRegex(ValueError, 'size-contract label'): d.compare(bad_label)
        cases = [good[:-1]]
        for field, value in [('fixture_root', good[0]['fixture_root']), ('handles_released', False),
                             ('diagnostic_complete', False)]:
            bad = copy.deepcopy(good); bad[1][field] = value; cases.append(bad)
        bad = copy.deepcopy(good); bad[0]['before']['files'].pop('home/' + d.PROGRESS); cases.append(bad)
        bad = copy.deepcopy(good); bad[0]['parameters']['buffer_hex'] = '00'; cases.append(bad)
        for bad in cases:
            with self.assertRaises((ValueError, KeyError)): d.compare(bad)

    def test_separate_experiments_create_separate_temporary_directories(self):
        # Verify actual fixture allocation/cleanup without pretending to run a
        # native call: deliberately stop at the HomeSession preparation boundary.
        roots = []
        def stop(path, **kw):
            roots.append(Path(path).parent)
            raise OSError('fixture preparation unavailable')
        with patch.object(d.w, 'HomeSession', side_effect=stop):
            a, b = d.experiment('win32', 56, False), d.experiment('native', 56, False)
        self.assertNotEqual(roots[0], roots[1])
        self.assertTrue(all(not root.exists() for root in roots))
        self.assertFalse(a['diagnostic_complete']); self.assertFalse(b['diagnostic_complete'])
        self.assertEqual(a['phase'], 'preparation')
        self.assertNotIn('call', a)

    def test_native_error_status_is_captured_without_using_iosb_as_success(self):
        def native_call(stage, ios, buffer, length, kind):
            self.assertEqual(kind, 10)
            return d.w.I32(0xC000000D).value
        call = Mock(side_effect=native_call)
        api = SimpleNamespace(n=SimpleNamespace(NtSetInformationFile=call), to_dos=lambda value: 87)
        result = d.run_call(api, 123, 456, d.build_buffer(456, 56), 'native')
        self.assertEqual(result['returned_status'], 0xC000000D)
        self.assertEqual(d.outcome(result), 'other_failure')
        self.assertEqual(result['mapped_error'], 87)

    def test_pending_wait_failure_does_not_unwind_live_buffers_or_pass(self):
        call = Mock(return_value=d.PENDING)
        api = SimpleNamespace(n=SimpleNamespace(NtSetInformationFile=call),
                              k=SimpleNamespace(WaitForSingleObject=Mock(return_value=258)))
        with patch.object(d, 'emit'), patch.object(d.os, '_exit', side_effect=SystemExit(3)) as terminate:
            with self.assertRaises(SystemExit): d.run_call(api, 123, 456, d.build_buffer(456, 56), 'native')
        terminate.assert_called_once_with(3)

    def test_api_result_survives_failed_post_call_snapshot(self):
        for kind, length, call in [('win32', 56, observation('win32', 87)),
                                   ('native', 56, observation('native', 0xC000000D)),
                                   ('native', 58, observation('native', 0))]:
            with self.subTest(kind=kind, length=length):
                session = MagicMock()
                session.__enter__.return_value = session
                session.classification.return_value = None
                session.root, session.home, session.parent = 456, 123, 789
                session.own.return_value = 7
                session.mapping = ('device', 'volume')
                session.inspect.return_value = ((1, b'x' * 16), len(d.PAYLOAD))
                session.api.read.return_value = d.PROGRESS_BYTES
                def volume(*args):
                    args[6].value = 'NTFS'
                    return 1
                session.api.volume_info.side_effect = volume
                emitted = []
                with patch.object(d.w, 'HomeSession', return_value=session), \
                        patch.object(d, 'run_call', return_value=call) as invoke, \
                        patch.object(d, 'snapshot', side_effect=[initial(), OSError('post-call snapshot failed')]) as snap:
                    def capture(value):
                        if value.get('event') == 'call_result':
                            self.assertEqual(snap.call_count, 1, 'API result must be emitted BEFORE the final snapshot')
                        emitted.append(json.loads(json.dumps(value)))
                    with patch.object(d, 'emit', side_effect=capture):
                        result = d.experiment(kind, length, False)
                invoke.assert_called_once()
                self.assertEqual(snap.call_count, 2)
                self.assertEqual(result.get('call'), call)
                self.assertEqual(result['phase'], 'verification')
                self.assertFalse(result['diagnostic_complete'])
                self.assertEqual(result['error'], 'post-call snapshot failed')
                self.assertNotIn('after', result)
                evidence = [v for v in emitted if v.get('event') == 'call_result']
                self.assertEqual(len(evidence), 1)
                self.assertEqual(evidence[0]['call'], call)
                self.assertEqual(evidence[0]['phase'], 'verification')
                self.assertEqual(evidence[0]['variant'], d.variant(kind, length))


if __name__ == '__main__': unittest.main()
