"""Reuse source verification inside a cache lookup, never between operations."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import source_integrity as integrity
import video_sources


class VideoHashReuse(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name).resolve();self.assets=self.folder/'video';self.assets.mkdir()
        self.video=self.assets/'lecture.mp4';self.video.write_bytes(b'a'*4096)
        self.sha=integrity.file_hash(self.video)
        self.data=dict(video=dict(source='https://example.invalid/lecture.mp4',sha256=self.sha,path=str(self.video)))
        (self.folder/'lesson.json').write_text(json.dumps(self.data))
        self.receipt=dict(source=self.data['video']['source'],file=self.video.name,size=4096,sha256=self.sha)
        (self.assets/'download.json').write_text(json.dumps(self.receipt))

    def read(self):return course.checked_video(self.folder,self.data)

    @unittest.skipIf(os.name=='nt','Windows retains conservative source rechecks')
    def test_cached_video_has_one_hash_per_operation(self):
        original=hashlib.file_digest;hashed=[]
        def count(stream,*args,**kwargs):
            if Path(stream.name)==self.video:hashed.append(True)
            return original(stream,*args,**kwargs)
        with patch.object(hashlib,'file_digest',count):
            self.assertEqual(self.read(),(self.video,self.sha));self.assertEqual(len(hashed),1)
            self.read();self.assertEqual(len(hashed),2)

    def test_cache_receipt_and_registered_hash_are_both_checked(self):
        self.data['video']['sha256']='b'*64
        with self.assertRaisesRegex(ValueError,'Video changed'):self.read()
        self.assertEqual(json.loads((self.folder/'lesson.json').read_text())['video']['sha256'],self.sha)

    def test_modified_source_between_operations_is_not_blessed(self):
        self.read();old=self.video.stat();self.video.write_bytes(b'b'*4096)
        os.utime(self.video,ns=(old.st_atime_ns,old.st_mtime_ns))
        with self.assertRaisesRegex(ValueError,'integrity changed'):self.read()

    def test_change_before_publish_is_rejected(self):
        original=course.update_lesson
        def change(*args,**kwargs):
            self.video.write_bytes(b'b'*4096)
            return original(*args,**kwargs)
        with patch.object(course,'update_lesson',change):
            with self.assertRaisesRegex(ValueError,'changed'):self.read()

    def test_corrupted_receipt_does_not_pass_registered_hash(self):
        self.receipt['sha256']='b'*64
        (self.assets/'download.json').write_text(json.dumps(self.receipt))
        with self.assertRaisesRegex(ValueError,'integrity changed'):self.read()

    def test_failed_operation_does_not_keep_a_hash(self):
        with patch.object(course,'update_lesson',side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):self.read()
        self.video.write_bytes(b'b'*4096)
        with self.assertRaisesRegex(ValueError,'integrity changed'):self.read()

    @unittest.skipIf(os.name=='nt','Windows retains conservative source rechecks')
    def test_new_download_staging_is_not_retained_in_verification_cache(self):
        self.video.unlink();(self.assets/'download.json').unlink()
        self.data['video'].pop('path');original=hashlib.file_digest;hashed=[]
        def count(stream,*args,**kwargs):
            hashed.append(Path(stream.name));return original(stream,*args,**kwargs)
        def fetch(url,target,*args,**kwargs):target.write_bytes(b'a'*4096)
        with patch.object(video_sources,'curl',fetch), patch.object(video_sources,'validate_media'), patch.object(hashlib,'file_digest',count):
            video,checksum=self.read()
        self.assertEqual(checksum,self.sha)
        self.assertTrue(video.exists());self.assertEqual(hashed.count(video),1)
        self.assertEqual(len(hashed),2)
        self.assertEqual(list(self.assets.glob('.fetch-*')),[])

    @unittest.skipIf(os.name=='nt','ctime is not a change counter on Windows')
    def test_in_operation_same_size_and_restored_mtime_still_detected(self):
        original=course.update_lesson
        def change(*args,**kwargs):
            old=self.video.stat();self.video.write_bytes(b'b'*4096)
            os.utime(self.video,ns=(old.st_atime_ns,old.st_mtime_ns))
            return original(*args,**kwargs)
        with patch.object(course,'update_lesson',change):
            with self.assertRaisesRegex(ValueError,'changed'):self.read()

    def test_without_metadata_reuse_retains_full_source_checks(self):
        original=hashlib.file_digest;hashed=[]
        def count(stream,*args,**kwargs):
            if Path(stream.name)==self.video:hashed.append(True)
            return original(stream,*args,**kwargs)
        with patch.object(course,'video_verification_paths',return_value=[]), patch.object(integrity,'include_verification_path'), patch.object(hashlib,'file_digest',count):
            self.read()
        self.assertEqual(len(hashed),3)

    def test_source_cache_never_hides_corrupted_prepared_materials(self):
        prepared=self.folder/'prepared';prepared.mkdir();(prepared/'frames').mkdir()
        names=('audio.wav','frames/one.jpg','transcript.json')
        for name in names:
            with self.subTest(material=name):
                for target in names:(prepared/target).write_bytes(b'original')
                manifest=dict(frames=[dict(file='frames/one.jpg')],
                    evidence_sha256={target:integrity.file_hash(prepared/target) for target in names[1:]},
                    audio_sha256=integrity.file_hash(prepared/'audio.wav'))
                def change(*args,**kwargs):
                    integrity.verify_evidence(prepared,manifest,audio=True)
                    path=prepared/name;old=path.stat();path.write_bytes(b'modified')
                    os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
                    integrity.verify_evidence(prepared,manifest,audio=True)
                with patch.object(course,'update_lesson',change):
                    with self.assertRaisesRegex(ValueError,'integrity changed'):self.read()

    def test_strict_verification_uses_initial_and_final_hash(self):
        original=hashlib.file_digest;hashed=[]
        def count(stream,*args,**kwargs):
            if Path(stream.name)==self.video:hashed.append(True)
            return original(stream,*args,**kwargs)
        @integrity.verification_session(paths=lambda: [self.video],rehash=True)
        def strict():return self.read()
        with patch.object(hashlib,'file_digest',count):
            self.assertEqual(strict(),(self.video,self.sha))
        self.assertEqual(len(hashed),2)

    def test_strict_final_hash_detects_changes_when_metadata_cannot(self):
        before=integrity.identity(self.video);original=course.update_lesson
        def change(*args,**kwargs):
            self.video.write_bytes(b'b'*4096)
            return original(*args,**kwargs)
        @integrity.verification_session(paths=lambda: [self.video],rehash=True)
        def strict():return self.read()
        with patch.object(integrity,'identity',return_value=before), patch.object(course,'update_lesson',change):
            with self.assertRaisesRegex(ValueError,'changed'):strict()
