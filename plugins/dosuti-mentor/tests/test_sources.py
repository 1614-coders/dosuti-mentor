"""Real media, controlled remote responses and relocated course storage."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import course
import storage_paths
import test_integrity as fixtures
import video_sources


class SourceAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SourceIntegrityRegression.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.lesson = self.root / 'lesson'
        self.lesson.mkdir()
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def fake_curl(self, url, target, duration=1800):
        self.calls.append(url)
        if 'cloud-api.yandex.net' in url:
            target.write_text(json.dumps(dict(type='file', file='https://cdn.example/video',
                size=len(self.a), sha256=hashlib.sha256(self.a).hexdigest())))
        else:
            target.write_bytes(self.a)

    def acquire(self, source, **fields):
        return video_sources.acquire(self.lesson, dict(source=source, **fields))

    def test_yandex_cache_works_without_metadata_or_network(self):
        url = 'https://disk.yandex.ru/i/example'
        with patch.object(video_sources, 'curl', self.fake_curl):
            first = self.acquire(url)
        with patch.object(video_sources, 'curl', side_effect=AssertionError('Network called')):
            second = self.acquire(url, sha256=course.file_hash(first), path=str(first))
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list((self.lesson/'video').glob('.fetch-*')), [])

    def test_direct_url_including_query_and_uppercase_suffix(self):
        url = 'https://files.example/lecture.MP4?token=test'
        with patch.object(video_sources, 'curl', self.fake_curl):
            path = self.acquire(url)
        self.assertEqual(path.read_bytes(), self.a)
        marker = course.read(self.lesson/'video/download.json')
        self.assertEqual(marker['adapter'], 'direct')
        self.assertEqual(marker['source'], url)

    def test_other_sites_use_generic_extractor_not_host_whitelist(self):
        url = 'https://other.example/lesson/1'
        commands = []
        real_run = subprocess.run
        def fake(command, **kw):
            if '-m' in command and 'yt_dlp' in command:
                if '--dump-single-json' in command:
                    return NS(returncode=0, stdout=json.dumps(dict(_type='playlist', entries=[dict(id='1')])))
                commands.append(command)
                template = Path(command[command.index('-o')+1])
                final = template.parent/'lecture.mkv'
                final.write_bytes(self.a)
                report = Path(command[command.index('--print-to-file')+2])
                report.write_text(json.dumps(str(final))+'\n')
                return NS(returncode=0)
            return real_run(command, **kw)
        with patch.object(video_sources.subprocess, 'run', fake):
            path = self.acquire(url)
        self.assertEqual(path.read_bytes(), self.a)
        self.assertEqual(commands[0][-1], url)
        self.assertIn('--no-playlist', commands[0])
        self.assertIn('--ignore-config', commands[0])
        self.assertIn('--no-plugin-dirs', commands[0])
        with patch.object(video_sources, 'fetch_site', side_effect=AssertionError('Network called')):
            self.assertEqual(self.acquire(url), path)

    def test_legacy_pinned_yandex_cache_is_adopted_offline(self):
        assets = self.lesson/'video'; assets.mkdir()
        path = assets/'lecture.mp4'; path.write_bytes(self.a)
        with patch.object(video_sources, 'curl', side_effect=AssertionError('Network called')):
            self.assertEqual(self.acquire('https://yadi.sk/i/example', sha256=course.file_hash(path)), path.resolve())

    def test_unpinned_legacy_file_is_not_blessed(self):
        assets = self.lesson/'video'; assets.mkdir()
        old = assets/'lecture.mp4'; old.write_bytes(self.b)
        with patch.object(video_sources, 'curl', self.fake_curl):
            new = self.acquire('https://files.example/lecture.mp4')
        self.assertEqual(old.read_bytes(), self.b)
        self.assertEqual(new.read_bytes(), self.a)
        self.assertNotEqual(old, new)

    def test_changed_cached_bytes_never_trigger_silent_redownload(self):
        url = 'https://files.example/lecture.mp4'
        with patch.object(video_sources, 'curl', self.fake_curl):
            path = self.acquire(url)
        path.write_bytes(self.b)
        with patch.object(video_sources, 'curl', side_effect=AssertionError('Network called')):
            with self.assertRaisesRegex(ValueError, 'integrity'):
                self.acquire(url)

    def test_missing_pinned_cached_file_can_be_restored_from_same_bytes(self):
        url='https://files.example/lecture.mp4'
        with patch.object(video_sources,'curl',self.fake_curl):
            first=self.acquire(url)
            checksum=course.file_hash(first);first.unlink()
            restored=self.acquire(url,sha256=checksum)
        self.assertEqual(restored.read_bytes(),self.a)
        self.assertEqual(len(self.calls),2)

    def test_missing_cache_does_not_accept_changed_remote_video(self):
        url='https://files.example/lecture.mp4'
        with patch.object(video_sources,'curl',self.fake_curl): first=self.acquire(url)
        checksum=course.file_hash(first);first.unlink()
        marker=(self.lesson/'video/download.json').read_bytes()
        def different(url,target,duration=1800):target.write_bytes(self.b)
        with patch.object(video_sources,'curl',different):
            with self.assertRaisesRegex(ValueError,'changed since registration'):
                self.acquire(url,sha256=checksum)
        self.assertEqual((self.lesson/'video/download.json').read_bytes(),marker)

    def test_cache_for_another_url_is_rejected(self):
        with patch.object(video_sources, 'curl', self.fake_curl):
            self.acquire('https://files.example/one.mp4')
        with self.assertRaisesRegex(ValueError, 'another source'):
            self.acquire('https://files.example/two.mp4')

    def test_cached_receipt_cannot_escape_assets(self):
        assets=self.lesson/'video';assets.mkdir()
        outside=self.root/'outside.mp4';outside.write_bytes(self.a)
        course.save(assets/'download.json', dict(source='https://files.example/one.mp4',
            file='../../outside.mp4', size=len(self.a), sha256=course.file_hash(outside)))
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.acquire('https://files.example/one.mp4')

    def test_direct_html_is_not_registered_as_video(self):
        def html(url, target, duration=1800): target.write_text('<html>Login required</html>')
        with patch.object(video_sources, 'curl', html):
            with self.assertRaisesRegex(ValueError, 'readable video'):
                self.acquire('https://files.example/lecture.mp4')
        self.assertFalse((self.lesson/'video/download.json').exists())

    def test_remote_bytes_must_match_existing_pin(self):
        with patch.object(video_sources, 'curl', self.fake_curl):
            with self.assertRaisesRegex(ValueError, 'changed since registration'):
                self.acquire('https://files.example/lecture.mp4', sha256=hashlib.sha256(self.b).hexdigest())
        self.assertFalse((self.lesson/'video/download.json').exists())

    def test_yandex_bad_size_or_hash_is_rejected(self):
        def bad(url, target, duration=1800):
            if 'cloud-api' in url:
                target.write_text(json.dumps(dict(type='file', file='https://cdn.example/video',
                    size=len(self.a), sha256=hashlib.sha256(self.b).hexdigest())))
            else: target.write_bytes(self.a)
        with patch.object(video_sources, 'curl', bad):
            with self.assertRaisesRegex(ValueError, 'integrity'):
                self.acquire('https://disk.yandex.com/i/one')

    def test_interrupted_download_leaves_no_ready_cache_and_can_retry(self):
        url = 'https://files.example/lecture.mp4'
        def broken(url, target, duration=1800):
            target.write_bytes(self.a[:100])
            raise subprocess.CalledProcessError(18, ['curl'])
        with patch.object(video_sources, 'curl', broken):
            with self.assertRaises(subprocess.CalledProcessError): self.acquire(url)
        self.assertFalse((self.lesson/'video/download.json').exists())
        self.assertEqual(list((self.lesson/'video').glob('.fetch-*')), [])
        with patch.object(video_sources, 'curl', self.fake_curl): self.acquire(url)

    def test_parallel_downloads_fetch_once(self):
        def slow(url, target, duration=1800):
            time.sleep(.1);self.fake_curl(url,target,duration)
        with patch.object(video_sources, 'curl', slow), ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _: self.acquire('https://files.example/lecture.mp4'),range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(len(self.calls),1)

    def test_site_failure_explains_local_file_fallback(self):
        with patch.object(video_sources.subprocess, 'run', side_effect=subprocess.CalledProcessError(1,['yt-dlp'])):
            with self.assertRaisesRegex(ValueError, 'login.*local video'):
                self.acquire('https://other.example/private')

    def test_playlist_is_rejected_before_downloading(self):
        result=NS(returncode=0,stdout=json.dumps(dict(_type='playlist',entries=[dict(id='1'),dict(id='2')])))
        with patch.object(video_sources.subprocess,'run',return_value=result) as run:
            with self.assertRaisesRegex(ValueError,'not a playlist'):
                self.acquire('https://other.example/list')
        self.assertEqual(run.call_count,1)

    def test_live_stream_is_rejected_before_downloading(self):
        with patch.object(video_sources.subprocess,'run',return_value=NS(stdout='{"is_live":true}')) as run:
            with self.assertRaisesRegex(ValueError,'live stream'):
                self.acquire('https://other.example/live')
        self.assertEqual(run.call_count,1)

    def test_windows_paths_are_not_urls(self):
        for source in ['C:\\Videos\\lesson.mp4', 'D:/Videos/lesson.mp4', '\\\\server\\share\\lesson.mp4']:
            self.assertEqual(storage_paths.source_kind(source), 'local')
        if os.name != 'nt':
            with self.assertRaisesRegex(ValueError,'requires Windows'):
                self.acquire('C:\\Videos\\lesson.mp4')

    def test_http_and_https_classification_and_unknown_schemes(self):
        for url in ['http://example.com/lecture.mp4','https://example.com/watch?v=1']:
            self.assertEqual(storage_paths.source_kind(url),'remote')
        for url in ['ftp://example.com/video.mp4','file:///tmp/video.mp4','']:
            with self.assertRaises(ValueError):storage_paths.source_kind(url)


class PortableCourse(fixtures.SourceIntegrityRegression):
    # Inherit fixture helpers, not the older regression cases.
    def test_relative_video_registration_survives_changed_working_directory(self):
        relative=self.root/'relative.mp4';relative.write_bytes(self.a)
        old=Path.cwd()
        try:
            os.chdir(self.root)
            args=NS(**{**vars(self.args),'lesson':'relative','video':'relative.mp4'})
            with contextlib.redirect_stdout(io.StringIO()):course.add_lesson(args)
        finally:os.chdir(old)
        data=course.read(self.path/'lessons/relative/lesson.json')
        self.assertEqual(course.download_video(self.path/'lessons/relative',data),relative.resolve())

    def test_moved_course_keeps_context_code_homework_resources_and_progress(self):
        self.register(self.bundle())
        teacher=self.root/'teacher';teacher.mkdir();(teacher/'Main.java').write_text('class Main {}')
        self.attach_code(teacher)
        task=self.root/'task.txt';task.write_text('Implement Main')
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(NS(**vars(self.args),kind='homework',source=str(task),none=False))
            course.attach(NS(**vars(self.args),kind='resources',source=str(task),none=False,resource_type='file'))
            course.progress(NS(**vars(self.args),topic='OOP',stage='understood',details='Explained',homework_revision=None))
        # Remove external original to ensure it is not required for reading verified prepared material.
        self.video.unlink()
        moved=self.root/'moved course';self.path.rename(moved)
        self.args.course=moved;self.path=moved;self.lesson=moved/'lessons/lesson-01'
        result=self.capture(course.context,time=0,window=2)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['video_integrity'],'original_unavailable_using_pinned_hash')
        self.assertEqual(result['code_integrity'],'snapshot_hashes_match')
        self.assertTrue(all(Path(f['absolute_path']).is_file() for f in result['frames']))
        self.assertTrue(Path(result['homework']['file']).is_file())
        self.assertTrue(Path(result['resources']['items'][0]['source']).is_file())
        self.assertEqual(result['learner_progress'][0]['topic'],'OOP')

    def test_internal_video_paths_follow_course_move(self):
        video=self.path/'local.mp4';video.write_bytes(self.a)
        args=NS(**{**vars(self.args),'lesson':'internal','video':str(video)})
        with contextlib.redirect_stdout(io.StringIO()):course.add_lesson(args)
        moved=self.root/'renamed';self.path.rename(moved)
        directory=moved/'lessons/internal'
        data=course.read(directory/'lesson.json')
        self.assertEqual(course.download_video(directory,data),moved.resolve()/'local.mp4')

    def test_legacy_course_can_be_anchored_before_move(self):
        self.register(self.bundle())
        path=self.lesson/'lesson.json'
        raw=json.loads(path.read_text());raw.pop('storage_paths')
        path.write_text(json.dumps(raw))
        with contextlib.redirect_stdout(io.StringIO()):course.make_portable(self.args)
        moved=self.root/'moved';self.path.rename(moved);self.args.course=moved
        self.assertTrue(self.capture(course.context,time=0,window=2)['coverage']['complete'])

    def test_code_history_and_diff_keep_versions_after_move(self):
        teacher=self.root/'teacher';teacher.mkdir()
        source=teacher/'Main.java';source.write_text('class Main { int n=1; }\n')
        self.attach_code(teacher)
        source.write_text('class Main { int n=2; }\n');self.attach_code(teacher)
        args=NS(**{**vars(self.args),'lesson':'second','video':str(self.video)})
        with contextlib.redirect_stdout(io.StringIO()):
            course.add_lesson(args)
            source.write_text('class Main { int n=3; }\n')
            course.attach(NS(**vars(args),kind='code',source=str(teacher),none=False,ref=None))
        moved=self.root/'moved';self.path.rename(moved);args.course=moved
        data=course.read(moved/'lessons/lesson-01/lesson.json')
        self.assertIn('n=1',(Path(data['code']['history'][0]['snapshot'])/'Main.java').read_text())
        output=io.StringIO()
        with contextlib.redirect_stdout(output):course.diff_code(NS(**vars(args),previous='lesson-01',file=None))
        self.assertIn('n=3',json.loads(output.getvalue())['changes'][0]['diff'])

    def test_managed_anchor_cannot_escape_course(self):
        path=self.lesson/'lesson.json'
        data=json.loads(path.read_text())
        data['storage_paths']['anchors']=[dict(keys=['video','path'],relative='../outside.mp4')]
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'within the course'):course.read(path)

    def test_windows_anchor_and_unrelated_field_are_rejected(self):
        path=self.lesson/'lesson.json';original=json.loads(path.read_text())
        for anchor in [dict(keys=['video','path'],relative='C:/outside.mp4'),
                       dict(keys=['video','sha256'],relative='any')]:
            data={**original,'storage_paths':dict(version=1,anchors=[anchor])}
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):course.read(path)


# Reuse media/helper methods without counting inherited tests twice.
for _name in list(vars(fixtures.SourceIntegrityRegression)):
    if _name.startswith('test_') and _name not in PortableCourse.__dict__:
        setattr(PortableCourse,_name,None)


if __name__=='__main__':unittest.main()
