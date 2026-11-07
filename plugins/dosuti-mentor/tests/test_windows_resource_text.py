"""Portable text/CAS models and real Windows-only resource extraction security."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import course
import material_sources as ms
import registry_update as registry
import safe_material_import as safe
import windows_resource_text as rt
from windows_material_import import SingleFile


def item_for_models():
    return dict(id='resource', kind='file', source=r'C:\lesson\resources\one\notes.txt',
        origin=dict(source=r'C:\user\notes.txt', raw_file=r'C:\lesson\resources\one\notes.txt',
                    sha256='a' * 64, media_type=''))


class ResourceTextModels(unittest.TestCase):
    def test_utf8_bytes_match_existing_posix_text_contract(self):
        cases = [b'\n  code\n\twork()\n\n', b'\r\n  code\r\n\r\n', b'\twork()',
                 b'\xef\xbb\xbf\ttext\r\n', b'line\r\nnext\n\n\n']
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'notes.txt'
            for raw in cases:
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    text, method = ms.text_content(path)
                    self.assertEqual(rt.utf8_text(raw, 1024), text.encode('utf-8'))
                    self.assertEqual(method, 'utf8_text')

    def test_binary_html_empty_invalid_utf8_and_limits_are_not_extracted(self):
        for raw in (b'%PDF-1.4', b'PK\x03\x04zip', b'x\0y', b'\xff', b' \r\n', b'<html>text</html>'):
            with self.subTest(raw=raw), self.assertRaises(ValueError): rt.utf8_text(raw, 100)
        with self.assertRaises(ValueError): rt.utf8_text(b'12345', 4)
        with self.assertRaises(ValueError): rt.utf8_text(b'1234', 4)  # Normalized newline also bounded.

    def test_dispatch_is_only_registered_local_plain_resource(self):
        row, args = item_for_models(), NS(action='extract', visual_pages=None)
        self.assertTrue(rt.eligible(row, args))
        for change in [dict(source='https://example.test/notes'), dict(kind='video'),
                       dict(origin={}), dict(extraction='pdf_text_layer_v1')]:
            candidate = row | change
            self.assertFalse(rt.eligible(candidate, args))
        for key, value in [('source', 'https://example.test/notes'), ('media_type', 'text/html'),
                           ('raw_file', r'C:\lesson\resources\one\notes.pdf')]:
            candidate = deepcopy(row); candidate['origin'][key] = value
            self.assertFalse(rt.eligible(candidate, args))
        self.assertFalse(rt.eligible(row, NS(action='fetch', visual_pages=None)))
        self.assertFalse(rt.eligible(row, NS(action='extract', visual_pages=[1])))

    def test_resource_cas_refuses_changed_or_removed_item_without_writes(self):
        row = item_for_models(); proposed = row | dict(extracted_file='new', text_sha256='b'*64)
        for items in ([], [row | dict(status='concurrent')]):
            data = dict(resources=dict(items=deepcopy(items))); old = deepcopy(data)
            with self.assertRaisesRegex(ValueError, 'concurrently'):
                rt.compare_and_update(data, 'resource', row, proposed)
            self.assertEqual(data, old)
        other = dict(id='other', status='untouched')
        data = dict(resources=dict(items=[deepcopy(row), deepcopy(other)]))
        rt.compare_and_update(data, 'resource', row, proposed)
        self.assertEqual(data['resources']['items'], [proposed, other])
        proposed['status'] = 'caller changed it'
        self.assertNotIn('status', data['resources']['items'][0])

    def test_outside_snapshot_or_invalid_sha_is_refused_before_pin(self):
        store = rt.ResourceText(Path.cwd() / 'lesson', NS(), {})
        for path, checksum in [('relative.txt', 'a'*64), (str(Path.cwd()/'foreign.txt'), 'a'*64),
                               (str(store.directory/'resources/one/a.txt'), 'invalid'),
                               (str(store.directory/'code/one/a.txt'), 'a'*64)]:
            with self.subTest(path=path), patch.object(store, 'pin') as pin:
                with self.assertRaises(ValueError): store.checked_input(path, checksum)
                pin.assert_not_called()

    def test_writable_handle_never_reaches_registry(self):
        store = rt.ResourceText(Path.cwd(), NS(), {}); store.writers.add(123)
        with patch.object(rt.registry, 'update') as update:
            with self.assertRaisesRegex(ValueError, 'Writable resource HANDLE'): store.commit()
            update.assert_not_called()

    def test_unknown_receipt_is_not_replayed_and_primary_cleanup_are_preserved(self):
        receipt = registry.Receipt('lesson', 1, 'b'*64, 'new', 'a'*64, 'old', 'stage')
        primary = ValueError('primary')
        failure = registry.CommitInterrupted(receipt, 'unknown'); failure.__cause__ = primary
        cleanup = OSError('close failed')
        fake = NS(directory=Path.cwd(), prepare_resource=Mock(), commit=Mock(side_effect=failure),
                  close=Mock(side_effect=cleanup))
        with patch.object(rt, 'ResourceText', return_value=fake), patch.object(rt, 'owned_io', side_effect=lambda f:f()):
            with self.assertRaises(registry.CommitInterrupted) as caught:
                rt.resource_text(NS(action='extract'), Path.cwd(), item_for_models())
        self.assertIs(caught.exception, failure)
        self.assertIs(caught.exception.__cause__, primary)
        self.assertIs(caught.exception.receipt, receipt)
        self.assertEqual(caught.exception.cleanup_errors, (cleanup,))
        fake.commit.assert_called_once(); fake.close.assert_called_once()

    def test_windows_dispatch_uses_dedicated_backend_not_path_text_content(self):
        row = item_for_models(); args = NS(action='extract', resource_id='resource')
        with patch.object(ms.sys, 'platform', 'win32'), patch.object(course, 'lesson_path', return_value=Path.cwd()), \
                patch.object(course, 'read', return_value=dict(resources=dict(items=[row]))), \
                patch.object(rt, 'resource_text', return_value={'verified': True}) as run, \
                patch.object(ms, 'text_content', side_effect=AssertionError('No pathname extraction')):
            self.assertEqual(ms._resource.__wrapped__(args), {'verified': True})
            run.assert_called_once_with(args, Path.cwd(), row)


@unittest.skipUnless(os.name == 'nt', 'Requires native Windows x64/NTFS, never model acceptance')
class NativeResourceText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.course = self.root / 'course'
        self.lesson = self.course / 'lessons/lesson-01'; self.lesson.mkdir(parents=True)
        self.registry = self.lesson / 'lesson.json'
        self.registry.write_text(json.dumps(dict(id='lesson-01', revision=0, code={}, homework={},
            resources=dict(items=[]), video=dict(source='https://example.test/video'))), encoding='utf-8')
        self.progress = self.course / 'learners/default/progress.sqlite3'; self.progress.parent.mkdir(parents=True)
        with sqlite3.connect(self.progress) as db:
            db.execute('CREATE TABLE fixture_progress (value TEXT)')
            db.execute('INSERT INTO fixture_progress VALUES (?)', ('Existing learner progress',))
        self.progress_bytes = self.progress.read_bytes()
        self.source = self.root / 'заметки with spaces.txt'
        self.raw = b'\xef\xbb\xbf\tcode\r\n    work()\r\n\r\n'
        self.source.write_bytes(self.raw)
        self.foreign = self.root / 'foreign'; self.foreign.mkdir()
        (self.foreign / self.source.name).write_bytes(b'FOREIGN MUST STAY')
        self.item = self.attach()

    def attach(self):
        from material_import import _CURRENT
        state = dict(directory=self.lesson, owned=[]); token = _CURRENT.set(state)
        try:
            with redirect_stdout(io.StringIO()):
                safe.attach(NS(kind='resources', source=str(self.source), resource_type='file'), self.lesson)
        finally:
            if state.get('safe') is not None: state['safe'].close()
            _CURRENT.reset(token)
        return course.read(self.registry)['resources']['items'][-1]

    def args(self, action='extract', **kwargs):
        return NS(**(dict(course=self.course, lesson='lesson-01', action=action,
            resource_id=self.item['id'], expected_revision=None, notes=None, visual_pages=None) | kwargs))

    def extract(self):
        result = ms.resource(self.args())
        return next(row for row in result['items'] if row['id'] == self.item['id'])

    def intact(self):
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(self.progress.read_bytes(), self.progress_bytes)
        self.assertEqual((self.foreign / self.source.name).read_bytes(), b'FOREIGN MUST STAY')

    def test_extract_repeat_read_review_and_exact_hashes(self):
        with patch.object(ms, 'integrity', side_effect=AssertionError('Use verified HANDLE bytes')):
            row = self.extract()
        expected = rt.utf8_text(self.raw, ms.MAX_BYTES)
        self.assertEqual(row['extraction'], 'utf8_text')
        self.assertEqual(row['origin']['sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(row['text_sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(row['revision'], ms.resource_revision(row))
        self.assertEqual(row['source_integrity'], 'verified'); self.assertEqual(row['text_integrity'], 'verified')
        target = Path(row['extracted_file']); self.assertEqual(target.read_bytes(), expected)
        self.assertNotEqual(target.parent, Path(row['origin']['raw_file']).parent)
        result = ms.resource(self.args('review', expected_revision=row['revision'], notes='Read and checked code'))
        self.assertTrue(result['items'][0]['review_valid'])
        before, files = target.stat(), set((self.lesson/'materials').iterdir())
        with patch.object(rt.ResourceText, 'write_text', side_effect=AssertionError('No cached replay')):
            cached = self.extract()
        self.assertEqual(cached['extracted_file'], str(target)); self.assertTrue(cached['review_valid'])
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(set((self.lesson/'materials').iterdir()), files)
        self.assertEqual(Path(row['origin']['raw_file']).read_bytes(), self.raw)
        self.intact()

    def test_stale_review_refuses_without_registry_or_snapshot_changes(self):
        self.extract(); before = self.registry.read_bytes(); files = set((self.lesson/'materials').iterdir())
        with self.assertRaisesRegex(ValueError, 'revision changed'):
            ms.resource(self.args('review', expected_revision='sha256:stale', notes='Read'))
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(set((self.lesson/'materials').iterdir()), files)
        self.intact()

    def test_two_extractors_reach_barrier_one_resource_cas_wins(self):
        original, barrier, reached = rt.ResourceText.commit, threading.Barrier(2), []
        def commit(store):
            reached.append(store.folder); barrier.wait(timeout=10)
            return original(store)
        def worker():
            try: return ('ok', self.extract())
            except ValueError as error:
                if 'concurrently' not in str(error): raise
                return ('conflict', str(error))
        with patch.object(rt.ResourceText, 'commit', commit), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [f.result(timeout=30) for f in futures]
        self.assertEqual(len(reached), 2)
        self.assertEqual(sorted(row[0] for row in results), ['conflict', 'ok'])
        self.assertEqual(len(set(reached)), 2)
        for folder in reached: self.assertTrue(folder.is_dir(), 'Neither winner nor uncertain residue is deleted')
        before = set((self.lesson/'materials').iterdir())
        with patch.object(rt.ResourceText, 'write_text', side_effect=AssertionError('No automatic replay')):
            self.extract()
        self.assertEqual(set((self.lesson/'materials').iterdir()), before)
        self.intact()

    def test_source_and_output_namespace_replacement_is_blocked_after_verified_seal(self):
        original, attacks = rt.ResourceText.commit, []
        def commit(store):
            store.verify()
            for path in (store.inputs[0].path, store.inputs[0].path.parent, store.folder/store.name, store.folder):
                with self.assertRaises(OSError) as blocked: path.rename(path.with_name(path.name+'-moved'))
                self.assertIn(blocked.exception.winerror, (5, 32)); attacks.append(path)
            self.assertEqual(store.writers, set())
            with self.assertRaises(OSError): (store.folder/'foreign.txt').write_bytes(b'bad')
            return original(store)
        with patch.object(rt.ResourceText, 'commit', commit): self.extract()
        self.assertEqual(len(attacks), 4)
        self.intact()

    def test_source_junction_replacement_before_pin_refuses_foreign_target(self):
        raw = Path(self.item['origin']['raw_file']); snapshot = raw.parent
        snapshot.rename(snapshot.with_name(snapshot.name+'-owned'))
        process = subprocess.run(['cmd','/d','/c','mklink','/J',str(snapshot),str(self.foreign)],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
        self.assertTrue(snapshot.is_junction())
        before = self.registry.read_bytes()
        with self.assertRaises((ValueError, OSError)): self.extract()
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse((self.lesson/'materials').exists())
        self.intact()

    def test_wrong_source_sha_is_not_repaired_or_registered(self):
        def wrong(data): data['resources']['items'][0]['origin']['sha256'] = '0'*64
        course.update_lesson(self.lesson, wrong)
        before = self.registry.read_bytes()
        with self.assertRaisesRegex(ValueError, 'SHA256 changed'): self.extract()
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse((self.lesson/'materials').exists())
        self.intact()

    def test_precommit_failure_preserves_residue_and_explicit_retry_never_adopts_it(self):
        before = self.registry.read_bytes()
        with patch.object(rt.ResourceText, 'commit', side_effect=OSError('before resource commit')) as fail:
            with self.assertRaisesRegex(OSError, 'before resource commit'): self.extract()
            fail.assert_called_once()
        self.assertEqual(self.registry.read_bytes(), before)
        residue = next((self.lesson/'materials').iterdir())
        content = {p.name:p.read_bytes() for p in residue.iterdir()}
        row = self.extract()
        self.assertNotEqual(Path(row['extracted_file']).parent, residue)
        self.assertEqual({p.name:p.read_bytes() for p in residue.iterdir()}, content)
        self.intact()

    def test_postcommit_failure_retains_receipt_and_recovery_reuses_registered_text(self):
        original, receipts = rt.ResourceText.read_registry, []
        def read(store):
            if store.receipt is not None:
                receipts.append(store.receipt)
                raise OSError('after resource commit')
            return original(store)
        with patch.object(rt.ResourceText, 'read_registry', read):
            with self.assertRaises(registry.CommitInterrupted) as caught: self.extract()
        self.assertEqual(caught.exception.outcome, 'committed')
        self.assertIs(caught.exception.receipt, receipts[0])
        self.assertEqual(registry.recover(self.lesson, receipts[0]), 'committed')
        row = course.read(self.registry)['resources']['items'][0]
        self.assertTrue(Path(row['extracted_file']).is_file())
        before = set((self.lesson/'materials').iterdir())
        with patch.object(rt.ResourceText, 'write_text', side_effect=AssertionError('No replay after recovery')):
            cached = self.extract()
        self.assertEqual(cached['extracted_file'], row['extracted_file'])
        self.assertEqual(set((self.lesson/'materials').iterdir()), before)
        self.intact()

    def test_cached_text_wrong_sha_refuses_without_reextract_or_repair(self):
        row = self.extract(); target = Path(row['extracted_file']); data = target.read_bytes()
        course.update_lesson(self.lesson, lambda value:value['resources']['items'][0].update(text_sha256='0'*64))
        before, folders = self.registry.read_bytes(), set((self.lesson/'materials').iterdir())
        with patch.object(rt.ResourceText, 'write_text', side_effect=AssertionError('No repair by replay')):
            with self.assertRaisesRegex(ValueError, 'SHA256 changed'): self.extract()
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(set((self.lesson/'materials').iterdir()), folders)
        self.intact()

    def test_exclusive_text_snapshot_collision_preserves_foreign_file(self):
        from windows_managed_home import NativeError
        parent = self.lesson/'materials'; parent.mkdir()
        collision = parent/('a'*12); collision.mkdir()
        (collision/'keep').write_bytes(b'FOREIGN SNAPSHOT')
        before = self.registry.read_bytes()
        with patch.object(rt.uuid, 'uuid4', return_value=NS(hex='a'*32)):
            with self.assertRaises(NativeError) as caught: self.extract()
        self.assertEqual(caught.exception.status, 0xC0000035)
        self.assertEqual((collision/'keep').read_bytes(), b'FOREIGN SNAPSHOT')
        self.assertEqual(self.registry.read_bytes(), before)
        self.intact()

    def test_output_write_flush_and_seal_faults_never_reach_registration(self):
        before = self.registry.read_bytes()
        from windows_managed_home import Native
        from windows_material_import import Security
        for owner, name in ((Native, 'write'), (Native, 'flush'), (Security, 'seal')):
            with self.subTest(name=name), patch.object(owner, name, side_effect=OSError('injected '+name)) as failure:
                with self.assertRaisesRegex(OSError, 'injected '+name): self.extract()
                failure.assert_called_once()
                self.assertEqual(self.registry.read_bytes(), before)
        original = rt.ResourceText.read
        def corrupt(store, handle):
            data = original(store, handle)
            return data+b'wrong' if store.writers else data
        with patch.object(rt.ResourceText, 'read', corrupt):
            with self.assertRaisesRegex(OSError, 'readback mismatch'): self.extract()
        self.assertEqual(self.registry.read_bytes(), before)
        self.intact()


if __name__ == '__main__': unittest.main()
