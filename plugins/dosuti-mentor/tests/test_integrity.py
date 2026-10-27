"""Actual media and filesystem regressions for source substitution and drift."""
import contextlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import course
import lecture
import tutor_packet
from source_integrity import evidence_hashes


class SourceIntegrityRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import imageio_ffmpeg
        cls.media_temp = tempfile.TemporaryDirectory()
        cls.media = Path(cls.media_temp.name)
        for name, color in [('a', 'black'), ('b', 'red')]:
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', f'color=c={color}:s=160x90:r=5:d=2',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(cls.media / f'{name}.mp4')],
                check=True, capture_output=True)
        size = max(p.stat().st_size for p in cls.media.glob('*.mp4')) + 1024
        for path in cls.media.glob('*.mp4'):
            padding = size - path.stat().st_size
            with path.open('ab') as stream:
                stream.write(struct.pack('>I', padding) + b'free' + bytes(padding - 8))
        cls.a = (cls.media / 'a.mp4').read_bytes()
        cls.b = (cls.media / 'b.mp4').read_bytes()
        assert len(cls.a) == len(cls.b) and cls.a != cls.b

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / 'lecture.mp4'
        self.video.write_bytes(self.a)
        self.path = self.root / 'course'
        self.args = NS(course=self.path, lesson='lesson-01', profile='default',
                       title='Integrity', video=str(self.video), order=1)
        with contextlib.redirect_stdout(io.StringIO()):
            course.init(self.args)
            course.add_lesson(self.args)
        self.lesson = self.path / 'lessons/lesson-01'

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        return course.read(self.lesson / 'lesson.json')

    def capture(self, function, **kw):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            function(NS(**vars(self.args), **kw))
        return json.loads(output.getvalue())

    def extract(self, video=None, folder=None, resume=False, frame_every='0.5'):
        folder = folder or self.root / 'bundle'
        command = [sys.executable, str(SCRIPTS / 'lecture.py'), str(video or self.video),
                   '--output', str(folder), '--start', '0', '--duration', '2',
                   '--frame-every', frame_every, '--prepare-only']
        if resume:
            command.append('--resume')
        return subprocess.run(command, capture_output=True, text=True, timeout=20), folder

    def bundle(self, video=None):
        result, folder = self.extract(video=video)
        self.assertEqual(result.returncode, 0, result.stderr)
        course.save(folder / 'transcript.json', [dict(start=0.1, end=1.5, text='Original speech')])
        manifest = course.read(folder / 'manifest.json')
        manifest['actual_duration'] = 2
        manifest['evidence_sha256'] = evidence_hashes(folder, manifest, transcript=True)
        course.save(folder / 'manifest.json', manifest)
        tutor_packet.build_packet(folder)
        return folder

    def register(self, folder):
        course.prepare(NS(**vars(self.args), import_prepared=folder))

    def attach_code(self, source):
        course.attach(NS(**vars(self.args), kind='code', source=str(source),
                         none=False, ref=None))

    def test_video_content_is_pinned_at_registration(self):
        self.assertEqual(self.state()['video']['sha256'], course.file_hash(self.video))

    def test_same_filename_and_size_substitution_cannot_resume(self):
        result, folder = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        before = (folder / 'manifest.json').read_bytes()
        self.video.write_bytes(self.b)
        result, _ = self.extract(resume=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not match', result.stderr)
        self.assertEqual((folder / 'manifest.json').read_bytes(), before)

    def test_identical_video_renamed_can_resume(self):
        result, folder = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        renamed = self.root / 'renamed.mp4'
        renamed.write_bytes(self.a)
        result, _ = self.extract(video=renamed, folder=folder, resume=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_frame_interval_is_rejected(self):
        result, _ = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        result, _ = self.extract(resume=True, frame_every='0.25')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('frame interval', result.stderr)

    def test_corrupt_audio_cannot_resume(self):
        result, folder = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        (folder / 'audio.wav').write_bytes(b'Changed audio')
        result, _ = self.extract(resume=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('audio integrity', result.stderr)

    def test_corrupt_frame_cannot_resume(self):
        result, folder = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        next((folder / 'frames').glob('*.jpg')).write_bytes(b'Changed image')
        result, _ = self.extract(resume=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('integrity changed', result.stderr)

    def test_legacy_resume_without_hash_is_rejected(self):
        result, folder = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = course.read(folder / 'manifest.json')
        manifest.pop('source_sha256')
        course.save(folder / 'manifest.json', manifest)
        (folder / 'checkpoint.json').unlink(missing_ok=True)
        result, _ = self.extract(resume=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('prepare this source again', result.stderr)

    def test_different_video_bundle_with_same_filename_cannot_be_imported(self):
        wrong = self.root / 'other/lecture.mp4'
        wrong.parent.mkdir()
        wrong.write_bytes(self.b)
        folder = self.bundle(wrong)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.register(folder)
        self.assertEqual(self.state()['prepared_fragments'], [])

    def test_legacy_import_without_hash_cannot_be_blessed_by_filename(self):
        folder = self.bundle()
        manifest = course.read(folder / 'manifest.json')
        manifest.pop('source_sha256')
        course.save(folder / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.register(folder)
        self.assertEqual(self.state()['prepared_fragments'], [])

    def test_valid_import_and_context_match_pinned_source(self):
        folder = self.bundle()
        self.register(folder)
        result = self.capture(course.context, time=0, window=2)
        self.assertEqual(result['speech'][0]['text'], 'Original speech')
        self.assertEqual(result['sources'][0]['source_sha256'], self.state()['video']['sha256'])
        self.assertEqual(result['sources'][0]['identity_verification'], 'pinned_source_hash_matches')

    def test_modified_transcript_is_rejected_before_import(self):
        folder = self.bundle()
        course.save(folder / 'transcript.json', [dict(start=0.1, end=1.5, text='From another video')])
        with self.assertRaisesRegex(ValueError, 'integrity changed'):
            self.register(folder)
        self.assertEqual(self.state()['prepared_fragments'], [])

    def test_modified_frame_is_rejected_before_import(self):
        folder = self.bundle()
        next((folder / 'frames').glob('*.jpg')).write_bytes(b'Wrong frame')
        with self.assertRaisesRegex(ValueError, 'integrity changed'):
            self.register(folder)

    def test_corruption_during_import_copy_is_rejected_before_publication(self):
        folder = self.bundle()
        original = course.shutil.copyfile
        def corrupted(source, destination, *args, **kw):
            result = original(source, destination, *args, **kw)
            if Path(destination).name == 'transcript.json':
                Path(destination).write_text('[]')
            return result
        with patch.object(course.shutil, 'copyfile', corrupted):
            with self.assertRaisesRegex(ValueError, 'integrity changed'):
                self.register(folder)
        self.assertEqual(self.state()['prepared_fragments'], [])

    def test_modified_packet_is_rejected_by_context(self):
        self.register(self.bundle())
        folder = Path(self.state()['prepared_fragments'][0]['folder'])
        packet = course.read(folder / 'tutor_packet.json')
        packet['speech'][0]['text'] = 'Replaced explanation'
        course.save(folder / 'tutor_packet.json', packet)
        with self.assertRaisesRegex(ValueError, 'Packet integrity'):
            self.capture(course.context, time=0, window=2)

    def test_modified_selected_frame_is_rejected_by_context(self):
        self.register(self.bundle())
        folder = Path(self.state()['prepared_fragments'][0]['folder'])
        next((folder / 'frames').glob('*.jpg')).write_bytes(b'Edited frame')
        with self.assertRaisesRegex(ValueError, 'frame integrity'):
            self.capture(course.context, time=0, window=2)

    def test_replaced_original_cannot_supply_context_or_new_frame(self):
        self.register(self.bundle())
        self.video.write_bytes(self.b)
        for function, kwargs in [(course.context, dict(time=0, window=2)), (course.frame, dict(time=1))]:
            with self.subTest(command=function.__name__), self.assertRaisesRegex(ValueError, 'Video changed'):
                self.capture(function, **kwargs)

    def test_unavailable_original_keeps_verified_cached_context(self):
        self.register(self.bundle())
        self.video.unlink()
        result = self.capture(course.context, time=0, window=2)
        self.assertEqual(result['speech'][0]['text'], 'Original speech')
        self.assertIn('original_video_unavailable', result['warnings'])

    def test_replaced_video_prepare_keeps_original_pin_and_materials(self):
        self.register(self.bundle())
        before = self.state()
        self.video.write_bytes(self.b)
        with self.assertRaisesRegex(ValueError, 'Video changed'):
            course.prepare(NS(**vars(self.args), import_prepared=None))
        after = self.state()
        self.assertEqual(after['video']['sha256'], before['video']['sha256'])
        self.assertEqual(after['prepared_fragments'], before['prepared_fragments'])

    def test_video_change_during_frame_extraction_does_not_publish_image(self):
        original = course.preparation_run
        def mutate_after_frame(command, *args, **kw):
            result = original(command, *args, **kw)
            if '-frames:v' in command:
                self.video.write_bytes(self.b)
            return result
        with patch.object(course, 'preparation_run', mutate_after_frame):
            with self.assertRaisesRegex(ValueError, 'Video changed during frame'):
                self.capture(course.frame, time=1)
        self.assertFalse((self.lesson / 'evidence/frames/000000001000.jpg').exists())

    def test_external_code_link_is_rejected_without_changing_lesson(self):
        source = self.root / 'teacher'
        source.mkdir()
        external = self.root / 'External.java'
        external.write_text('class External {}')
        (source / 'Main.java').symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            self.attach_code(source)
        self.assertEqual(self.state()['code']['status'], 'not_asked')

    def test_internal_code_link_is_also_rejected(self):
        source = self.root / 'teacher'
        source.mkdir()
        (source / 'Original.java').write_text('class Original {}')
        (source / 'Main.java').symlink_to('Original.java')
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            self.attach_code(source)

    def test_code_directory_link_is_rejected(self):
        source = self.root / 'teacher'
        source.mkdir()
        external = self.root / 'external'
        external.mkdir()
        (source / 'src').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            self.attach_code(source)

    def test_normal_code_snapshot_is_read_only_and_independent(self):
        source = self.root / 'Main.java'
        source.write_text('class Main { int value = 1; }')
        self.attach_code(source)
        snapshot = Path(self.state()['code']['snapshot']) / 'Main.java'
        self.assertEqual(snapshot.stat().st_mode & 0o222, 0)
        source.write_text('class Main { int value = 2; }')
        self.assertIn('value = 1', snapshot.read_text())

    def test_editing_snapshot_cannot_silently_change_diff_or_context(self):
        source = self.root / 'Main.java'
        source.write_text('class Main {}')
        self.attach_code(source)
        course.add_lesson(NS(course=self.path, lesson='lesson-02', title='Next', video=str(self.video), order=2))
        course.attach(NS(course=self.path, lesson='lesson-02', kind='code', source=str(source), none=False, ref=None))
        snapshot = Path(self.state()['code']['snapshot']) / 'Main.java'
        snapshot.chmod(0o644)
        snapshot.write_text('class Changed {}')
        with self.assertRaisesRegex(ValueError, 'snapshot integrity changed'):
            self.capture(course.diff_code, previous='lesson-02', file=None)
        with self.assertRaisesRegex(ValueError, 'snapshot integrity changed'):
            self.capture(course.context, time=0, window=2)

    def test_new_code_file_is_detected(self):
        source = self.root / 'Main.java'
        source.write_text('class Main {}')
        self.attach_code(source)
        snapshot = Path(self.state()['code']['snapshot'])
        snapshot.chmod(0o755)
        (snapshot / 'Extra.java').write_text('class Extra {}')
        with self.assertRaisesRegex(ValueError, 'snapshot integrity changed'):
            self.capture(course.context, time=0, window=2)

    def test_legacy_packet_is_not_used_as_a_verified_source(self):
        folder = self.bundle()
        self.register(folder)
        data = self.state()
        data['prepared_fragments'][0].pop('packet_sha256')
        course.save(self.lesson / 'lesson.json', data)
        with self.assertRaisesRegex(ValueError, 'legacy packet'):
            self.capture(course.context, time=0, window=2)

    def test_legacy_packet_is_retained_as_history_after_fresh_preparation(self):
        folder = self.bundle()
        data = self.state()
        data['prepared_fragments'].append(dict(folder='old-unverified', coverage=dict(start=0, end=2)))
        course.save(self.lesson / 'lesson.json', data)
        self.register(folder)
        self.assertEqual(len(self.state()['prepared_fragments']), 1)
        self.assertEqual(self.state()['retired_fragments'][0]['folder'], 'old-unverified')

    def test_video_change_during_extraction_is_rejected(self):
        import media_input
        original = media_input.preparation_run
        def mutate_after_audio(command, *args, **kw):
            result = original(command, *args, **kw)
            if '-ar' in command:
                self.video.write_bytes(self.b)
            return result
        output = self.root / 'changed-during-extraction'
        argv = ['lecture.py', str(self.video), '--output', str(output), '--duration', '2',
                '--frame-every', '0.5', '--prepare-only']
        with patch.object(sys, 'argv', argv), patch.object(media_input, 'preparation_run', mutate_after_audio):
            with self.assertRaises(SystemExit) as error:
                lecture.main()
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((output / 'manifest.json').exists())

    def test_infinite_and_impossible_packet_data_is_rejected_without_hanging(self):
        folder = self.bundle()
        manifest = course.read(folder / 'manifest.json')
        manifest['actual_duration'] = math.inf
        course.save(folder / 'manifest.json', manifest)
        with self.assertRaises(ValueError):
            tutor_packet.build_packet(folder)
        manifest['actual_duration'] = 2
        course.save(folder / 'manifest.json', manifest)
        course.save(folder / 'transcript.json', [dict(start=999, end=-5, text='Impossible')])
        with self.assertRaises(ValueError):
            tutor_packet.build_packet(folder)

    def test_wrong_duration_types_are_rejected(self):
        folder = self.bundle()
        original = course.read(folder / 'manifest.json')
        for duration in ['2', True, None]:
            manifest = dict(original, actual_duration=duration)
            course.save(folder / 'manifest.json', manifest)
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                tutor_packet.build_packet(folder)

    def attach_git_fixture(self, linked=False):
        repository = self.root / 'repository'
        subprocess.run(['git', 'init', '-q', str(repository)], check=True, capture_output=True)
        if linked:
            (repository / 'Main.java').symlink_to('../external.java')
        else:
            (repository / 'Main.java').write_text('class Main {}')
        subprocess.run(['git', '-C', str(repository), 'add', 'Main.java'], check=True, capture_output=True)
        hooks = self.root / 'empty-hooks'
        hooks.mkdir()
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', '-c', f'core.hooksPath={hooks}',
            'commit', '-qm', 'Fixture'], check=True, capture_output=True)
        commit = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
        config = self.root / 'git-fixture.config'
        config.write_text(f'[url "{repository.as_uri()}"]\n insteadOf = https://github.com/audit/source\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config), 'GIT_CONFIG_NOSYSTEM': '1'}):
            self.attach_code('https://github.com/audit/source')
        return commit

    def test_real_git_clone_retains_commit_and_seals_plain_files(self):
        commit = self.attach_git_fixture()
        code = self.state()['code']
        self.assertEqual(code['origin']['commit'], commit)
        self.assertEqual(set(code['inventory']), {'Main.java'})
        self.assertFalse((Path(code['snapshot']) / '.git').exists())
        self.assertEqual((Path(code['snapshot']) / 'Main.java').stat().st_mode & 0o222, 0)

    def test_real_git_clone_with_symlink_is_not_registered(self):
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            self.attach_git_fixture(linked=True)
        self.assertEqual(self.state()['code']['status'], 'not_asked')


if __name__ == '__main__':
    unittest.main()
