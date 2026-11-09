"""Failure cleanup is non-destructive even when another process replaces a path."""
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import screen_evidence
import video_sources
from source_integrity import file_hash


class CleanupCases:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.video = self.root / 'input.mp4'
        self.video.write_bytes(b'isolated input; decoder mocked')
        self.foreign = self.root / 'foreign'
        (self.foreign / '.git').mkdir(parents=True)
        (self.foreign / '.git' / 'keep').write_bytes(b'foreign git')
        (self.foreign / 'keep').write_bytes(b'foreign data')
        self.stage = None
        self.replacement = None
        self.warning = io.StringIO()

    @property
    def module(self):
        return screen_evidence if self.component == 'screen' else video_sources

    @property
    def hook(self):
        return 'extract_frames' if self.component == 'screen' else 'fetch_direct'

    def call(self):
        if self.component == 'screen':
            return screen_evidence.scan(self.video, self.root / 'screens', 0, 2, .5, file_hash(self.video))
        return video_sources.acquire(self.root, dict(source='https://example.invalid/input.mp4'))

    def replace(self, path, *, symlink=False):
        self.replacement = path
        code = ('from pathlib import Path;import sys;'
                'p=Path(sys.argv[1]);f=Path(sys.argv[2]);p.rename(p.with_name(p.name+"-owned"));')
        code += 'p.symlink_to(f,target_is_directory=True)' if symlink else 'f.rename(p)'
        subprocess.run([sys.executable, '-B', '-c', code, str(path), str(self.foreign)],
                       check=True, capture_output=True, timeout=10)

    def failure(self, callback=None):
        def injected(source, folder, *args, **kwargs):
            self.stage = folder.parent if self.component == 'screen' else folder
            if callback: callback(self.stage)
            raise OSError('controlled extraction interruption')
        with patch.object(self.module, self.hook, injected), contextlib.redirect_stderr(self.warning):
            with self.assertRaisesRegex(OSError, 'controlled extraction interruption'):
                self.call()

    def assert_foreign_preserved(self):
        destination = self.replacement
        self.assertEqual((destination / '.git' / 'keep').read_bytes(), b'foreign git')
        self.assertEqual((destination / 'keep').read_bytes(), b'foreign data')
        self.assertEqual(sorted(p.relative_to(destination).as_posix() for p in destination.rglob('*')),
                         ['.git', '.git/keep', 'keep'])

    def test_replacement_before_cleanup_preserves_git(self):
        self.failure(self.replace)
        self.assert_foreign_preserved()

    @unittest.skipUnless(os.name == 'posix', 'Native POSIX symlink substitution')
    def test_symlink_before_cleanup_preserves_foreign_files(self):
        self.failure(lambda path: self.replace(path, symlink=True))
        self.assertTrue(self.replacement.is_symlink())
        self.assert_foreign_preserved()

    def test_parent_replacement_before_cleanup_preserves_git(self):
        self.failure(lambda path: self.replace(path.parent))
        self.assert_foreign_preserved()

    def test_replacement_at_cleanup_entry_preserves_git(self):
        original = self.module.preserve_temporary
        def cleanup(path):
            self.replace(path)
            return original(path)
        with patch.object(self.module, 'preserve_temporary', cleanup):
            self.failure()
        self.assert_foreign_preserved()

    def test_replacement_during_warning_preserves_git(self):
        class Sink(io.StringIO):
            def write(sink, text):
                if self.stage is not None and self.replacement is None:
                    self.replace(self.stage)
                return super().write(text)
        self.warning = Sink()
        self.failure()
        self.assert_foreign_preserved()

    def test_interrupted_attempt_is_retained_with_warning_and_retry(self):
        self.failure(lambda path: (path / 'partial').write_bytes(b'partial'))
        self.assertEqual((self.stage / 'partial').read_bytes(), b'partial')
        self.assertIn('preserved', self.warning.getvalue())
        self.failure()
        self.assertFalse((self.root / 'video' / 'download.json').exists())
        self.assertFalse(list((self.root / 'screens').glob('scan-*/completion.json')))

    def child_code(self, block):
        return f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(SCRIPTS)!r})
