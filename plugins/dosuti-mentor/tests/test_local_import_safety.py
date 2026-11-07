"""Ordinary material writes must not follow destinations replaced by another process."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import course
import safe_material_import as safe
import test_persistence


def descriptor_path(fd):
    if sys.platform == 'darwin':
        import fcntl
        return Path(fcntl.fcntl(fd, 50, bytes(1024)).split(b'\0', 1)[0].decode())
    return Path(os.readlink('/proc/self/fd/' + str(fd)))


class LocalImportFixture(unittest.TestCase):
    def setUp(self):
        self.f = test_persistence.PersistenceRegression(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.lesson = self.f.path.resolve() / 'lessons/lesson-01'
        self.source = self.f.directory / 'input'; self.source.mkdir()
        self.file = self.source / 'victim.py'; self.file.write_bytes(b'INCOMING CODE\n')
        self.foreign = self.f.directory / 'foreign'; self.foreign.mkdir()
        (self.foreign / 'victim.py').write_bytes(b'FOREIGN CODE\n')
        (self.foreign / '.git').mkdir(); (self.foreign / '.git/keep').write_bytes(b'FOREIGN GIT\n')
        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(self.f.args(topic='existing', stage='explained', details='Actual old course progress'))
        self.progress = self.f.path / 'learners/default/progress.sqlite3'
        self.previous_progress = self.progress.read_bytes()
        self.swaps = 0

    def attach(self, kind, source=None):
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(self.f.args(kind=kind, source=str(source or self.file), none=False,
                                     ref=None, resource_type='file'))

    def snapshot(self, directory):
        return {str(p.relative_to(directory)): p.read_bytes()
                for p in directory.rglob('*') if p.is_file()}

    def swap(self, destination, replacement=None, *, leaf=False, rename=False):
        self.swaps += 1
        code = ('from pathlib import Path; import sys; p=Path(sys.argv[1]); q=Path(sys.argv[2]); '
                'p.rename(p.with_name(p.name+"-owned")); '
                + ('q.rename(p)' if rename else 'p.symlink_to(q, target_is_directory=' + str(not leaf) + ')'))
        subprocess.run([sys.executable, '-B', '-c', code, str(destination), str(replacement or self.foreign)],
                       check=True, capture_output=True, timeout=10)

    def assert_rejected_without_damage(self, callback):
        registry = (self.lesson / 'lesson.json').read_bytes(); foreign = self.snapshot(self.foreign)
        outcome = None
        with contextlib.redirect_stderr(io.StringIO()):
            try: callback()
            except (ValueError, OSError) as error: outcome = error
        self.assertEqual(self.swaps, 1, 'The real second-process replacement must actually run')
        self.assertEqual(self.snapshot(self.foreign), foreign, 'No foreign file or .git may be modified')
        self.assertIsNotNone(outcome, 'A replaced import must be refused, not registered')
        self.assertEqual((self.lesson / 'lesson.json').read_bytes(), registry)
        self.assertEqual(self.progress.read_bytes(), self.previous_progress)


@unittest.skipUnless(os.name == 'posix', 'Anchored writes are POSIX-only; native Windows refusal has separate coverage')
class LocalImportSafety(LocalImportFixture):
    def root_before_open(self, kind, source=None, level='root', rename=False):
        mkdir = os.mkdir
        def created(path, *args, **kwargs):
            result = mkdir(path, *args, **kwargs)
            candidate = Path(path)
            if kwargs.get('dir_fd') is not None: candidate = descriptor_path(kwargs['dir_fd']) / candidate
            if (not self.swaps and candidate.parent == self.lesson / kind):
                selected = candidate if level == 'root' else candidate.parent
                replacement = self.foreign
                if level == 'parent':
                    replacement = self.f.directory / 'foreign-parent'; replacement.mkdir()
                    self.foreign.rename(replacement / candidate.name)
                    self.foreign = replacement / candidate.name
                self.swap(selected, replacement, rename=rename)
            return result
        move = safe.move_exclusive
        def moved(src, name, parent, leaf):
            result = move(src, name, parent, leaf)
            candidate = descriptor_path(parent) / leaf
            if not self.swaps and candidate.parent == self.lesson / kind:
                selected = candidate if level == 'root' else candidate.parent
                self.swap(selected, rename=rename)
            return result
        with patch.object(safe.secure, 'supported', return_value=True), patch.object(os, 'mkdir', created), patch.object(safe, 'move_exclusive', moved): self.attach(kind, source)

    def test_code_folder_replaced_before_open_preserves_foreign_git(self):
        self.assert_rejected_without_damage(lambda: self.root_before_open('code', self.source))

    def test_code_file_replaced_before_open_preserves_foreign_bytes(self):
        self.assert_rejected_without_damage(lambda: self.root_before_open('code'))

    def leaf_after_creation(self, kind):
        opened = os.open; io_open = io.open
        def intercept(path, *, dir_fd=None):
            if self.swaps: return
            candidate = Path(path)
            if dir_fd is not None: candidate = descriptor_path(dir_fd) / candidate
            if self.lesson / kind not in candidate.parents: return
            self.swap(candidate, self.foreign / 'victim.py', leaf=True)
        def descriptor(file, flags, *args, **kwargs):
            fd = opened(file, flags, *args, **kwargs)
            if flags & os.O_CREAT and flags & os.O_EXCL: intercept(file, dir_fd=kwargs.get('dir_fd'))
            return fd
        def stream(file, mode='r', *args, **kwargs):
            result = io_open(file, mode, *args, **kwargs)
            if not isinstance(file, int) and 'x' in mode: intercept(file)
            return result
        with patch.object(safe.secure, 'supported', return_value=True), patch.object(os, 'open', descriptor), patch.object(io, 'open', stream): self.attach(kind)

    def test_text_homework_leaf_swap_never_overwrites_foreign_file(self):
        self.assert_rejected_without_damage(lambda: self.leaf_after_creation('homework'))

    def test_local_resource_leaf_swap_never_overwrites_foreign_file(self):
        self.assert_rejected_without_damage(lambda: self.leaf_after_creation('resources'))

    def test_code_parent_symlink_swap_preserves_foreign_tree(self):
        # Make the replacement before the baseline snapshot so all foreign paths stay stable.
        mkdir = os.mkdir
        def created(path, *args, **kwargs):
            result = mkdir(path, *args, **kwargs)
            candidate = Path(path)
            if kwargs.get('dir_fd') is not None: candidate = descriptor_path(kwargs['dir_fd']) / candidate
            if not self.swaps and candidate.parent == self.lesson / 'code':
                target_name = candidate.name
                replacement = self.f.directory / 'parent-replacement'; replacement.mkdir()
                # Preserve the foreign directory itself and move it under the replacement.
                self.foreign.rename(replacement / target_name); self.foreign = replacement / target_name
                self.swap(candidate.parent, replacement)
            return result
        before = self.snapshot(self.foreign)
        move = safe.move_exclusive
        def moved(src, name, parent, leaf):
            result = move(src, name, parent, leaf)
            candidate = descriptor_path(parent) / leaf
            if not self.swaps and candidate.parent == self.lesson / 'code': self.swap(candidate.parent)
            return result
        with patch.object(safe.secure, 'supported', return_value=True), patch.object(os, 'mkdir', created), patch.object(safe, 'move_exclusive', moved), contextlib.redirect_stderr(io.StringIO()):
            try: self.attach('code')
            except (ValueError, OSError): pass
        self.assertEqual(self.swaps, 1)
        self.assertEqual(self.snapshot(self.foreign), before)
        self.assertEqual(self.progress.read_bytes(), self.previous_progress)

    def test_root_replaced_by_ordinary_foreign_directory_is_not_adopted(self):
        # Renaming a foreign ordinary directory must be as safe as a symlink replacement.
        before = self.snapshot(self.foreign)
        with contextlib.redirect_stderr(io.StringIO()):
            try: self.root_before_open('code', rename=True)
            except (ValueError, OSError): pass
        self.assertEqual(self.swaps, 1)
        destination = next(p for p in (self.lesson / 'code').iterdir() if not p.name.endswith('-owned'))
        self.assertEqual(self.snapshot(destination), before)
        self.assertEqual(self.progress.read_bytes(), self.previous_progress)

    def test_normal_folder_file_homework_and_resources_preserve_bytes_and_progress(self):
        (self.source / '.git').mkdir(); (self.source / '.git/keep').write_bytes(b'SOURCE GIT')
        self.attach('code', self.source)
        folder = course.read(self.lesson / 'lesson.json')['code']
        self.assertEqual(course.verify_snapshot(folder), 'snapshot_hashes_match')
        self.assertEqual((Path(folder['snapshot']) / 'victim.py').read_bytes(), self.file.read_bytes())
        self.assertFalse((Path(folder['snapshot']) / '.git').exists())
        self.assertEqual((self.source / '.git/keep').read_bytes(), b'SOURCE GIT')
        self.attach('code')
        self.attach('homework')
        self.attach('resources')
        data = course.read(self.lesson / 'lesson.json')
        self.assertEqual(Path(data['homework']['file']).read_bytes(), self.file.read_bytes())
        self.assertEqual(Path(data['resources']['items'][-1]['source']).read_bytes(), self.file.read_bytes())
        self.assertEqual(self.progress.read_bytes(), self.previous_progress)

    def test_source_symlink_is_not_copied(self):
        (self.source / 'link').symlink_to(self.foreign / 'victim.py')
        registry = (self.lesson / 'lesson.json').read_bytes(); foreign = self.snapshot(self.foreign)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises((ValueError, OSError)):
            self.attach('code', self.source)
        self.assertEqual((self.lesson / 'lesson.json').read_bytes(), registry)
        self.assertEqual(self.snapshot(self.foreign), foreign)

    def test_destination_swap_during_streaming_preserves_foreign_data(self):
        original = os.read
        def reading(fd, length):
            block = original(fd, length)
            if not self.swaps and descriptor_path(fd) == self.file:
                target = next((self.lesson / 'code').iterdir())
                self.swap(target)
            return block
        with patch.object(os, 'read', reading):
            self.assert_rejected_without_damage(lambda: self.attach('code'))

    def test_exclusive_file_creation_preserves_foreign_hardlink(self):
        original = safe.secure.new_file
        def injecting(parent, name):
            if name == self.file.name:
                os.link(self.foreign / 'victim.py', name, dst_dir_fd=parent)
            return original(parent, name)
        before = self.snapshot(self.foreign)
        with patch.object(safe.secure, 'new_file', injecting), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError):self.attach('homework')
        self.assertEqual(self.snapshot(self.foreign), before)

    def test_interrupt_closes_output_descriptor_and_retry_keeps_progress(self):
        descriptors=[]; original=safe.secure.new_file
        def created(*args):
            fd=original(*args);descriptors.append(fd);return fd
        registry=(self.lesson/'lesson.json').read_bytes()
        with patch.object(safe.secure,'new_file',created), patch.object(os,'read',side_effect=KeyboardInterrupt('stop')), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):self.attach('homework')
        self.assertTrue(descriptors)
        for fd in descriptors:
            with self.assertRaises(OSError):os.fstat(fd)
        self.assertEqual((self.lesson/'lesson.json').read_bytes(),registry)
        self.attach('homework')
        self.assertEqual(self.progress.read_bytes(),self.previous_progress)

    def test_parent_replaced_during_registry_publication_never_registers_or_removes_foreign_data(self):
        original=safe.secure.publish
        def publishing(*args,**kwargs):
            self.swap(self.lesson/'code')
            return original(*args,**kwargs)
        with patch.object(safe.secure,'publish',publishing):
            self.assert_rejected_without_damage(lambda:self.attach('code'))

    def test_download_writes_only_to_the_opened_descriptor_after_a_swap(self):
        from types import SimpleNamespace as NS
        def downloaded(command,**kwargs):
            self.assertEqual(command[command.index('--output')+1],'-')
            target=next((self.lesson/'materials').iterdir());self.swap(target)
            kwargs['stdout'].write(b'INCOMING CODE\n')
            return NS(returncode=0,stderr='{"url_effective":"https://example.invalid/victim.py"}')
        def attach():
            with patch('preparation_control.run',side_effect=downloaded):
                self.attach('homework','https://example.invalid/victim.py')
        self.assert_rejected_without_damage(attach)

    def test_resource_extraction_leaf_swap_preserves_foreign_bytes(self):
        import material_sources
        self.attach('resources')
        resource=course.read(self.lesson/'lesson.json')['resources']['items'][0]
        original=safe.secure.new_file
        def created(parent,name):
            fd=original(parent,name)
            if name.startswith('text-') and not self.swaps:
                self.swap(descriptor_path(parent)/name,self.foreign/'victim.py',leaf=True)
            return fd
        def extract():
            with patch.object(safe.secure,'new_file',created):
                material_sources.resource(self.f.args(action='extract',resource_id=resource['id'],expected_revision=None,notes=None))
        self.assert_rejected_without_damage(extract)

    def test_pdf_image_worker_uses_pinned_output_even_after_path_replacement(self):
        import pdf_fixtures,pdf_visuals
        from source_integrity import file_hash
        from visual_materials import limits
        raw=self.f.directory/'input.pdf';raw.write_bytes(pdf_fixtures.textbook())
        output=self.f.directory/'images';output.mkdir()
        fd=os.open(output,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        before=self.snapshot(self.foreign)
        try:
            self.swap(output)
            value=pdf_visuals.worker(raw,output,file_hash(raw),[1],limits(),fd)
            self.assertEqual(len(value['images']),1)
            self.assertEqual(self.snapshot(self.foreign),before)
            self.assertTrue((output.with_name(output.name+'-owned')/value['images'][0]['file']).is_file())
        finally:os.close(fd)

    def test_failed_exclusive_directory_publication_has_no_copy_fallback(self):
        import errno
        before=self.snapshot(self.f.path);errors=io.StringIO()
        with patch.object(safe,'move_exclusive',side_effect=OSError(errno.EXDEV,'cross-device')),contextlib.redirect_stderr(errors):
            with self.assertRaises(OSError):self.attach('code')
        self.assertEqual(self.snapshot(self.f.path),before)
        self.assertIn('preserved',errors.getvalue())

    def test_two_independent_processes_merge_without_losing_snapshots(self):
        child = r"""import course,sys,safe_material_import as safe
