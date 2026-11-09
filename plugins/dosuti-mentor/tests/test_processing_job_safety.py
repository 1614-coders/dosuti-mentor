"""Worker writes and error paths never follow a directory substituted by a peer."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import processing_job
import test_recovery


class WorkerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_recovery.JobRecovery.setUpClass()
    @classmethod
    def tearDownClass(cls):
        test_recovery.JobRecovery.tearDownClass()
    def setUp(self):
        self.f=test_recovery.JobRecovery();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.foreign=self.f.root/'foreign';self.foreign.mkdir()
        (self.foreign/'checkpoint.json').write_bytes(b'FOREIGN CHECKPOINT')
        (self.foreign/'.git').mkdir();(self.foreign/'.git/keep').write_bytes(b'FOREIGN GIT')
        self.swaps=0
    def snapshot(self,path):
        return {str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()}
    def swap(self,path,symlink=False):
        self.swaps+=1;self.replaced=Path(path)
        code="from pathlib import Path;import sys;p=Path(sys.argv[1]);q=Path(sys.argv[2]);p.rename(p.with_name(p.name+'-owned'));"
        code+="p.symlink_to(q,target_is_directory=True)" if symlink else "q.rename(p)"
        subprocess.run([sys.executable,'-B','-c',code,str(path),str(self.foreign)],check=True,capture_output=True,timeout=10)
    def assert_preserved(self,call):
        before=self.snapshot(self.foreign);error=None
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            try:call()
            except (ValueError,OSError) as caught:error=caught
        self.assertEqual(self.swaps,1,'The real peer-process substitution must execute')
        self.assertEqual(self.snapshot(self.replaced),before)
        self.assertIsNotNone(error)


@unittest.skipUnless(os.name=='posix','Native POSIX worker; unsupported-platform refusal checked separately')
class WorkerSafety(WorkerFixture):
    def test_checkpoint_root_swap_never_overwrites_foreign_checkpoint(self):
        persist=processing_job.persist
        def changed(folder,state):
            if not self.swaps:self.swap(folder)
            return persist(folder,state)
        with patch.object(processing_job,'persist',changed):self.assert_preserved(self.f.run_job)

    def test_frames_failure_never_deletes_replacement_directory_or_git(self):
        def changed(video,folder,*a,**kw):
            self.swap(folder);raise OSError('controlled frame extraction failure')
        with patch.object(processing_job,'extract_frames',changed):self.assert_preserved(self.f.run_job)

    def test_checkpoint_root_symlink_swap_preserves_foreign_files(self):
        original=processing_job.persist
        def changed(folder,state):
            if not self.swaps:self.swap(folder,True)
            return original(folder,state)
        with patch.object(processing_job,'persist',changed):self.assert_preserved(self.f.run_job)

    def test_parent_swap_never_redirects_metadata_or_cleanup(self):
        self.f.args.output=self.f.root/'parent'/'job'
        original=processing_job.persist
        def changed(folder,state):
            if not self.swaps:self.swap(folder.parent)
            return original(folder,state)
        with patch.object(processing_job,'persist',changed):self.assert_preserved(self.f.run_job)

    def test_ffmpeg_frames_cannot_write_through_a_replaced_folder(self):
        original=processing_job.extract_frames
        def changed(video,folder,*a,**kw):
            self.swap(folder,True)
            return original(video,folder,*a,**kw)
        with patch.object(processing_job,'extract_frames',changed):self.assert_preserved(self.f.run_job)

    def test_checkpoint_leaf_symlink_is_refused_without_touching_target(self):
        original=processing_job.persist;injected=[];foreign=self.foreign/'checkpoint.json';before=foreign.read_bytes()
        def changed(folder,state):
            if not injected:
                (folder/'checkpoint.json').symlink_to(foreign);injected.append(True)
            return original(folder,state)
        with patch.object(processing_job,'persist',changed),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):self.f.run_job()
        self.assertTrue(injected);self.assertEqual(foreign.read_bytes(),before)
        self.assertTrue((self.f.out/'checkpoint.json').is_symlink())

    def test_fd_lock_opens_existing_entry_without_creating_over_it(self):
        from storage_lock import file_lock
        folder=self.f.root/'lock-root';folder.mkdir();lock=folder/'.processing.lock';lock.write_bytes(b'EXISTING LOCK')
        fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);opened=os.open;calls=[]
        def opening(name,flags,*a,**kw):
            if kw.get('dir_fd')==fd:
                calls.append(flags)
                if flags & os.O_CREAT and not flags & os.O_EXCL:raise FileNotFoundError('concurrent create/open race')
            return opened(name,flags,*a,**kw)
        try:
            with patch.object(os,'open',opening):
                with file_lock(lock,directory_fd=fd):pass
        finally:os.close(fd)
        self.assertEqual(lock.read_bytes(),b'EXISTING LOCK')
        self.assertEqual(len(calls),2);self.assertTrue(calls[0]&os.O_EXCL);self.assertFalse(calls[1]&os.O_CREAT)

    def test_checkpoint_leaf_swap_after_binding_never_writes_foreign_inode(self):
        from test_local_import_safety import descriptor_path
        original=os.write;changed=[];foreign=self.foreign/'checkpoint.json';before=foreign.read_bytes()
        def writing(fd,value):
            try:path=descriptor_path(fd)
            except OSError:path=None
            if path==self.f.out/'checkpoint.json' and not changed:
                code="from pathlib import Path;import sys;p=Path(sys.argv[1]);p.rename(p.with_suffix('.owned'));Path(sys.argv[2]).rename(p)"
                subprocess.run([sys.executable,'-B','-c',code,str(path),str(foreign)],check=True,timeout=10);changed.append(True)
            return original(fd,value)
        with patch.object(os,'write',writing),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):self.f.run_job()
        self.assertTrue(changed);self.assertEqual((self.f.out/'checkpoint.json').read_bytes(),before)

    def test_audio_leaf_replacement_never_changes_foreign_bytes(self):
        import safe_zip_import
        from test_local_import_safety import descriptor_path
        original=safe_zip_import.new_file;changed=[];foreign=self.foreign/'checkpoint.json';before=foreign.read_bytes()
        def created(parent,name):
            fd=original(parent,name)
            if name.startswith('audio-') and name.endswith('.staging.wav') and not changed:
                path=descriptor_path(parent)/name
                code="from pathlib import Path;import sys;p=Path(sys.argv[1]);p.rename(p.with_suffix('.owned'));p.symlink_to(Path(sys.argv[2]))"
                subprocess.run([sys.executable,'-B','-c',code,str(path),str(foreign)],check=True,timeout=10);changed.append(True)
            return fd
        with patch.object(safe_zip_import,'new_file',created),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):self.f.run_job()
        self.assertTrue(changed);self.assertEqual(foreign.read_bytes(),before)

    def test_interrupt_retains_residue_and_resume_preserves_answers(self):
        self.f.out.mkdir();progress=self.f.out/'progress.json';progress.write_bytes(b'{"learner_answers":["real answer"]}')
        before=progress.read_bytes()
        with patch.object(processing_job,'extract_frames',side_effect=KeyboardInterrupt('stop')),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):self.f.run_job()
        self.assertEqual(progress.read_bytes(),before)
        self.assertFalse((self.f.out/'publication.json').exists())
        with contextlib.redirect_stdout(io.StringIO()):state=self.f.run_job()
        self.assertEqual(state['status'],'complete');self.assertEqual(progress.read_bytes(),before)

    def test_normal_frames_match_legacy_pixels_and_source_times(self):
        import screen_evidence
        from PIL import Image
        baseline=self.f.root/'baseline'
        original=screen_evidence.extract_frames(self.f.video,baseline,0,2,.5,self.f.ffmpeg,scene_changes=True)
        with contextlib.redirect_stdout(io.StringIO()):state=self.f.run_job()
        actual=state['stages']['frames']['items']
        self.assertEqual([f['approx_seconds'] for f in original],[f['approx_seconds'] for f in actual])
        for left,right in zip(original,actual):
            with Image.open(baseline/left['file']) as old,Image.open(self.f.out/right['file']) as new:
                self.assertEqual(old.convert('RGB').tobytes(),new.convert('RGB').tobytes())

    def test_unrelated_corruption_is_not_healed_or_overwritten(self):
        with contextlib.redirect_stdout(io.StringIO()):self.f.run_job()
        checkpoint=self.f.out/'checkpoint.json';checkpoint.write_bytes(b'not-json')
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'damaged'):self.f.run_job()
        self.assertEqual(checkpoint.read_bytes(),b'not-json')

    def test_corrupt_snapshot_cannot_repair_a_partial_checkpoint(self):
        with contextlib.redirect_stdout(io.StringIO()):self.f.run_job()
        checkpoint=self.f.out/'checkpoint.json';checkpoint.write_bytes(b'{')
        for snapshot in (self.f.out/'.worker-state').glob('checkpoint.json.*.snapshot'):snapshot.write_bytes(b'corrupt')
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'damaged'):self.f.run_job()
        self.assertEqual(checkpoint.read_bytes(),b'{')

    def launch(self,mode):
        code=r'''import sys,processing_job as jobs,test_recovery
from pathlib import Path
from contextlib import contextmanager
video,out,model,ffmpeg,mode=sys.argv[1:]
test_recovery.JobRecovery.a=Path(video).read_bytes()
f=test_recovery.JobRecovery();f.setUp()
f.args.video=Path(video);f.args.output=Path(out);f.args.model=model;f.ffmpeg=ffmpeg
if mode=='torn':
 import os
 from test_local_import_safety import descriptor_path
 original=os.write;paused=[]
 def write(fd,value):
  try:path=descriptor_path(fd)
  except OSError:path=None
  if path==Path(out)/'checkpoint.json' and not paused:
   paused.append(True);n=original(fd,value[:8]);print('ready',flush=True);sys.stdin.readline();return n
  return original(fd,value)
 os.write=write
elif mode=='kill':
 original=jobs.persist
 def gate(folder,state):
  original(folder,state)
  print('ready',flush=True);sys.stdin.readline()
 jobs.persist=gate
else:
 original=jobs.file_lock
 @contextmanager
 def gate(path,*a,**kw):
  if path.name=='.processing.lock':print('ready',flush=True);sys.stdin.readline()
  with original(path,*a,**kw) as stream:yield stream
 jobs.file_lock=gate
try:f.run_job()
finally:f.tearDown()
'''
        return subprocess.Popen([sys.executable,'-B','-c',code,str(self.f.video),str(self.f.out),str(self.f.model),self.f.ffmpeg,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ,PYTHONPATH=str(Path(processing_job.__file__).parent)+os.pathsep+str(Path(test_recovery.__file__).parent)))

    def ready(self,p):
        for _ in range(30):
            line=p.stdout.readline().strip()
            if line=='ready':return
            self.assertTrue(line,'Child exited before the deterministic barrier')
        self.fail('Child did not reach barrier')

    def close_child(self,p):
        if p.poll() is None:p.kill();p.wait()
        for stream in (p.stdin,p.stdout,p.stderr):stream.close()

    def test_killed_worker_releases_lock_and_resume_preserves_progress(self):
        self.f.out.mkdir();progress=self.f.out/'progress.json';progress.write_bytes(b'{"learner_answers":["saved"]}')
        before=progress.read_bytes();p=self.launch('kill')
        try:self.ready(p);p.kill();p.communicate(timeout=10)
        finally:self.close_child(p)
        self.assertEqual(progress.read_bytes(),before)
        with contextlib.redirect_stdout(io.StringIO()):state=self.f.run_job()
        self.assertEqual(state['status'],'complete');self.assertEqual(progress.read_bytes(),before)

    def test_torn_checkpoint_after_sigkill_recovers_without_losing_progress(self):
        self.f.out.mkdir();progress=self.f.out/'progress.json';progress.write_bytes(b'{"learner_answers":["saved"]}')
        before=progress.read_bytes();p=self.launch('torn')
        try:self.ready(p);p.kill();p.communicate(timeout=10)
        finally:self.close_child(p)
        with self.assertRaises(ValueError):processing_job.json.loads((self.f.out/'checkpoint.json').read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):state=self.f.run_job()
        self.assertEqual(state['status'],'complete');self.assertEqual(progress.read_bytes(),before)

    def test_two_processes_serialize_one_job_and_keep_progress(self):
        self.f.out.mkdir();progress=self.f.out/'progress.json';progress.write_bytes(b'{"learner_answers":["saved"]}')
        processes=[];before=progress.read_bytes()
        try:
            for _ in range(2):
                p=self.launch('parallel');processes.append(p);self.ready(p)
            for p in processes:p.stdin.write('go\n');p.stdin.flush()
            for p in processes:
                _,error=p.communicate(timeout=25);self.assertEqual(p.returncode,0,error)
        finally:
            for p in processes:self.close_child(p)
        self.assertEqual(progress.read_bytes(),before)
        checkpoint=processing_job.json.loads((self.f.out/'checkpoint.json').read_text())
        self.assertEqual(checkpoint['status'],'complete')
        self.assertIn('packet',checkpoint['stages'])


class WorkerRefusal(WorkerFixture):
    def test_missing_safe_primitives_refuse_before_writing(self):
        import safe_zip_import
        before=self.snapshot(self.f.root)
        with patch.object(safe_zip_import,'supported',return_value=False):
            with self.assertRaisesRegex(ValueError,'Safe material import is unavailable'):self.f.run_job()
        self.assertEqual(self.snapshot(self.f.root),before)

    def test_snapshot_limit_refuses_without_modifying_progress(self):
        import worker_io
        self.f.out.mkdir();progress=self.f.out/'progress.json';progress.write_bytes(b'{"learner_answers":["saved"]}')
        before=progress.read_bytes()
        with patch.object(worker_io,'MAX_SNAPSHOTS',0),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'snapshot count limit'):self.f.run_job()
        self.assertEqual(progress.read_bytes(),before);self.assertFalse((self.f.out/'checkpoint.json').exists())
