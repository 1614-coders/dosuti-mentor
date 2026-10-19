import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import managed_storage as storage
from storage_lock import file_lock


class ManagedStorage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = storage.initialize(self.base / 'данные с пробелами')

    def test_existing_unowned_folder_is_preserved(self):
        other = self.base / 'other'; other.mkdir(); (other / 'keep').write_text('user')
        with self.assertRaises(ValueError): storage.initialize(other)
        self.assertEqual((other / 'keep').read_text(), 'user')
        self.assertFalse((other / storage.MARKER).exists())

    def test_marker_is_stable_and_discovered_from_descendant(self):
        before = (self.home / storage.MARKER).read_bytes()
        self.assertEqual(storage.initialize(self.home), self.home)
        self.assertEqual(storage.find_home(self.home / 'run/course/lessons'), self.home)
        self.assertEqual((self.home / storage.MARKER).read_bytes(), before)

    def test_corrupt_marker_does_not_fall_back(self):
        (self.home / storage.MARKER).write_text('{}')
        with self.assertRaises(ValueError): storage.find_home(self.home / 'course')

    def test_session_redirects_caches_temp_and_restores_environment(self):
        before = dict(os.environ)
        with storage.session(self.home):
            self.assertTrue(Path(tempfile.gettempdir()).is_relative_to(self.home / 'temp'))
            target = Path(tempfile.gettempdir()) / 'a'; target.write_text('working')
            for key in ('PIP_CACHE_DIR','HF_HOME','XDG_CACHE_HOME','IMAGEIO_USERDIR'):
                self.assertTrue(Path(os.environ[key]).is_relative_to(self.home / 'cache'))
            worker = subprocess.run([sys.executable, '-B', '-c',
                'import tempfile,os,json; print(json.dumps([tempfile.gettempdir(),os.environ["PIP_CACHE_DIR"]]))'],
                capture_output=True, text=True, check=True)
            child = json.loads(worker.stdout)
            self.assertEqual(child[0], tempfile.gettempdir())
            self.assertEqual(child[1], os.environ['PIP_CACHE_DIR'])
        self.assertFalse(target.exists())
        self.assertEqual(dict(os.environ), before)

    def test_session_cleans_up_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with storage.session(self.home):
                target = Path(tempfile.gettempdir())
                raise RuntimeError('interrupted')
        self.assertFalse(target.exists())

    def test_nested_session_keeps_parent_lease(self):
        with storage.session(self.home):
            first = tempfile.gettempdir()
            with storage.session(self.home): self.assertEqual(tempfile.gettempdir(), first)
            self.assertTrue(Path(first).exists())

    def test_active_worker_blocks_deletion(self):
        with storage.session(self.home):
            cache = self.home / 'cache/pip/keep'; cache.write_text('in use')
            with self.assertRaises(TimeoutError): storage.maintain(self.home, True, 0)
            self.assertEqual(cache.read_text(), 'in use')

    def test_bootstrap_lock_blocks_deletion(self):
        with file_lock(self.home / '.bootstrap.lock'):
            with self.assertRaises(TimeoutError): storage.maintain(self.home, True, 0)

    def test_cache_budget_preserves_models_progress_and_unknown_temp(self):
        storage.environment(self.home)
        cache = self.home / 'cache/a'; cache.write_bytes(b'1234567890')
        model = self.home / 'models/weights'; model.parent.mkdir(); model.write_bytes(b'model')
        progress = self.home / 'run/course/progress'; progress.parent.mkdir(parents=True); progress.write_text('answer')
        unknown = self.home / 'temp/user-note'; unknown.write_text('keep')
        preview = storage.maintain(self.home, max_cache_bytes=0)
        self.assertFalse(preview['applied']); self.assertTrue(cache.exists())
        result = storage.maintain(self.home, True, 0)
        self.assertEqual(result['cache_after_bytes'], 0); self.assertFalse(cache.exists())
        self.assertEqual(model.read_bytes(), b'model')
        self.assertEqual(progress.read_text(), 'answer'); self.assertTrue(unknown.exists())

    def test_reclaims_only_abandoned_job_temporary(self):
        storage.environment(self.home)
        stale = self.home / 'temp' / ('job-' + uuid.uuid4().hex); stale.mkdir()
        (stale / '.lease.lock').touch(); (stale / 'download.part').write_text('partial')
        missing_marker = self.home / 'temp' / ('job-' + uuid.uuid4().hex); missing_marker.mkdir()
        storage.maintain(self.home, True)
        self.assertFalse(stale.exists()); self.assertTrue(missing_marker.exists())

    def test_dry_run_and_apply_preserve_learning_original_and_external_files(self):
        storage.environment(self.home)
        names=['original.mp4','run/course/learners/default/profile.json',
               'run/course/learners/default/unit-learning.json','run/course/evidence/answer.json',
               'run/course/study-units.json','run/course/study-sources/text.txt']
        protected=[self.home/name for name in names]+[self.base/'external.txt']
        for p in protected:p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'preserved')
        cache=self.home/'cache/disposable';cache.write_bytes(b'cached')
        preview=storage.maintain(self.home,max_cache_bytes=0)
        self.assertEqual(preview['delete_cache_files'],['cache/disposable']);self.assertTrue(cache.exists())
        storage.maintain(self.home,apply=True,max_cache_bytes=0)
        self.assertFalse(cache.exists())
        for p in protected:self.assertEqual(p.read_bytes(),b'preserved')

    def test_cleanup_without_managed_marker_cannot_delete_cache(self):
        storage.environment(self.home)
        cache=self.home/'cache/disposable';cache.write_text('keep')
        (self.home/storage.MARKER).unlink()
        with self.assertRaises(OSError):storage.maintain(self.home,apply=True,max_cache_bytes=0)
        self.assertEqual(cache.read_text(),'keep')

    def test_rejects_cache_symlink_before_deleting_any_file(self):
        storage.environment(self.home)
        outside = self.base / 'outside'; outside.write_text('user')
        first = self.home / 'cache/a'; first.write_text('owned')
        (self.home / 'cache/z').symlink_to(outside)
        with self.assertRaises(ValueError): storage.maintain(self.home, True, 0)
        self.assertTrue(first.exists()); self.assertEqual(outside.read_text(), 'user')

    def test_rejects_hard_links_and_negative_budget(self):
        storage.environment(self.home)
        outside = self.base / 'outside'; outside.write_text('user')
        os.link(outside, self.home / 'cache/link')
        with self.assertRaises(ValueError): storage.maintain(self.home, True, 0)
        with self.assertRaises(ValueError): storage.maintain(self.home, True, -1)

    def test_no_managed_home_keeps_old_course_behavior(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(storage.find_home(self.base / 'old-course'))

    def test_temporary_override_cannot_escape_home(self):
        with self.assertRaises(ValueError): storage.environment(self.home, self.base)

    def test_usage_counts_owned_bytes_without_following_external_links(self):
        folder = self.home / 'models'; folder.mkdir(); (folder / 'weights').write_bytes(b'1234')
        outside = self.base / 'outside'; outside.write_bytes(b'external')
        (folder / 'external').symlink_to(outside)
        result = storage.usage(self.home)
        self.assertEqual(result['logical_bytes_by_folder']['models'], 4)
        self.assertEqual(result['links_skipped'], 1)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'Native OCR cache is macOS-only')
    def test_native_cache_is_persistent_inside_home_not_job_temporary(self):
        from native_cache import host_root
        with storage.session(self.home):
            self.assertTrue(host_root().is_relative_to(self.home / 'runtime'))
            self.assertFalse(host_root().is_relative_to(Path(tempfile.gettempdir())))