import screen_evidence,video_sources
from source_integrity import file_hash
root=Path(sys.argv[1]);component=sys.argv[2]
module=screen_evidence if component=='screen' else video_sources
hook='extract_frames' if component=='screen' else 'fetch_direct'
def injected(source,folder,*a,**kw):
 stage=folder.parent if component=='screen' else folder
 (stage/'partial').write_bytes(b'child partial')
 print(str(stage),flush=True)
 {block}
 raise OSError('child controlled interruption')
setattr(module,hook,injected)
try:
 if component=='screen':module.scan(root/'input.mp4',root/'screens',0,2,.5,file_hash(root/'input.mp4'))
 else:module.acquire(root,dict(source='https://example.invalid/input.mp4'))
except OSError:pass
'''

    @unittest.skipUnless(os.name == 'posix', 'SIGKILL test requires POSIX')
    def test_forced_termination_preserves_partial_then_retry(self):
        child = subprocess.Popen([sys.executable, '-B', '-c', self.child_code('sys.stdin.read(1)'),
                                  str(self.root), self.component], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=15), 'Child must reach controlled barrier')
            stage = Path(child.stdout.readline().strip())
            child.kill(); child.communicate(timeout=10)
            self.assertEqual(child.returncode, -signal.SIGKILL)
            self.assertEqual((stage / 'partial').read_bytes(), b'child partial')
            self.failure()
            self.assertEqual((self.foreign / '.git' / 'keep').read_bytes(), b'foreign git')
        finally:
            if child.poll() is None: child.kill()
            child.communicate(timeout=10)

    def test_two_processes_do_not_delete_retained_attempts(self):
        command = [sys.executable, '-B', '-c', self.child_code('pass'), str(self.root), self.component]
        children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(2)]
        try:
            for child in children:
                output, error = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, error)
                self.assertEqual((Path(output.strip()) / 'partial').read_bytes(), b'child partial')
                self.assertIn('preserved', error)
        finally:
            for child in children:
                if child.poll() is None: child.kill()
                child.communicate(timeout=10)

    def test_successful_processing_and_cached_repeat(self):
        def frames(source, folder, *args, **kwargs):
            from PIL import Image
            image=Image.new('RGB', (32, 32), 'white')
            if kwargs.get('storage'):
                with kwargs['storage'].output(folder, 'frame.jpg') as stream:image.save(stream, format='JPEG')
            else:
                folder.mkdir()
                image.save(folder / 'frame.jpg')
            return [dict(file='frame.jpg', approx_seconds=0.0)]
        def download(source, folder, *args, **kwargs):
            target = folder / 'lecture.mp4'; target.write_bytes(b'validated synthetic download')
            return target, 'direct'
        with patch.object(self.module, self.hook, frames if self.component == 'screen' else download), \
                patch.object(video_sources, 'validate_media'), contextlib.redirect_stderr(self.warning):
            first = self.call()
        with patch.object(self.module, self.hook, side_effect=AssertionError('Must reuse verified cache')):
            self.assertEqual(self.call(), first)
        self.assertTrue(first.exists())
        self.assertEqual((self.foreign / '.git' / 'keep').read_bytes(), b'foreign git')


class ScreenCleanupSafety(CleanupCases, unittest.TestCase):
    component = 'screen'


class VideoCleanupSafety(CleanupCases, unittest.TestCase):
    component = 'video'


class RetentionPolicy(unittest.TestCase):
    def test_warning_never_opens_moves_or_deletes_the_diagnostic_path(self):
        from temporary_retention import preserve_temporary
        with contextlib.ExitStack() as stack:
            for name in ('stat', 'lstat', 'open', 'listdir', 'scandir', 'unlink', 'rmdir', 'rename', 'replace'):
                stack.enter_context(patch.object(os, name, side_effect=AssertionError('No filesystem cleanup allowed')))
            output = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            preserve_temporary('/missing/or/replaced/directory')
        self.assertIn('preserved without automatic deletion', output.getvalue())

    def test_policy_has_no_unsafe_platform_fallback(self):
        from temporary_retention import preserve_temporary
        with patch.object(os, 'name', 'nt'), contextlib.redirect_stderr(io.StringIO()) as output:
            preserve_temporary('C:\\potentially-replaced')
        self.assertIn('ownership cannot be guaranteed', output.getvalue())