from pathlib import Path
from types import SimpleNamespace as NS
original=safe.Attempt.publish
def gate(self,*args,**kwargs):
 print('ready',flush=True)
 if sys.stdin.readline().strip()!='go':raise RuntimeError('not released')
 return original(self,*args,**kwargs)
safe.Attempt.publish=gate
course.attach(NS(course=Path(sys.argv[1]),lesson='lesson-01',kind='code',source=sys.argv[2],none=False,ref=None,resource_type='file'))
"""
        env=dict(os.environ,PYTHONPATH=str(Path(course.__file__).parent));processes=[]
        try:
            for _ in range(2):
                process=subprocess.Popen([sys.executable,'-B','-c',child,str(self.f.path),str(self.file)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
                processes.append(process);self.assertEqual(process.stdout.readline().strip(),'ready')
            for process in processes:process.stdin.write('go\n');process.stdin.flush()
            for process in processes:
                _,error=process.communicate(timeout=15);self.assertEqual(process.returncode,0,error)
        finally:
            for process in processes:
                if process.poll() is None:process.kill();process.wait()
                for stream in (process.stdin,process.stdout,process.stderr):stream.close()
        material=course.read(self.lesson/'lesson.json')['code']
        self.assertEqual(len(material['history']),1)
        for row in [material,*material['history']]:self.assertEqual(course.verify_snapshot(row),'snapshot_hashes_match')
        self.assertEqual(self.progress.read_bytes(),self.previous_progress)


class LocalImportRefusal(LocalImportFixture):
    def test_unavailable_primitives_refuse_every_local_material_without_writes(self):
        before=self.snapshot(self.f.path)
        with patch.object(safe.secure,'supported',return_value=False), \
                patch('windows_material_import.Security', side_effect=ValueError(
                    'Safe material import is unavailable: native security primitives unavailable')):
            for kind in ('code','homework','resources'):
                with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'Safe material import is unavailable'):
                    self.attach(kind)
        self.assertEqual(self.snapshot(self.f.path),before)

    def test_windows_policy_has_no_path_based_fallback(self):
        if os.name=='nt':self.assertFalse(safe.secure.supported())
        else:
            with patch.object(safe.secure.os,'name','nt'):self.assertFalse(safe.secure.supported())


if __name__ == '__main__': unittest.main()
