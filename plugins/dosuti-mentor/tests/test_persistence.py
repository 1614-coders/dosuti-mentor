"""Regression tests for lost material updates and stale homework assessments."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import course
import tutor_packet
from storage_lock import file_lock


class PersistenceRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'course'
        self.video = self.directory / 'lecture.mp4'
        self.video.write_bytes(b'Fixture video identity')
        with contextlib.redirect_stdout(io.StringIO()):
            course.init(self.args())
            course.add_lesson(self.args())

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **kw):
        values = dict(course=self.path, profile='default', lesson='lesson-01',
                      title='Regression', video=str(self.video), order=1)
        values.update(kw)
        return NS(**values)

    def state(self):
        return course.read(self.path / 'lessons/lesson-01/lesson.json')

    def capture(self, function, **kw):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            function(self.args(**kw))
        return json.loads(output.getvalue())

    def attach(self, kind, source=None, **kw):
        course.attach(self.args(kind=kind, source=str(source) if source else None,
                               none=source is None, ref=None, resource_type='file', **kw))

    def packet(self):
        path = self.directory / 'prepared'
        (path / 'frames').mkdir(parents=True)
        course.save(path / 'manifest.json', dict(source='lecture.mp4', start=0,
                    source_sha256=course.file_hash(self.video),
                    actual_duration=60, requested_duration=60, frames=[]))
        course.save(path / 'transcript.json', [dict(start=1, end=10, text='Teacher')])
        manifest = course.read(path / 'manifest.json')
        manifest['evidence_sha256'] = {'transcript.json': course.file_hash(path / 'transcript.json')}
        course.save(path / 'manifest.json', manifest)
        tutor_packet.build_packet(path)
        return path

    def test_simultaneous_resource_attaches_keep_both(self):
        sources = [self.directory / f'resource-{i}.txt' for i in range(2)]
        for source in sources:
            source.write_text(source.name)
        original = course.shutil.copyfile
        barrier = threading.Barrier(2)

        def delayed(source, destination, *args, **kw):
            result = original(source, destination, *args, **kw)
            barrier.wait(timeout=5)
            return result

        with patch.object(course.shutil, 'copyfile', delayed):
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(self.attach, 'resources', source) for source in sources]
                for future in futures:
                    future.result(timeout=10)
        items = self.state()['resources']['items']
        self.assertEqual(len(items), 2)
        self.assertEqual({Path(item['source']).read_text() for item in items},
                         {source.name for source in sources})

    def test_parallel_cli_processes_keep_all_resources(self):
        processes = [subprocess.Popen([sys.executable, str(SCRIPTS / 'course.py'),
                     'attach', '--course', str(self.path), '--lesson', 'lesson-01',
                     '--kind', 'resources', '--source', f'https://example.com/{i}'],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(8)]
        for process in processes:
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, error)
        items = self.state()['resources']['items']
        self.assertEqual({item['source'] for item in items},
                         {f'https://example.com/{i}' for i in range(8)})

    def prepare_interleaved(self, fail=False):
        source = self.packet()
        ready, proceed = threading.Event(), threading.Event()
        original = course.shutil.copytree

        def delayed(src, dst, *args, **kw):
            if Path(src).resolve() == (source / 'frames').resolve():
                ready.set()
                if not proceed.wait(timeout=5):
                    raise TimeoutError('Test did not release prepare')
                if fail:
                    raise OSError('Simulated extraction failure')
            return original(src, dst, *args, **kw)

        try:
            with patch.object(course.shutil, 'copytree', delayed):
                with ThreadPoolExecutor(1) as pool:
                    future = pool.submit(course.prepare, self.args(import_prepared=source))
                    try:
                        self.assertTrue(ready.wait(timeout=5))
                        self.attach('resources', 'https://example.com/added-during-processing')
                    finally:
                        proceed.set()
                    if fail:
                        with self.assertRaisesRegex(OSError, 'Simulated'):
                            future.result(timeout=10)
                    else:
                        future.result(timeout=10)
        finally:
            proceed.set()
        data = self.state()
        self.assertEqual(len(data['resources']['items']), 1)
        self.assertEqual(len(data['prepared_fragments']), 0 if fail else 1)
        self.assertEqual('last_processing_error' in data, fail)

    def test_prepare_success_keeps_attachment(self):
        self.prepare_interleaved()

    def test_prepare_failure_keeps_attachment(self):
        self.prepare_interleaved(fail=True)

    def save_result(self, profile='default', stage='homework_passed', revision=None):
        course.progress(self.args(profile=profile, topic='Homework', stage=stage,
                        details='Solved original task', homework_revision=revision))

    def test_new_homework_invalidates_all_profiles_but_preserves_history(self):
        homework = self.directory / 'homework.txt'
        homework.write_text('Task one')
        self.attach('homework', homework)
        revision = self.state()['homework'].get('revision')
        self.save_result(revision=revision)
        self.save_result(profile='friend', stage='needs_review', revision=revision)
        homework.write_text('Task two')
        self.attach('homework', homework)
        self.assertNotEqual(self.state()['homework'].get('revision'), revision)
        for profile in ['default', 'friend']:
            status = self.capture(course.status, profile=profile)
            state = status['progress'][0]
            self.assertEqual(state['stage'], 'needs_review')
            self.assertTrue(state['invalidated'])
            self.assertNotIn('Solved original task', state['details'])
            history = self.capture(course.history, profile=profile, limit=100)
            self.assertEqual(history['events'][0]['details'], 'Solved original task')
            self.assertEqual(history['events'][0]['homework_revision'], revision)

    def test_reattaching_identical_homework_keeps_pass(self):
        homework = self.directory / 'homework.txt'
        homework.write_text('Same task')
        self.attach('homework', homework)
        revision = self.state()['homework'].get('revision')
        self.save_result(revision=revision)
        self.attach('homework', homework)
        state = self.capture(course.status)['progress'][0]
        self.assertEqual(state['stage'], 'homework_passed')
        self.assertFalse(state['invalidated'])

    def test_old_chat_cannot_mark_new_homework_passed(self):
        homework = self.directory / 'homework.txt'
        homework.write_text('Old task')
        self.attach('homework', homework)
        revision = self.state()['homework'].get('revision')
        homework.write_text('New task')
        self.attach('homework', homework)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.save_result(revision=revision)
        self.assertEqual(self.capture(course.status)['progress'], [])

    def test_unversioned_result_cannot_overwrite_homework_assessment(self):
        homework = self.directory / 'homework.txt'
        homework.write_text('Task')
        self.attach('homework', homework)
        self.save_result(revision=self.state()['homework'].get('revision'))
        with self.assertRaisesRegex(ValueError, 'homework-revision'):
            self.save_result(stage='practice_checked')
        self.assertEqual(self.capture(course.status)['progress'][0]['stage'], 'homework_passed')

    def test_context_also_hides_obsolete_pass(self):
        homework = self.directory / 'homework.txt'
        homework.write_text('Task one')
        self.attach('homework', homework)
        self.save_result(revision=self.state()['homework'].get('revision'))
        course.prepare(self.args(import_prepared=self.packet()))
        homework.write_text('Task two')
        self.attach('homework', homework)
        state = self.capture(course.context, time=0, window=10)['learner_progress'][0]
        self.assertEqual(state['stage'], 'needs_review')
        self.assertTrue(state['invalidated'])

    def test_simultaneous_code_attaches_preserve_both_snapshots(self):
        sources = [self.directory / f'code-{i}' for i in range(2)]
        for i, source in enumerate(sources):
            source.mkdir()
            (source / 'Main.java').write_text(f'class Main {{ int value = {i}; }}')
        original = course.shutil.copytree
        barrier = threading.Barrier(2)

        def delayed(src, dst, *args, **kw):
            result = original(src, dst, *args, **kw)
            barrier.wait(timeout=5)
            return result

        with patch.object(course.shutil, 'copytree', delayed):
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(self.attach, 'code', source) for source in sources]
                for future in futures:
                    future.result(timeout=10)
        code = self.state()['code']
        versions = code['history'] + [code]
        self.assertEqual(len(versions), 2)
        self.assertEqual({(Path(version['snapshot']) / 'Main.java').read_text()
                          for version in versions},
                         {(source / 'Main.java').read_text() for source in sources})

    def test_parallel_first_status_calls_migrate_same_profile(self):
        processes = [subprocess.Popen([sys.executable, str(SCRIPTS / 'course.py'),
                     'status', '--course', str(self.path)], stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE) for _ in range(6)]
        for process in processes:
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(json.loads(output)['progress'], [])

    def test_two_preparations_keep_both_registered_fragments(self):
        source = self.packet()
        original = course.shutil.copytree
        barrier = threading.Barrier(2)

        def delayed(src, dst, *args, **kw):
            result = original(src, dst, *args, **kw)
            barrier.wait(timeout=5)
            return result

        with patch.object(course.shutil, 'copytree', delayed):
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(course.prepare, self.args(import_prepared=source))
                           for _ in range(2)]
                for future in futures:
                    future.result(timeout=10)
        fragments = self.state()['prepared_fragments']
        self.assertEqual(len(fragments), 2)
        self.assertEqual(len({fragment['folder'] for fragment in fragments}), 2)

    def test_lock_timeout_and_killed_owner_do_not_block_later_writes(self):
        path = self.path / 'lessons/lesson-01/.lesson.lock'
        worker = (
            'import sys,time; from pathlib import Path; '
            'sys.path.insert(0,sys.argv[1]); from storage_lock import file_lock; '
            '\nwith file_lock(Path(sys.argv[2])):\n'
            ' print("locked",flush=True)\n time.sleep(20)\n'
        )
        process = subprocess.Popen([sys.executable, '-c', worker, str(SCRIPTS), str(path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            before = self.state()
            with self.assertRaises(TimeoutError):
                with file_lock(path, timeout=0.1):
                    self.fail('Lock acquired while child owns it')
            self.assertEqual(self.state(), before)
        finally:
            process.kill()
            process.communicate(timeout=5)
        self.attach('resources', 'https://example.com/after-crash')
        self.assertEqual(len(self.state()['resources']['items']), 1)

    def test_pass_requires_present_homework_and_explicit_revision(self):
        with self.assertRaises(ValueError):
            self.save_result()
        homework = self.directory / 'homework.txt'
        homework.write_text('Task')
        self.attach('homework', homework)
        with self.assertRaises(ValueError):
            self.save_result()

    def test_removing_homework_invalidates_pass(self):
        homework = self.directory / 'homework.txt'
        homework.write_text('Task')
        self.attach('homework', homework)
        revision = self.state()['homework'].get('revision')
        self.save_result(revision=revision)
        self.attach('homework')
        self.assertTrue(self.capture(course.status)['progress'][0]['invalidated'])

    def test_legacy_progress_is_migrated_without_inventing_revision(self):
        path = self.path / 'learners/default/progress.sqlite3'
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as connection:
            connection.executescript('''
              CREATE TABLE progress (lesson TEXT, topic TEXT, stage TEXT, details TEXT,
                updated TEXT, PRIMARY KEY(lesson,topic));
              CREATE TABLE events (id INTEGER PRIMARY KEY, lesson TEXT, topic TEXT,
                stage TEXT, details TEXT, created TEXT);
              INSERT INTO progress VALUES ('lesson-01','Homework','homework_passed','Old pass','old');
              INSERT INTO events(lesson,topic,stage,details,created)
                VALUES ('lesson-01','Homework','homework_passed','Old pass','old');
            ''')
        state = self.capture(course.status)['progress'][0]
        self.assertEqual(state['stage'], 'needs_review')
        self.assertTrue(state['invalidated'])
        event = self.capture(course.history, limit=100)['events'][0]
        self.assertEqual(event['details'], 'Old pass')
        self.assertIsNone(event['homework_revision'])


if __name__ == '__main__':
    unittest.main()
