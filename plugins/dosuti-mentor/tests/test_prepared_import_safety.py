"""Prepared imports own every output descriptor, even across real directory swaps."""
import contextlib
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
import tutor_packet


class PreparedFixture(unittest.TestCase):
    def setUp(self):
        self.f=test_persistence.PersistenceRegression();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.lesson=self.f.path/'lessons/lesson-01';self.source=self.f.packet().resolve()
        self.foreign=self.f.directory/'foreign';self.foreign.mkdir()
        for name in ('manifest.json','TUTOR_RULES.md','progress.json'):(self.foreign/name).write_bytes(b'FOREIGN '+name.encode())
        (self.foreign/'.git').mkdir();(self.foreign/'.git/keep').write_bytes(b'FOREIGN GIT')
        self.swaps=0

    def run_import(self):
        with contextlib.redirect_stdout(io.StringIO()):course.prepare(self.f.args(import_prepared=self.source))

    def snapshot(self,path):
        return {str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()}

    def swap(self,path,symlink=False):
        self.swaps+=1
        code="from pathlib import Path; import sys; p=Path(sys.argv[1]); q=Path(sys.argv[2]); p.rename(p.with_name(p.name+'-owned')); "
        code+="p.symlink_to(q,target_is_directory=True)" if symlink else "q.rename(p)"
        subprocess.run([sys.executable,'-B','-c',code,str(path),str(self.foreign)],check=True,capture_output=True,timeout=10)
        self.replacement=path

    def assert_foreign_preserved(self,call):
        before=self.snapshot(self.foreign);failure=None
        with contextlib.redirect_stderr(io.StringIO()):
            try:call()
            except (ValueError,OSError):failure=True
        self.assertEqual(self.swaps,1,'The second-process swap must actually execute')
        self.assertEqual(self.snapshot(self.replacement),before)
        self.assertTrue(failure,'A replaced attempt must not be registered')
        self.assertEqual(self.f.state()['prepared_fragments'],[])


