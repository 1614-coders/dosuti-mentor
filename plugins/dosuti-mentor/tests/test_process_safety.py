"""Real owned processes, bounded capture and failure cleanup; no user data."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import preparation_control as control


class ProcessSafety(unittest.TestCase):
    def test_capture_limit_is_enforced_on_success_and_live_output(self):
        for code in ("import os;os.write(1,b'x'*200000)",
                     "import os,time\nwhile True: os.write(2,b'x'*65536);time.sleep(.001)"):
            with self.subTest(code=code), patch.object(control, 'MAX_CAPTURE_BYTES', 65536, create=True):
                with self.assertRaisesRegex(RuntimeError, 'output|вывод'):
                    control.run([sys.executable, '-c', code], capture_output=True, timeout=2)

    def test_unicode_bytes_input_and_nonzero_results(self):
        cmd = [sys.executable, '-c', 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());sys.stderr.write("error");sys.exit(7)']
        for text in (True, False):
            data = 'Привет с пробелами\n' if text else b'\x00\xff\n'
            result = control.run(cmd, input=data, capture_output=True, text=text, encoding='utf-8' if text else None, timeout=3)
            self.assertEqual(result.stdout, data)
            self.assertEqual(result.stderr, 'error' if text else b'error')
            self.assertEqual(result.returncode, 7)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            control.run(cmd, input=b'test', capture_output=True, check=True, timeout=3)
        self.assertEqual(caught.exception.output, b'test')

    def test_timeout_stops_descendants_without_tracked_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            beat = Path(folder) / 'heartbeat'
            child = "import pathlib,time,sys\np=pathlib.Path(sys.argv[1])\nwhile p.parent.exists(): p.write_text(str(time.time()));time.sleep(.02)"
            parent = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]);time.sleep(30)'
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    control.run([sys.executable, '-c', parent, child, str(beat)], timeout=3, capture_output=True)
                self.assertTrue(beat.exists())
                first = beat.read_bytes()
                time.sleep(.15)
                self.assertEqual(beat.read_bytes(), first)
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.kill(); unrelated.wait(timeout=3)

    @unittest.skipIf(os.name == 'nt', 'POSIX process groups; Windows uses taskkill for a live owner')
    def test_stop_group_even_when_direct_child_has_exited(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        child.wait(timeout=3)
        with patch.object(control.os, 'killpg') as kill:
            control.stop(child)
        kill.assert_called_once_with(child.pid, control.signal.SIGKILL)

    @unittest.skipIf(os.name == 'nt', 'POSIX group ownership; native Windows job containment is not available')
    def test_successful_parent_cannot_leave_background_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            beat = Path(folder) / 'beat'
            child = "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile p.parent.exists(): p.write_text(str(time.time()));time.sleep(.02)"
            parent = ('import pathlib,subprocess,sys,time\n'
                      'child=subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]])\n'
                      'deadline=time.monotonic()+5\n'
                      'while not pathlib.Path(sys.argv[2]).exists():\n'
                      ' if child.poll() is not None: raise RuntimeError("fixture child exited before ready")\n'
                      ' if time.monotonic()>deadline: raise RuntimeError("fixture readiness timed out")\n'
                      ' time.sleep(.01)')
            with self.assertRaisesRegex(RuntimeError, 'running subprocesses'):
                control.run([sys.executable, '-c', parent, child, str(beat)], capture_output=True, timeout=10)
            self.assertTrue(beat.exists())
            first = beat.read_bytes()
            time.sleep(.15)
            self.assertEqual(beat.read_bytes(), first)

    def test_nested_leaf_stays_owned_by_worker_on_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            beat = Path(folder) / 'nested beat'
            script = Path(folder) / 'worker.py'
            script.write_text("import sys\nsys.path.insert(0,sys.argv[1])\nfrom preparation_control import run\nrun([sys.executable,'-c',sys.argv[2],sys.argv[3]],process_group=False,timeout=20)\n")
            child = "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile p.parent.exists(): p.write_text(str(time.time()));time.sleep(.02)"
            with self.assertRaises(subprocess.TimeoutExpired):
                control.run([sys.executable, str(script), str(Path(control.__file__).parent), child, str(beat)], timeout=3, capture_output=True)
            self.assertTrue(beat.exists())
            first = beat.read_bytes()
            time.sleep(.15)
            self.assertEqual(beat.read_bytes(), first)

    def test_leaf_timeout_does_not_signal_callers_process_group(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            control.run([sys.executable, '-c', 'import time;time.sleep(10)'], process_group=False, timeout=.1, capture_output=True)
        self.assertEqual(control.run([sys.executable, '-c', 'import os;os.write(1,b"retry\\n")'], process_group=False, capture_output=True, timeout=2).stdout, b'retry\n')

    def test_capture_files_close_on_error_timeout_and_repeated_success(self):
        real = control.tempfile.TemporaryFile
        streams = []
        def opened(*args, **kwargs):
            stream = real(*args, **kwargs)
            streams.append(stream)
            return stream
        with patch.object(control.tempfile, 'TemporaryFile', opened):
            for _ in range(2):
                self.assertEqual(control.run([sys.executable, '-c', 'import os;os.write(1,b"ok\\n")'], capture_output=True, timeout=2).stdout, b'ok\n')
                with self.assertRaises(subprocess.TimeoutExpired):
                    control.run([sys.executable, '-c', 'import time;time.sleep(10)'], capture_output=True, timeout=.1)
                with self.assertRaises(subprocess.CalledProcessError):
                    control.run([sys.executable, '-c', 'import sys;sys.exit(9)'], capture_output=True, check=True, timeout=2)
        self.assertTrue(streams)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_pipe_aliases_and_explicit_file_output_remain_supported(self):
        cmd = [sys.executable, '-c', 'import os;os.write(1,b"one");os.write(2,b"two")']
        result = control.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=2)
        self.assertEqual(result.stdout, b'onetwo')
        self.assertIsNone(result.stderr)
        with tempfile.TemporaryFile() as output:
            result = control.run(cmd, stdout=output, stderr=subprocess.DEVNULL, timeout=2)
            output.seek(0)
            self.assertEqual(output.read(), b'one')
            self.assertIsNone(result.stdout)

    def test_large_valid_capture_is_not_truncated(self):
        result = control.run([sys.executable, '-c', 'import os;os.write(1,b"x"*2000000);os.write(2,b"y"*2000000)'], capture_output=True, timeout=3)
        self.assertEqual(result.stdout, b'x' * 2000000)
        self.assertEqual(result.stderr, b'y' * 2000000)

    def test_failed_start_and_capture_conflict_do_not_leave_temporaries(self):
        with self.assertRaises(OSError):
            control.run(['/does-not-exist/дочерний процесс'], capture_output=True, timeout=1)
        with self.assertRaises(ValueError):
            control.run([sys.executable], capture_output=True, stdout=subprocess.PIPE)


if __name__ == '__main__':
    unittest.main()
