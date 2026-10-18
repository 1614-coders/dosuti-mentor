"""Observable workflow invariants: persistence, separation, source preservation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/course.py'


class CourseWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.course = self.directory / 'course'
        self.run_cli('init', '--title', 'Java project')
        self.run_cli('add-lesson', '--lesson', 'lesson-01', '--title', 'First', '--video', 'not-downloaded.mp4')

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args):
        return [sys.executable, str(SCRIPT), args[0], '--course', str(self.course), *args[1:]]

    def run_cli(self, *args):
        result = subprocess.run(self.command(*args), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_progress_is_shared_by_chats_and_separate_by_learner(self):
        for chat in ['chat-one', 'chat-two']:
            self.run_cli('bind', '--lesson', 'lesson-01', '--chat', chat, '--profile', 'timofey')
        processes = [subprocess.Popen(self.command('progress', '--lesson', 'lesson-01',
                     '--profile', 'timofey', '--topic', topic, '--stage', 'understood',
                     '--details', 'Answered independently'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for topic in ['HashSet', 'HashMap']]
        for process in processes:
            output, error = process.communicate()
            self.assertEqual(process.returncode, 0, error)
        state = json.loads(self.run_cli('status', '--profile', 'timofey'))
        self.assertEqual(len(state['chat_bindings']), 2)
        self.assertEqual({x['topic'] for x in state['progress']}, {'HashSet', 'HashMap'})
        friend = json.loads(self.run_cli('status', '--profile', 'friend'))
        self.assertEqual(friend['progress'], [])
        self.assertEqual(friend['chat_bindings'], [])

    def test_status_exposes_worker_stage_without_rewriting_lesson(self):
        meta = self.course / 'lessons/lesson-01/lesson.json'
        lesson = json.loads(meta.read_text())
        lesson['processing_jobs'] = {'base': {'job_key': 'example', 'status': 'pending'}}
        meta.write_text(json.dumps(lesson))
        working = meta.parent / '.jobs/example'
        working.mkdir(parents=True)
        (working / '.processing.lock').touch()
        (working / 'processing-status.json').write_text(json.dumps(dict(
            schema='processing_status_v1', stage='frames', status='running',
            label='Извлекаю кадры', started_at=1, elapsed_seconds=3)))
        before = meta.read_bytes()
        result = json.loads(self.run_cli('status'))
        status = result['lessons'][0]['processing_jobs']['base']['processing_status']
        self.assertEqual((status['status'], status['stage']), ('interrupted', 'frames'))
        self.assertEqual(meta.read_bytes(), before)

    def test_code_snapshots_preserve_lecture_versions(self):
        teacher = self.directory / 'teacher'
        teacher.mkdir()
        source = teacher / 'Main.java'
        source.write_text('class Main { int value = 1; }\n')
        self.run_cli('attach', '--lesson', 'lesson-01', '--kind', 'code', '--source', str(teacher))
        source.write_text('class Main { int value = 2; }\n')
        self.run_cli('add-lesson', '--lesson', 'lesson-02', '--title', 'Second', '--video', 'next.mp4', '--order', '2')
        self.run_cli('attach', '--lesson', 'lesson-02', '--kind', 'code', '--source', str(teacher))
        state = json.loads(self.run_cli('status'))
        snapshot = Path(state['lessons'][0]['code']['snapshot'])
        self.assertIn('value = 1', (snapshot/'Main.java').read_text())
        diff = json.loads(self.run_cli('diff-code', '--previous', 'lesson-01', '--lesson', 'lesson-02'))
        self.assertEqual(diff['lecture_match'], 'unverified')
        self.assertIn('+class Main { int value = 2;', diff['changes'][0]['diff'])

    def test_absent_materials_differ_from_not_asked(self):
        state = json.loads(self.run_cli('status'))
        self.assertEqual(state['lessons'][0]['homework']['status'], 'not_asked')
        self.run_cli('attach', '--lesson', 'lesson-01', '--kind', 'homework', '--none')
        self.run_cli('attach', '--lesson', 'lesson-01', '--kind', 'resources', '--source', 'https://example.com/article')
        state = json.loads(self.run_cli('status'))
        self.assertEqual(state['lessons'][0]['homework']['status'], 'absent')
        self.assertEqual(state['lessons'][0]['resources']['items'][0]['status'], 'registered_not_read')

    def test_code_archive_cannot_escape_snapshot(self):
        archive = self.directory / 'code.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../../outside.java', 'bad')
        result = subprocess.run(self.command('attach', '--lesson', 'lesson-01', '--kind', 'code', '--source', str(archive)), capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.course/'lessons/lesson-01/outside.java').exists())


if __name__ == '__main__':
    unittest.main()
