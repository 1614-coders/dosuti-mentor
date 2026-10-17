import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import document_report as report
import document_store as ds
from atomic_files import atomic_json


class DocumentReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / 'state.json'

    def write(self, text):
        with patch.object(ds, 'INLINE_BYTES', 1):
            ds.write(self.path, dict(schema='qa', text=text), 'qa')

    def fingerprint(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def test_old_and_new_parts_are_counted_without_writes(self):
        self.write('a' * 300000)
        old = {p.name for p in ds.folder(self.path).iterdir()}
        self.write('b' * 300000)
        current = json.loads(self.path.read_text())
        names = {p['sha256'] + '.part' for p in current['parts']}
        before = self.fingerprint()
        result = report.inventory(self.path, 'qa')
        self.assertEqual(result['totals']['unreferenced_files'], len(old - names))
        self.assertEqual(result['totals']['referenced_files'], len(names))
        self.assertTrue(result['current_bytes_verified'])
        self.assertTrue(result['observed_unchanged'])
        self.assertIsNone(result['reclaimable_bytes'])
        self.assertFalse(result['deletion_supported'])
        self.assertEqual(before, self.fingerprint())

    def test_legacy_inline_does_not_create_objects_or_locks(self):
        atomic_json(self.path, dict(schema='qa', text='old'))
        before = self.fingerprint()
        result = report.inventory(self.path, 'qa')
        self.assertEqual(result['format'], 'inline')
        self.assertFalse(result['current_bytes_verified'])
        self.assertFalse(ds.folder(self.path).exists())
        self.assertEqual(before, self.fingerprint())

    def test_parts_left_after_inline_conversion_are_only_unreferenced(self):
        self.write('a' * 300000)
        atomic_json(self.path, dict(schema='qa'))
        result = report.inventory(self.path, 'qa')
        self.assertGreater(result['totals']['unreferenced_bytes'], 0)
        self.assertEqual(result['totals']['referenced_bytes'], 0)

    def test_corruption_and_wrong_kind_are_refused(self):
        self.write('a' * 300000)
        with self.assertRaises(ValueError): report.inventory(self.path, 'wrong')
        item = json.loads(self.path.read_text())['parts'][0]
        (ds.folder(self.path) / (item['sha256'] + '.part')).write_bytes(b'x')
        before = self.fingerprint()
        with self.assertRaises(ValueError): report.inventory(self.path, 'qa')
        self.assertEqual(before, self.fingerprint())

    def test_changed_aggregate_hash_and_bad_total_are_refused(self):
        self.write('a' * 300000)
        value = json.loads(self.path.read_text())
        value['sha256'] = '0' * 64
        atomic_json(self.path, value)
        with self.assertRaises(ValueError): report.inventory(self.path, 'qa')
        value['bytes'] += 1
        atomic_json(self.path, value)
        with self.assertRaises(ValueError): report.inventory(self.path, 'qa')

    def test_unknown_symlink_hardlink_and_directory_are_excluded(self):
        atomic_json(self.path, dict(schema='qa'))
        folder = ds.folder(self.path)
        folder.mkdir()
        (folder / 'notes').write_bytes(b'x')
        (folder / ('a' * 64 + '.part')).symlink_to(self.path)
        os.link(self.path, folder / ('b' * 64 + '.part'))
        (folder / ('c' * 64 + '.part')).mkdir()
        # Manifest itself cannot be hardlinked either.
        with self.assertRaises(ValueError): report.inventory(self.path, 'qa')
        (folder / ('b' * 64 + '.part')).unlink()
        outside = self.root / 'outside'
        outside.write_bytes(b'x')
        os.link(outside, folder / ('b' * 64 + '.part'))
        result = report.inventory(self.path, 'qa')
        self.assertEqual(result['totals']['unknown_entries'], 4)
        self.assertEqual(result['totals']['unreferenced_bytes'], 0)

    def test_symlink_ancestor_or_objects_folder_refused(self):
        atomic_json(self.path, dict(schema='qa'))
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): report.inventory(alias / 'state.json', 'qa')
        ds.folder(self.path).symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): report.inventory(self.path, 'qa')

    def test_entry_limit_and_sample_are_bounded(self):
        atomic_json(self.path, dict(schema='qa'))
        folder = ds.folder(self.path)
        folder.mkdir()
        for number in range(110):
            (folder / (f'{number:064x}.part')).write_bytes(b'x')
        result = report.inventory(self.path, 'qa', 5)
        self.assertFalse(result['scan_complete'])
        self.assertEqual(result['entries_visited'], 5)
        result = report.inventory(self.path, 'qa')
        self.assertTrue(result['sample_truncated'])
        self.assertEqual(len(result['unreferenced_sample']), 100)
        for limit in (True, 0, 100001):
            with self.assertRaises(ValueError): report.inventory(self.path, 'qa', limit)

    def test_manifest_change_is_reported_without_repair(self):
        atomic_json(self.path, dict(schema='qa'))
        original = report.manifest_bytes
        calls = []
        def changing(path):
            calls.append(path)
            if len(calls) == 2:
                atomic_json(path, dict(schema='qa', newer=True))
            return original(path)
        with patch.object(report, 'manifest_bytes', side_effect=changing):
            result = report.inventory(self.path, 'qa')
        self.assertFalse(result['observed_unchanged'])
        self.assertTrue(json.loads(self.path.read_text())['newer'])

    def test_repeated_parts_use_order_for_hash_but_unique_disk_count(self):
        folder = ds.folder(self.path)
        folder.mkdir()
        raw = b'x' * 40
        sha = hashlib.sha256(raw).hexdigest()
        (folder / (sha + '.part')).write_bytes(raw)
        atomic_json(self.path, dict(schema=ds.SCHEMA, kind='qa', bytes=80,
                    sha256=hashlib.sha256(raw * 2).hexdigest(),
                    parts=[dict(sha256=sha, bytes=40)] * 2))
        result = report.inventory(self.path, 'qa')
        self.assertEqual(result['totals']['referenced_files'], 1)
        self.assertEqual(result['totals']['referenced_bytes'], 40)
        self.assertFalse(result['logical_json_verified'])

    def test_oversized_manifest_missing_document_and_part_are_refused(self):
        with self.assertRaises(OSError): report.inventory(self.path, 'qa')
        self.path.write_bytes(b' ' * (ds.INLINE_BYTES + 1))
        with self.assertRaises(ValueError): report.inventory(self.path, 'qa')
        self.write('a' * 300000)
        next(ds.folder(self.path).iterdir()).unlink()
        with self.assertRaises(OSError): report.inventory(self.path, 'qa')
