"""Publication must never write or recursively clean a directory substituted by a peer."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import publication
import safe_material_import as safe
import test_publication
import test_context
from types import SimpleNamespace as NS
from atomic_files import atomic_json
from source_integrity import file_hash


class PublicationFixture(unittest.TestCase):
    def setUp(self):
        fixture=test_context.ContextRegression();fixture.setUp();self.addCleanup(fixture.tearDown)
        working=fixture.add('working',0,60,[(1,2,'Example')])
        (working/'audio.wav').write_bytes(b'fixture audio')
        manifest=publication.json.loads((working/'manifest.json').read_text())
        manifest['audio_sha256']=file_hash(working/'audio.wav');atomic_json(working/'manifest.json',manifest)
        atomic_json(working/'checkpoint.json',dict(status='complete',stages=dict(packet=dict(sha256=file_hash(working/'tutor_packet.json')))))
        self.f=NS(fixture=fixture,working=working,prepared=fixture.directory/'publications',checksum=file_hash(fixture.video))
        self.foreign=self.f.fixture.directory/'foreign';self.foreign.mkdir()
        (self.foreign/'manifest.json').write_bytes(b'FOREIGN MANIFEST')
        (self.foreign/'.git').mkdir();(self.foreign/'.git/keep').write_bytes(b'FOREIGN GIT')
        self.swaps=0

    def snapshot(self,path):
        return {str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()}

    def publish(self):
        return publication.publish_job(self.f.working,self.f.prepared,'new-job')

    def swap(self,path,symlink=False):
        self.swaps+=1;self.replaced=Path(path)
        code="from pathlib import Path;import sys;p=Path(sys.argv[1]);q=Path(sys.argv[2]);p.rename(p.with_name(p.name+'-owned'));"
        code+="p.symlink_to(q,target_is_directory=True)" if symlink else "q.rename(p)"
        subprocess.run([sys.executable,'-B','-c',code,str(path),str(self.foreign)],check=True,capture_output=True,timeout=10)

    def assert_preserved(self,call):
        before=self.snapshot(self.foreign);target=None;error=None
        with contextlib.redirect_stderr(io.StringIO()):
            try:target=call()
            except (ValueError,OSError) as caught:error=caught
        self.assertEqual(self.swaps,1,'The real second-process substitution must execute')
        self.assertEqual(self.snapshot(target or self.replaced),before)
        self.assertIsNotNone(error,'A substituted publication must be refused')


@unittest.skipUnless(os.name=='posix','Descriptor publication requires native POSIX; Windows refusal tested separately')
class PublicationSafety(PublicationFixture):
    def test_destination_swap_never_overwrites_foreign_manifest(self):
        copy=publication.shutil.copyfile;anchored=safe.Attempt.copy
        def old(src,dst,*a,**kw):
            if Path(dst).name=='manifest.json' and not self.swaps:self.swap(Path(dst).parent)
            return copy(src,dst,*a,**kw)
        def new(attempt,src,parent,name,*a,**kw):
            if name=='manifest.json' and not self.swaps:self.swap(Path(kw['destination']).parent)
            return anchored(attempt,src,parent,name,*a,**kw)
        with patch.object(publication.shutil,'copyfile',old),patch.object(safe.Attempt,'copy',new):
            self.assert_preserved(self.publish)

    def test_failure_after_copy_never_recursively_removes_foreign_git(self):
        copy=publication.shutil.copyfile;anchored=safe.Attempt.copy
        def old(src,dst,*a,**kw):
            result=copy(src,dst,*a,**kw)
            if Path(dst).name=='manifest.json' and not self.swaps:
                self.swap(Path(dst).parent);raise OSError('controlled failure after copy')
            return result
        def new(attempt,src,parent,name,*a,**kw):
            result=anchored(attempt,src,parent,name,*a,**kw)
            if name=='manifest.json' and not self.swaps:
                self.swap(Path(kw['destination']).parent);raise OSError('controlled failure after copy')
            return result
        with patch.object(publication.shutil,'copyfile',old),patch.object(safe.Attempt,'copy',new):
            self.assert_preserved(self.publish)

    def test_symlink_destination_swap_is_refused_without_foreign_writes(self):
        copy=safe.Attempt.copy
        def copying(attempt,src,parent,name,*a,**kw):
            if name=='manifest.json' and not self.swaps:self.swap(Path(kw['destination']).parent,True)
            return copy(attempt,src,parent,name,*a,**kw)
        with patch.object(safe.Attempt,'copy',copying):self.assert_preserved(self.publish)

    def test_parent_swap_never_redirects_writes_or_cleanup(self):
        copy=safe.Attempt.copy
        def copying(attempt,src,parent,name,*a,**kw):
            if name=='manifest.json' and not self.swaps:self.swap(self.f.prepared)
            return copy(attempt,src,parent,name,*a,**kw)
        with patch.object(safe.Attempt,'copy',copying):self.assert_preserved(self.publish)

    def test_swap_during_streaming_never_changes_foreign_files(self):
        from test_local_import_safety import descriptor_path
        original=os.read
        def reading(fd,size):
            value=original(fd,size)
            if not self.swaps and descriptor_path(fd)==self.f.working/'manifest.json':
                self.swap(next(self.f.prepared.glob('v4-new-job*')))
            return value
        with patch.object(os,'read',reading):self.assert_preserved(self.publish)

    def test_packet_generation_does_not_write_to_replaced_root(self):
        for name in ('manifest.json','transcript.json','audio.wav'):
            (self.foreign/name).write_bytes((self.f.working/name).read_bytes())
        (self.foreign/'frames').mkdir();(self.foreign/'TUTOR_RULES.md').write_bytes(b'FOREIGN RULES')
        build=publication.build_packet
        def generating(folder,*a,**kw):
            if not self.swaps:self.swap(folder)
            return build(folder,*a,**kw)
        with patch.object(publication,'build_packet',generating):self.assert_preserved(self.publish)

    def test_symlink_cache_is_not_reused_or_modified(self):
        target=self.publish();before=self.snapshot(target)
        alias=self.f.prepared/'alias';alias.symlink_to(target,target_is_directory=True)
        with self.assertRaises((ValueError,OSError)):
            publication.publish_job(self.f.working,self.f.prepared,'new-job',preferred=alias)
        self.assertEqual(self.snapshot(target),before);self.assertTrue(alias.is_symlink())

    def test_marker_creation_never_overwrites_injected_file(self):
        original=safe.secure.new_file;injected=[]
        def created(parent,name):
            if name=='publication.json':
                fd=original(parent,name)
                with os.fdopen(fd,'wb') as stream:stream.write(b'FOREIGN MARKER')
                from test_local_import_safety import descriptor_path
                injected.append(descriptor_path(parent)/name)
            return original(parent,name)
        with patch.object(safe.secure,'new_file',created),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError):self.publish()
        self.assertEqual(injected[0].read_bytes(),b'FOREIGN MARKER')
        self.assertFalse(publication.valid_publication(injected[0].parent,'new-job',self.f.checksum))

    def test_existing_final_folder_is_preserved_and_alternative_is_used(self):
        destination=self.f.prepared/'v4-new-job';destination.parent.mkdir()
        self.foreign.rename(destination);before=self.snapshot(destination)
        result=self.publish()
        self.assertNotEqual(result,destination);self.assertEqual(self.snapshot(destination),before)
        self.assertTrue(publication.valid_publication(result,'new-job',self.f.checksum))

    def test_final_name_collision_at_exclusive_creation_never_replaces_peer(self):
        move=safe.move_exclusive;seen=[]
        def moved(src,name,parent,leaf):
            if leaf=='v4-new-job':
                from test_local_import_safety import descriptor_path
                destination=descriptor_path(parent)/leaf
                self.foreign.rename(destination);seen.append(destination)
            return move(src,name,parent,leaf)
        before=self.snapshot(self.foreign)
        with patch.object(safe,'move_exclusive',moved),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError):self.publish()
        self.assertTrue(seen);self.assertEqual(self.snapshot(seen[0]),before)

    def test_success_and_reuse_preserve_working_and_student_answers(self):
        answers=dict(status='learning',learner_answers=[dict(topic='Loops',answer='Independent answer')])
        atomic_json(self.f.working/'progress.json',answers)
        before=self.snapshot(self.f.working);target=self.publish()
        after=self.snapshot(self.f.working)
        self.assertTrue(all(after[name]==value for name,value in before.items()))
        self.assertLessEqual(set(after)-set(before),{'.processing.lock'})
        self.assertEqual((target/'progress.json').read_bytes(),(self.f.working/'progress.json').read_bytes())
        answers['learner_answers'].append(dict(topic='Methods',answer='Later answer'))
        atomic_json(target/'progress.json',answers);published=self.snapshot(target)
        self.assertEqual(publication.publish_job(self.f.working,self.f.prepared,'new-job',preferred=target),target)
        self.assertEqual(self.snapshot(target),published)
        self.assertFalse(list(self.f.prepared.glob('.pending-*')))

    def test_keyboard_interrupt_preserves_partial_directory_and_allows_retry(self):
        copied=safe.Attempt.copy;errors=io.StringIO();fds=[];opened=safe.secure.new_file
        def created(*a):
            fd=opened(*a);fds.append(fd);return fd
        def interrupt(*a,**kw):
            result=copied(*a,**kw)
            if a[3]=='manifest.json':raise KeyboardInterrupt('stop publication')
            return result
        with patch.object(safe.Attempt,'copy',interrupt),patch.object(safe.secure,'new_file',created),contextlib.redirect_stderr(errors):
            with self.assertRaises(KeyboardInterrupt):self.publish()
        self.assertIn('preserved',errors.getvalue())
        for fd in fds:
            with self.assertRaises(OSError):os.fstat(fd)
        partial=list(self.f.prepared.glob('v4-*'));self.assertEqual(len(partial),1)
        self.assertFalse(publication.valid_publication(partial[0],'new-job',self.f.checksum))
        result=self.publish();self.assertNotEqual(result,partial[0])
        self.assertTrue(publication.valid_publication(result,'new-job',self.f.checksum))

    def child(self,gate):
        code=r'''import publication,safe_material_import as safe,sys
from pathlib import Path
if sys.argv[4]=='copy':
 original=safe.Attempt.copy
 def hook(self,source,parent,name,*a,**kw):
  value=original(self,source,parent,name,*a,**kw)
  if name=='manifest.json':print('ready',flush=True);sys.stdin.readline()
  return value
 safe.Attempt.copy=hook
else:
 from contextlib import contextmanager
 original=publication.file_lock
 @contextmanager
 def lock(path,*a,**kw):
  if path.name.endswith('.publish.lock'):print('ready',flush=True);sys.stdin.readline()
  with original(path,*a,**kw) as stream:yield stream
 publication.file_lock=lock
result=publication.publish_job(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3])
print(str(result),flush=True)
'''
        return subprocess.Popen([sys.executable,'-B','-c',code,str(self.f.working),str(self.f.prepared),'new-job',gate],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ,PYTHONPATH=str(Path(publication.__file__).parent)))

    def close_child(self,p):
        if p.poll() is None:p.kill();p.wait()
        for stream in (p.stdin,p.stdout,p.stderr):stream.close()

    def test_killed_process_leaves_no_valid_publication_and_retry_succeeds(self):
        p=self.child('copy')
        try:
            self.assertEqual(p.stdout.readline().strip(),'ready');p.kill();p.communicate(timeout=10)
        finally:self.close_child(p)
        partial=list(self.f.prepared.glob('v4-*'));self.assertEqual(len(partial),1)
        self.assertFalse(publication.valid_publication(partial[0],'new-job',self.f.checksum))
        result=self.publish();self.assertNotEqual(result,partial[0])
        self.assertTrue(publication.valid_publication(result,'new-job',self.f.checksum))

    def test_two_processes_serialize_and_reuse_one_complete_publication(self):
        # Independent working folders avoid serializing at the first job lock:
        # both contenders reach the publication lock's controlled barrier.
        import shutil
        other=self.f.fixture.directory/'other-working';shutil.copytree(self.f.working,other)
        first=self.child('publish');self.assertEqual(first.stdout.readline().strip(),'ready')
        saved=self.f.working;self.f.working=other
        try:second=self.child('publish')
        finally:self.f.working=saved
        try:
            self.assertEqual(second.stdout.readline().strip(),'ready')
            for p in (first,second):p.stdin.write('go\n');p.stdin.flush()
            paths=[]
            for p in (first,second):
                output,error=p.communicate(timeout=20);self.assertEqual(p.returncode,0,error);paths.append(output.strip())
            self.assertEqual(paths[0],paths[1]);self.assertTrue(publication.valid_publication(Path(paths[0]),'new-job',self.f.checksum))
            self.assertEqual(len(list(self.f.prepared.glob('v4-*'))),1)
        finally:
            for p in (first,second):self.close_child(p)


class PublicationRefusal(PublicationFixture):
    def test_unavailable_primitives_refuse_without_creating_directories(self):
        before=self.snapshot(self.f.fixture.directory)
        with patch.object(safe.secure,'supported',return_value=False):
            with self.assertRaisesRegex(ValueError,'Safe job publication is unavailable'):self.publish()
        self.assertEqual(self.snapshot(self.f.fixture.directory),before)

    def test_keys_cannot_introduce_directory_traversal(self):
        before=self.snapshot(self.f.fixture.directory)
        for key in ('../escape','x/y','x\\y','',None):
            with self.subTest(key=key),self.assertRaises(ValueError):publication.publish_job(self.f.working,self.f.prepared,key)
        self.assertEqual(self.snapshot(self.f.fixture.directory),before)
