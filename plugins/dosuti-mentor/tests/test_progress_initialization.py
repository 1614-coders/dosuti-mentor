"""Default fragment files must never replace a concurrent, committed learner write."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import atomic_files as af
import tutor_packet


WORKER = '''
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import atomic_files as af
from tutor_packet import initialize_user_files
mode, folder, ready, release = sys.argv[2:]
folder, ready, release = map(Path, (folder, ready, release))
original = af.os.fsync
def gate(fd):
    original(fd)
    if list(folder.glob('progress.json.*.tmp')):
        ready.write_text('staged')
        deadline = time.monotonic() + 15
        while not release.exists():
            if time.monotonic() > deadline: raise TimeoutError('QA barrier timed out')
            time.sleep(.005)
af.os.fsync = gate
if mode == 'init': initialize_user_files(folder)
else:
    payload = dict(learner_answers=[mode + ' actual answer'])
    if mode == 'course':
        from course import save
        save(folder / 'progress.json', payload)
    else: af.atomic_json(folder / 'progress.json', payload)
'''


class ProgressInitialization(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'курс с пробелами'; self.root.mkdir()

    def child(self, mode, folder=None, suffix='one'):
        folder = folder or self.root
        ready, release = self.root / (suffix + '.ready'), self.root / (suffix + '.release')
        process = subprocess.Popen([sys.executable, '-B', '-c', WORKER, str(SCRIPTS),
            mode, str(folder), str(ready), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.kill(); process.wait(timeout=10)
            process.stdout.close(); process.stderr.close()
        self.addCleanup(cleanup)
        return process, ready, release

    def staged(self, child):
        process, ready, _ = child; deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                self.fail('Worker exited before barrier: ' + process.stderr.read().decode())
            if time.monotonic() > deadline: self.fail('Worker did not reach QA barrier')
            time.sleep(.005)

    def finish(self, child):
        process, _, release = child; release.touch()
        _, error = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, error.decode())

    def test_answer_committed_after_absence_check_survives_initializer(self):
        for name in ('progress.json', 'visual_notes.json'):
            with self.subTest(file=name), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                folder = Path(tmp); original = af.os.fsync; written = []
                payload = dict(learner_answers=['real answer'], observations=['real note'])
                def interleave(fd):
                    original(fd)
                    if list(folder.glob(name + '.*.tmp')) and not written:
                        written.append(True)
                        run = subprocess.run([sys.executable, '-B', '-c',
                            'import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                            'from atomic_files import atomic_json; atomic_json(Path(sys.argv[2]),json.loads(sys.argv[3]))',
                            str(SCRIPTS), str(folder / name), json.dumps(payload)],
                            capture_output=True, text=True, timeout=10)
                        self.assertEqual(run.returncode, 0, run.stderr)
                with patch.object(af.os, 'fsync', interleave):
                    tutor_packet.initialize_user_files(folder)
                self.assertEqual(len(written), 1)
                self.assertEqual(json.loads((folder / name).read_text()), payload)
                self.assertFalse(list(folder.glob('*.tmp')))

    def test_two_independent_initializers_publish_complete_json(self):
        children = [self.child('init', suffix=str(i)) for i in range(2)]
        for child in children: self.staged(child)
        self.assertFalse((self.root / 'progress.json').exists())
        for child in children: child[2].touch()
        for child in children: self.finish(child)
        self.assertEqual(json.loads((self.root / 'progress.json').read_text())['learner_answers'], [])
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_atomic_and_course_answers_can_finish_after_initialization(self):
        for mode in ('atomic', 'course'):
            with self.subTest(writer=mode), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                folder = Path(tmp); child = self.child(mode, folder, mode); self.staged(child)
                tutor_packet.initialize_user_files(folder)
                self.assertEqual(json.loads((folder / 'progress.json').read_text())['learner_answers'], [])
                self.finish(child)
                self.assertEqual(json.loads((folder / 'progress.json').read_text())['learner_answers'], [mode + ' actual answer'])

    def test_existing_legacy_files_are_not_rewritten(self):
        values = {'progress.json': b'{"old": ["answer"], "unknown_field": 1}',
                  'visual_notes.json': b'{"legacy_notes": ["note"]}'}
        for name, raw in values.items(): (self.root / name).write_bytes(raw)
        before = {name: ((self.root / name).stat().st_ino, (self.root / name).stat().st_mtime_ns)
                  for name in values}
        tutor_packet.initialize_user_files(self.root)
        for name, raw in values.items():
            self.assertEqual((self.root / name).read_bytes(), raw)
            self.assertEqual(((self.root / name).stat().st_ino, (self.root / name).stat().st_mtime_ns), before[name])

    def test_fsync_error_leaves_no_partial_progress_and_can_retry(self):
        original = af.os.fsync
        def failure(fd):
            if list(self.root.glob('progress.json.*.tmp')): raise OSError('fixture disk failure')
            original(fd)
        with patch.object(af.os, 'fsync', failure):
            with self.assertRaises(OSError): tutor_packet.initialize_user_files(self.root)
        self.assertFalse((self.root / 'progress.json').exists())
        self.assertFalse(list(self.root.glob('*.tmp')))
        tutor_packet.initialize_user_files(self.root)
        self.assertEqual(json.loads((self.root / 'progress.json').read_text())['status'], 'prepared')

    def test_publication_error_does_not_fall_back_to_overwrite(self):
        (self.root / 'visual_notes.json').write_text('{}')
        with patch.object(af.os, 'link', side_effect=OSError('fixture unsupported publication')):
            with self.assertRaises(OSError): tutor_packet.initialize_user_files(self.root)
        self.assertFalse((self.root / 'progress.json').exists())
        self.assertFalse(list(self.root.glob('*.tmp')))
        self.assertEqual((self.root / 'visual_notes.json').read_text(), '{}')

    def test_unsupported_permissions_disk_full_and_io_errors_fail_closed(self):
        for code in (errno.ENOTSUP, errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EIO, errno.EXDEV):
            with self.subTest(errno=code), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                folder = Path(tmp); (folder / 'visual_notes.json').write_text('{}')
                failure = OSError(code, 'QA filesystem refusal')
                with patch.object(af.os, 'link', side_effect=failure):
                    with self.assertRaises(OSError) as caught:
                        tutor_packet.initialize_user_files(folder)
                self.assertEqual(caught.exception.errno, code)
                self.assertFalse((folder / 'progress.json').exists())
                self.assertEqual((folder / 'visual_notes.json').read_text(), '{}')
                self.assertFalse(list(folder.glob('*.tmp')))

    def test_permission_error_after_another_writer_commits_keeps_the_answer(self):
        original = af.os.link; payload = dict(learner_answers=['committed before refusal'])
        def failure(source, target):
            if Path(target).name == 'progress.json':
                af.atomic_json(target, payload)
                raise PermissionError(errno.EACCES, 'QA permission changed')
            return original(source, target)
        with patch.object(af.os, 'link', failure):
            with self.assertRaises(PermissionError): tutor_packet.initialize_user_files(self.root)
        self.assertEqual(json.loads((self.root / 'progress.json').read_text()), payload)
        self.assertFalse(list(self.root.glob('*.tmp')))
        tutor_packet.initialize_user_files(self.root)
        self.assertEqual(json.loads((self.root / 'progress.json').read_text()), payload)

    def test_crash_after_publication_keeps_complete_json_and_allows_new_answer(self):
        program = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import atomic_files as af
from tutor_packet import initialize_user_files
original = af.os.link
def crash_after_link(source, target):
    original(source, target)
    if Path(target).name == 'progress.json': os._exit(77)
af.os.link = crash_after_link
initialize_user_files(Path(sys.argv[2]))
'''
        run = subprocess.run([sys.executable, '-B', '-c', program, str(SCRIPTS), str(self.root)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 77, run.stderr)
        self.assertEqual((self.root / 'progress.json').stat().st_nlink, 2)
        raw = (self.root / 'progress.json').read_bytes()
        self.assertEqual(json.loads(raw)['learner_answers'], [])
        tutor_packet.initialize_user_files(self.root)
        self.assertEqual((self.root / 'progress.json').read_bytes(), raw)
        af.atomic_json(self.root / 'progress.json', dict(learner_answers=['new answer']))
        tutor_packet.initialize_user_files(self.root)
        self.assertEqual(json.loads((self.root / 'progress.json').read_text())['learner_answers'], ['new answer'])

    def test_successful_initialization_removes_only_its_temporary_link(self):
        foreign = self.root / 'progress.json.foreign.tmp'; foreign.write_bytes(b'preserve')
        tutor_packet.initialize_user_files(self.root)
        self.assertEqual((self.root / 'progress.json').stat().st_nlink, 1)
        self.assertEqual((self.root / 'visual_notes.json').stat().st_nlink, 1)
        self.assertEqual(foreign.read_bytes(), b'preserve')
        self.assertEqual(list(self.root.glob('*.tmp')), [foreign])

    def test_killed_initializer_cannot_expose_a_partial_file(self):
        child = self.child('init'); self.staged(child)
        child[0].kill(); child[0].wait(timeout=10)
        self.assertFalse((self.root / 'progress.json').exists())
        # An uncatchable kill may leave a private temp; a restart never adopts it.
        leftovers = {p: p.read_bytes() for p in self.root.glob('*.tmp')}
        tutor_packet.initialize_user_files(self.root)
        for path, raw in leftovers.items(): self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(json.loads((self.root / 'progress.json').read_text())['learner_answers'], [])

    def test_saved_answer_survives_restart_in_another_process(self):
        tutor_packet.initialize_user_files(self.root)
        af.atomic_json(self.root / 'progress.json', dict(learner_answers=['retained']))
        raw = (self.root / 'progress.json').read_bytes()
        run = subprocess.run([sys.executable, '-B', '-c',
            'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'from tutor_packet import initialize_user_files; initialize_user_files(Path(sys.argv[2]))',
            str(SCRIPTS), str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((self.root / 'progress.json').read_bytes(), raw)

    def test_independent_course_does_not_wait_for_a_stalled_initializer(self):
        child = self.child('init'); self.staged(child)
        other = self.root / 'other-course'; other.mkdir()
        run = subprocess.run([sys.executable, '-B', '-c',
            'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'from tutor_packet import initialize_user_files; initialize_user_files(Path(sys.argv[2]))',
            str(SCRIPTS), str(other)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIsNone(child[0].poll())
        self.assertEqual(json.loads((other / 'progress.json').read_text())['status'], 'prepared')
        self.finish(child)


if __name__ == '__main__': unittest.main()
