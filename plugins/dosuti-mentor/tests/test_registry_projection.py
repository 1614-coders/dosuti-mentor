"""Discard old unit objects only after verifying bytes and every historical record."""
import copy
import itertools
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from json_projection import visit_array, visit_array_chunks
import document_store as ds
import study_units as su
import unit_learning as ul
import test_unit_learning


class Projection(unittest.TestCase):
    def test_orders_nested_strings_whitespace_and_unicode(self):
        original = dict(schema='qa', history=[{'text': 'экраны 🧠 "]},\\\n', 'items': [1, None, True]}, []], value={'nested': ['a', {}]})
        for keys in itertools.permutations(original):
            text = '\n ' + json.dumps({k: original[k] for k in keys}, ensure_ascii=False, indent=2) + '\r\n'
            seen = []
            result, count = visit_array(text, 'history', seen.append)
            self.assertEqual(count, 2)
            self.assertEqual(seen, original['history'])
            self.assertEqual(result, original | dict(history=[]))

    def test_empty_and_missing_array(self):
        for text in ('{}', '{"history":[]}'):
            result, count = visit_array(text, 'history', lambda _: self.fail('Unexpected item'))
            self.assertEqual(count, 0)
            self.assertEqual(result, json.loads(text))

    def test_malformed_json_is_not_skipped(self):
        for text in ('{"history":[1,]}', '{"history":[1] ,}', '{"history":[1] "x":1}',
                     '{"history":null}', '{"history":{}}', '{"history":[}',
                     '{"history":[]} true', '[]', '{"history":[],"history":[]}',
                     '{"x":1,"x":2}', '{"history":[1 2]}', '{"history":[]}\v'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                visit_array(text, 'history', lambda _: None)

    def test_visitor_failure_not_swallowed(self):
        def visitor(_): raise ValueError('historical corruption')
        with self.assertRaisesRegex(ValueError, 'historical corruption'):
            visit_array('{"history":[{}]}', 'history', visitor)

    def test_all_small_chunk_boundaries_preserve_numbers_and_escapes(self):
        value = dict(history=[1e30, -12.00034, 1234567890, True, None,
                              {'text': '漢字🧠\\"\n', 'nested': [[], {}]}], other='tail')
        text = json.dumps(value, ensure_ascii=False)
        for size in range(1, len(text) + 1):
            seen = []
            result, count = visit_array_chunks((text[n:n+size] for n in range(0, len(text), size)), 'history', seen.append)
            self.assertEqual(seen, value['history'])
            self.assertEqual(result, value | dict(history=[]))
            self.assertEqual(count, len(seen))

    def test_invalid_token_prefix_at_chunk_boundary_is_rejected(self):
        for chunks in (['{"history":[1e', ']}'], ['{"history":[tru', 'eX]}'],
                       ['{"history":[1', '2 3]}'], ['{"history":[]} ', 'junk']):
            with self.assertRaises(ValueError): visit_array_chunks(chunks, 'history', lambda _: None)


class RegistryProjection(unittest.TestCase):
    def setUp(self):
        self.f = test_unit_learning.UnitLearning()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.root = self.f.f.f.path
        self.path = self.root / 'study-units.json'

    def fixture(self, segmented=True):
        data = su.load(self.root)
        row = copy.deepcopy(data['units']['maps'])
        data['history'] = [dict(unit='maps', record=row, at='2026-01-01T00:00:00Z') for _ in range(8)]
        data['revision'] = 9
        with patch.object(ds, 'INLINE_BYTES', 1 if segmented else ds.INLINE_BYTES):
            ds.write(self.path, data, 'study_units')
        return data

    def fingerprint(self):
        import hashlib
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file()}

    def test_inline_and_segmented_match_current_data_without_writes(self):
        for segmented in (False, True):
            expected = self.fixture(segmented)
            before = self.fingerprint()
            result = su.load(self.root, include_history=False)
            self.assertEqual(result, expected | dict(history=[]))
            self.assertEqual(su.load(self.root), expected)
            self.assertEqual(before, self.fingerprint())

    def test_all_old_records_still_validated(self):
        self.fixture()
        original = su.validate_record
        calls = []
        def validate(row):
            calls.append(row['id'])
            return original(row)
        with patch.object(su, 'validate_record', side_effect=validate):
            su.load(self.root, include_history=False)
        self.assertEqual(calls, ['maps'] * 9)

    def test_multibyte_utf8_part_boundaries_are_decoded_exactly(self):
        data = self.fixture()
        # A non-ASCII value in every history item crosses byte boundaries.
        for event in data['history']: event['at'] = '漢🧠я' * 7
        with patch.object(ds, 'INLINE_BYTES', 1), patch.object(ds, 'PART_BYTES', 101):
            ds.write(self.path, data, 'study_units')
        self.assertEqual(su.load(self.root, include_history=False), data | dict(history=[]))

    def test_bad_historical_record_blocks_projected_read(self):
        data = self.fixture()
        data['history'][-1]['record'] = copy.deepcopy(data['history'][-1]['record'])
        data['history'][-1]['record']['title'] = 'Changed without semantic hash'
        ds.write(self.path, data, 'study_units')
        before = self.fingerprint()
        for retain in (False, True):
            with self.assertRaises(ValueError): su.load(self.root, include_history=retain)
        self.assertEqual(before, self.fingerprint())

    def test_part_damage_blocks_projection_before_visitor(self):
        self.fixture()
        manifest = json.loads(self.path.read_text())
        p = ds.folder(self.path) / (manifest['parts'][-1]['sha256'] + '.part')
        raw = p.read_bytes()
        p.write_bytes(b'!' + raw[1:])
        with patch.object(su, 'validate_history', side_effect=AssertionError('Projection before integrity')):
            with self.assertRaisesRegex(ValueError, 'checksum'):
                su.load(self.root, include_history=False)

    def test_global_revision_and_graph_validation_remain(self):
        data = self.fixture()
        data['revision'] += 1
        ds.write(self.path, data, 'study_units')
        with self.assertRaisesRegex(ValueError, 'registry'): su.load(self.root, include_history=False)
        data['revision'] -= 1
        row = data['units']['maps']
        row['prerequisites'] = ['absent']
        row['prerequisite_hashes'] = {'absent': 'a' * 64}
        row['content_sha256'] = su.digest(su.semantic(row))
        ds.write(self.path, data, 'study_units')
        with self.assertRaisesRegex(ValueError, 'Missing prerequisite'): su.load(self.root, include_history=False)

    def test_learning_uses_projection_and_keeps_checkpoint_and_assessment(self):
        self.fixture()
        self.f.save()
        self.f.save(dict(recap='Maps', next_step='Practice', pending_question='Why?'), checkpoint=True)
        original = su.load
        def projected(root, **kwargs):
            self.assertIs(kwargs.get('include_history'), False)
            result = original(root, **kwargs)
            self.assertEqual(result['history'], [])
            return result
        before = self.fingerprint()
        with patch.object(su, 'load', side_effect=projected):
            result = self.f.read(session='a')['learning']
        self.assertTrue(result['checkpoint']['valid'])
        self.assertTrue(result['all_criteria_demonstrated'])
        self.assertEqual(result['attempt_count'], 1)
        self.assertEqual(before, self.fingerprint())

    def test_changed_source_still_invalidates_continuation(self):
        self.fixture()
        self.f.save(dict(recap='Maps', next_step='Practice', pending_question=None), checkpoint=True)
        row = su.load(self.root)['units']['maps']
        source = self.root / row['sources'][0]['snapshot']
        source.chmod(0o600)
        source.write_text('changed source')
        result = self.f.read(session='a')
        self.assertTrue(result['unit']['stale'])
        self.assertFalse(result['learning']['checkpoint']['valid'])
        self.assertEqual(result['unit']['source_contexts'], [])
