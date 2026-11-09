"""Final cache publication: isolated fixtures, no network or real learner data."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import video_sources as video
from safe_zip_import import supported


@unittest.skipUnless(supported(), 'Requires POSIX descriptor-relative no-follow operations')
class VideoCachePublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.lesson = self.root / 'lesson'
        self.lesson.mkdir()
        self.assets = self.lesson / 'video'
        self.source = dict(source='https://example.invalid/lecture.mp4')
        self.payload = b'isolated video fixture'
        self.fetches = 0
        self.fetch = patch.object(video, 'fetch_direct', self.download)
        self.fetch.start(); self.addCleanup(self.fetch.stop)
        self.validate = patch.object(video, 'validate_media')
        self.validate.start(); self.addCleanup(self.validate.stop)

    def download(self, source, folder):
        self.fetches += 1
        target = folder / 'lecture.mp4'
        target.write_bytes(self.payload)
        return target, 'direct'

    def acquire(self):
        return video.acquire(self.lesson, self.source)

    def test_download_and_cache_reuse(self):
        target = self.acquire()
        self.assertEqual(target.read_bytes(), self.payload)
        receipt = json.loads((self.assets / 'download.json').read_text())
        self.assertEqual(receipt['sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(self.acquire(), target)
        self.assertEqual(self.fetches, 1)

    def test_missing_pinned_asset_is_refetched_without_replacing_receipt(self):
        target = self.acquire()
        marker = self.assets / 'download.json'
        before = marker.read_bytes(), marker.stat().st_ino
        self.source['sha256'] = hashlib.sha256(self.payload).hexdigest()
        target.unlink()
        self.assertEqual(self.acquire(), target)
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual((marker.read_bytes(), marker.stat().st_ino), before)
        self.assertEqual(self.fetches, 2)

    def test_directory_swap_at_receipt_publication_preserves_foreign_files(self):
        foreign = self.root / 'foreign'
        (foreign / '.git').mkdir(parents=True)
        (foreign / '.git/keep').write_bytes(b'git sentinel')
        (foreign / 'download.json').write_bytes(b'foreign receipt')
        original = video.move_exclusive
        def swap(src_fd, src, dst_fd, dst):
            if dst == 'download.json':
                self.assets.rename(self.lesson / 'owned')
                foreign.rename(self.assets)
            return original(src_fd, src, dst_fd, dst)
        with patch.object(video, 'move_exclusive', swap):
            with self.assertRaises((ValueError, OSError)):
                self.acquire()
        self.assertEqual((self.assets / 'download.json').read_bytes(), b'foreign receipt')
        self.assertEqual((self.assets / '.git/keep').read_bytes(), b'git sentinel')

    def test_receipt_appearing_at_publication_is_not_overwritten(self):
        original = video.move_exclusive
        def compete(src_fd, src, dst_fd, dst):
            if dst == 'download.json':
                (self.assets / dst).write_bytes(b'other writer')
            return original(src_fd, src, dst_fd, dst)
        with patch.object(video, 'move_exclusive', compete):
            with self.assertRaises(FileExistsError):
                self.acquire()
        self.assertEqual((self.assets / 'download.json').read_bytes(), b'other writer')

    def test_rename_with_changed_ctime_is_accepted(self):
        original = video.move_exclusive
        observed = []
        def rename(src_fd, src, dst_fd, dst):
            if src == 'lecture.mp4':
                before = os.stat(src, dir_fd=src_fd, follow_symlinks=False)
                original(src_fd, src, dst_fd, dst)
                # chmod to the same mode forces a metadata event on POSIX;
                # the rename's destination bytes and stable fields stay intact.
                os.chmod(dst, before.st_mode & 0o777, dir_fd=dst_fd)
                after = os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
                observed.append((before.st_ctime_ns, after.st_ctime_ns))
            else:
                original(src_fd, src, dst_fd, dst)
        with patch.object(video, 'move_exclusive', rename):
            self.assertEqual(self.acquire().read_bytes(), self.payload)
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(*observed[0], 'Fixture must exercise a changed ctime')

    def test_destination_conflict_preserves_foreign_video(self):
        original = video.move_exclusive
        conflicts = []
        def compete(src_fd, src, dst_fd, dst):
            if src == 'lecture.mp4':
                target = self.assets / dst
                target.write_bytes(b'foreign video')
                conflicts.append(target)
            return original(src_fd, src, dst_fd, dst)
        with patch.object(video, 'move_exclusive', compete):
            with self.assertRaises(FileExistsError):
                self.acquire()
        self.assertEqual(conflicts[0].read_bytes(), b'foreign video')
        self.assertFalse((self.assets / 'download.json').exists())

    def test_registered_hash_mismatch_does_not_publish(self):
        self.source['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Video changed'):
            self.acquire()
        self.assertFalse((self.assets / 'download.json').exists())

    def test_source_leaf_swap_during_move_is_rejected(self):
        original = video.move_exclusive
        def swap(src_fd, src, dst_fd, dst):
            if src == 'lecture.mp4':
                os.rename(src, 'retained-original.mp4', src_dir_fd=src_fd, dst_dir_fd=src_fd)
                fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=src_fd)
                try:
                    os.write(fd, b'substitute')
                finally:
                    os.close(fd)
            return original(src_fd, src, dst_fd, dst)
        with patch.object(video, 'move_exclusive', swap):
            with self.assertRaisesRegex(ValueError, 'asset changed'):
                self.acquire()
        self.assertFalse((self.assets / 'download.json').exists())

    def test_changed_bytes_after_move_are_rejected(self):
        original = video.move_exclusive
        def tamper(src_fd, src, dst_fd, dst):
            original(src_fd, src, dst_fd, dst)
            if src == 'lecture.mp4':
                (self.assets / dst).write_bytes(b'x' * len(self.payload))
        with patch.object(video, 'move_exclusive', tamper):
            with self.assertRaises(ValueError):
                self.acquire()
        self.assertFalse((self.assets / 'download.json').exists())

    def test_interrupted_publication_can_retry_without_deleting_evidence(self):
        original = video.move_exclusive
        def interrupt(src_fd, src, dst_fd, dst):
            if dst == 'download.json':
                raise KeyboardInterrupt()
            return original(src_fd, src, dst_fd, dst)
        with patch.object(video, 'move_exclusive', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.acquire()
        retained = {p: p.read_bytes() for p in self.assets.glob('video-*')}
        self.assertEqual(len(retained), 1)
        target = self.acquire()
        self.assertEqual(target.read_bytes(), self.payload)
        for path, content in retained.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.acquire(), target)


class UnsupportedVideoPublication(unittest.TestCase):
    def test_unsupported_platform_refuses_before_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('safe_zip_import.supported', return_value=False), patch.object(video, 'fetch_direct') as fetch:
                with self.assertRaisesRegex(ValueError, 'no unsafe fallback'):
                    video.acquire(Path(tmp).resolve(), dict(source='https://example.invalid/lecture.mp4'))
                fetch.assert_not_called()