@unittest.skipUnless(os.name=='posix','Native descriptor-relative import unavailable; refusal checked separately')
class PreparedImportSafety(PreparedFixture):
    def test_destination_swap_before_manifest_copy_preserves_foreign_manifest(self):
        original=course.shutil.copyfile;copy=safe.Attempt.copy
        def old(src,dst,*a,**kw):
            if Path(dst).name=='manifest.json' and not self.swaps:self.swap(Path(dst).parent)
            return original(src,dst,*a,**kw)
        def new(attempt,src,parent,name,*a,**kw):
            if name=='manifest.json' and not self.swaps:self.swap(Path(kw['destination']).parent)
            return copy(attempt,src,parent,name,*a,**kw)
        with patch.object(course.shutil,'copyfile',old),patch.object(safe.Attempt,'copy',new):
            self.assert_foreign_preserved(self.run_import)

    def test_symlink_swap_before_manifest_copy_preserves_foreign_manifest(self):
        original=course.shutil.copyfile;copy=safe.Attempt.copy
        def old(src,dst,*a,**kw):
            if Path(dst).name=='manifest.json' and not self.swaps:self.swap(Path(dst).parent,True)
            return original(src,dst,*a,**kw)
        def new(attempt,src,parent,name,*a,**kw):
            if name=='manifest.json' and not self.swaps:self.swap(Path(kw['destination']).parent,True)
            return copy(attempt,src,parent,name,*a,**kw)
        with patch.object(course.shutil,'copyfile',old),patch.object(safe.Attempt,'copy',new):
            self.assert_foreign_preserved(self.run_import)

    def test_packet_generation_never_writes_through_a_replaced_directory(self):
        for name in ('manifest.json','transcript.json'):(self.foreign/name).write_bytes((self.source/name).read_bytes())
        original=tutor_packet.build_packet
        def building(folder,*a,**kw):
            if not self.swaps:self.swap(Path(folder))
            return original(folder,*a,**kw)
        with patch.object(tutor_packet,'build_packet',building):self.assert_foreign_preserved(self.run_import)

    def test_parent_directory_swap_never_redirects_manifest_or_cleanup(self):
        copy=safe.Attempt.copy
        def copying(attempt,src,parent,name,*a,**kw):
            if name=='manifest.json' and not self.swaps:self.swap(Path(kw['destination']).parent.parent)
            return copy(attempt,src,parent,name,*a,**kw)
        with patch.object(safe.Attempt,'copy',copying):self.assert_foreign_preserved(self.run_import)

    def test_lesson_parent_swap_does_not_redirect_status_or_failure_metadata(self):
        original=safe.Attempt.copy;before=self.snapshot(self.foreign)
        def copying(attempt,src,parent,name,*a,**kw):
            if name=='manifest.json' and not self.swaps:self.swap(self.lesson)
            return original(attempt,src,parent,name,*a,**kw)
        with patch.object(safe.Attempt,'copy',copying),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):self.run_import()
        self.assertEqual(self.swaps,1);self.assertEqual(self.snapshot(self.lesson),before)
        original_lesson=self.lesson.with_name(self.lesson.name+'-owned')
        self.assertEqual(course.read(original_lesson/'lesson.json')['prepared_fragments'],[])

    def test_corrupt_manifest_is_refused_without_repairing_the_source(self):
        (self.source/'manifest.json').write_bytes(b'[]');before=self.snapshot(self.source)
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaisesRegex(ValueError,'Imported manifest'):
            self.run_import()
        self.assertEqual(self.snapshot(self.source),before)
        self.assertEqual(self.f.state()['prepared_fragments'],[])

    def test_manifest_leaf_symlink_is_not_followed(self):
        original=safe.secure.new_file;foreign=self.foreign/'manifest.json';before=foreign.read_bytes();changed=[]
        def created(parent,name):
            fd=original(parent,name)
            if name=='manifest.json' and not changed:
                from test_local_import_safety import descriptor_path
                target=descriptor_path(parent)/name
                subprocess.run([sys.executable,'-B','-c',"from pathlib import Path;import sys;p=Path(sys.argv[1]);p.rename(p.with_suffix('.owned'));p.symlink_to(Path(sys.argv[2]))",str(target),str(foreign)],check=True,timeout=10)
                changed.append(True)
            return fd
        with patch.object(safe.secure,'new_file',created),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):self.run_import()
        self.assertTrue(changed);self.assertEqual(foreign.read_bytes(),before)
        self.assertEqual(self.f.state()['prepared_fragments'],[])

    def test_swap_during_manifest_streaming_does_not_change_foreign_bytes(self):
        from test_local_import_safety import descriptor_path
        original=os.read
        def reading(fd,size):
            value=original(fd,size)
            if not self.swaps and descriptor_path(fd)==self.source/'manifest.json':
                target=next((self.lesson/'prepared').glob('import-*'));self.swap(target)
            return value
        with patch.object(os,'read',reading):self.assert_foreign_preserved(self.run_import)

    def test_normal_import_preserves_source_and_existing_learning_progress(self):
        course.progress(self.f.args(topic='known',stage='explained',details='Existing learning'))
        progress=self.f.path/'learners/default/progress.sqlite3';before=progress.read_bytes();source=self.snapshot(self.source)
        self.run_import();fragments=self.f.state()['prepared_fragments'];self.assertEqual(len(fragments),1)
        target=Path(fragments[0]['folder'])
        self.assertEqual((target/'manifest.json').read_bytes(),(self.source/'manifest.json').read_bytes())
        self.assertEqual((target/'transcript.json').read_bytes(),(self.source/'transcript.json').read_bytes())
        self.assertEqual(self.snapshot(self.source),source);self.assertEqual(progress.read_bytes(),before)
        self.assertTrue((target/'tutor_context.md').is_file())

    def test_interrupt_preserves_residue_closes_handles_and_retry_succeeds(self):
        copied=safe.Attempt.copy;descriptors=[];created=safe.secure.new_file;errors=io.StringIO()
        def opened(*a):
            fd=created(*a);descriptors.append(fd);return fd
        def interrupt(*a,**kw):
            value=copied(*a,**kw)
            if a[3]=='manifest.json':raise KeyboardInterrupt('stop import')
            return value
        with patch.object(safe.secure,'new_file',opened),patch.object(safe.Attempt,'copy',interrupt),contextlib.redirect_stderr(errors):
            with self.assertRaises(KeyboardInterrupt):self.run_import()
        self.assertEqual(self.f.state()['prepared_fragments'],[]);self.assertIn('preserved',errors.getvalue())
        for fd in descriptors:
            with self.assertRaises(OSError):os.fstat(fd)
        self.run_import();self.assertEqual(len(self.f.state()['prepared_fragments']),1)

    def test_existing_destination_is_not_replaced(self):
        from types import SimpleNamespace as NS
        destination=self.lesson/'prepared'/('import-'+'a'*8);destination.mkdir(parents=True)
        (destination/'manifest.json').write_bytes(b'EXISTING')
        with patch.object(safe.uuid,'uuid4',return_value=NS(hex='a'*32)),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError):self.run_import()
        self.assertEqual((destination/'manifest.json').read_bytes(),b'EXISTING')

    def test_generated_files_cannot_replace_an_injected_foreign_file(self):
        build=tutor_packet.build_packet;before=b'UNOWNED RULES'
        def generating(folder,*a,**kw):
            (folder/'TUTOR_RULES.md').write_bytes(before)
            return build(folder,*a,**kw)
        with patch.object(tutor_packet,'build_packet',generating),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError):self.run_import()
        target=next((self.lesson/'prepared').glob('import-*'))
        self.assertEqual((target/'TUTOR_RULES.md').read_bytes(),before)
        self.assertEqual(self.f.state()['prepared_fragments'],[])

    def test_killed_process_leaves_unregistered_files_and_does_not_block_retry(self):
        import preparation_status
        course.checked_video(self.lesson,self.f.state())
        code=r"""import course,sys,safe_material_import as safe
from pathlib import Path
from types import SimpleNamespace as NS
copy=safe.Attempt.copy
def gate(self,source,parent,name,*a,**kw):
 result=copy(self,source,parent,name,*a,**kw)
 if name=='manifest.json':
  print('paused',flush=True);sys.stdin.readline()
 return result
safe.Attempt.copy=gate
course.prepare(NS(course=Path(sys.argv[1]),lesson='lesson-01',import_prepared=Path(sys.argv[2])))
"""
        env=dict(os.environ,PYTHONPATH=str(Path(course.__file__).parent))
        process=subprocess.Popen([sys.executable,'-B','-c',code,str(self.f.path),str(self.source)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        try:
            for _ in range(20):
                line=process.stdout.readline().strip()
                if line=='paused':break
                self.assertTrue(line,'Child terminated before the controlled interruption')
            self.assertEqual(line,'paused');process.kill();process.communicate(timeout=10)
        finally:
            if process.poll() is None:process.kill();process.wait()
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()
        self.assertEqual(self.f.state()['prepared_fragments'],[])
        self.assertTrue(list((self.lesson/'prepared').glob('import-*')))
        self.assertEqual(preparation_status.describe(self.lesson)['status'],'interrupted')
        self.run_import();self.assertEqual(len(self.f.state()['prepared_fragments']),1)

    def test_two_processes_keep_both_registered_fragments(self):
        course.checked_video(self.lesson,self.f.state())
        code=r'''import course,sys,safe_material_import as safe
from pathlib import Path
from types import SimpleNamespace as NS
publish=safe.Attempt.publish
def gate(self,*a,**kw):
 print('ready',flush=True)
 if sys.stdin.readline().strip()!='go':raise RuntimeError('not released')
 return publish(self,*a,**kw)
safe.Attempt.publish=gate
course.prepare(NS(course=Path(sys.argv[1]),lesson='lesson-01',import_prepared=Path(sys.argv[2])))
'''
        processes=[];env=dict(os.environ,PYTHONPATH=str(Path(course.__file__).parent))
        try:
            for _ in range(2):
                p=subprocess.Popen([sys.executable,'-B','-c',code,str(self.f.path),str(self.source)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
                processes.append(p)
                for _ in range(20):
                    line=p.stdout.readline().strip()
                    if line=='ready':break
                    self.assertTrue(line,'Child terminated before controlled barrier')
                self.assertEqual(line,'ready')
            for p in processes:p.stdin.write('go\n');p.stdin.flush()
            for p in processes:
                _,error=p.communicate(timeout=20);self.assertEqual(p.returncode,0,error)
        finally:
            for p in processes:
                if p.poll() is None:p.kill();p.wait()
                for stream in (p.stdin,p.stdout,p.stderr):stream.close()
        fragments=self.f.state()['prepared_fragments'];self.assertEqual(len(fragments),2)
        self.assertEqual(len({f['folder'] for f in fragments}),2)


class PreparedImportRefusal(PreparedFixture):
    def test_missing_primitives_refuse_without_fallback(self):
        before=self.snapshot(self.f.path)
        with patch.object(safe.secure,'supported',return_value=False):
            with self.assertRaisesRegex(ValueError,'Safe material import is unavailable'):self.run_import()
        self.assertEqual(self.snapshot(self.f.path),before)
