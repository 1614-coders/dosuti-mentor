"""Early setup reports, actual subprocess failures and safe project discovery."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pilot
import pilot_setup as setup
import connection
from atomic_files import atomic_json


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.root=self.base/'Подготовка с пробелами'
        self.video=self.base/'лекция.mp4';self.video.write_bytes(b'QA source')
        self.args=NS(root=self.root,video=self.video,runtime=None,model_path=None,offline=False,
                     model='tiny',seconds=10,start=0,language='ru')
        self.commands=[]
    def fake(self,command,timeout):
        self.commands.append(command)
        if '--worker' in command:
            worker=command[command.index('--worker')+1]
            if worker=='inventory':
                atomic_json(Path(command[command.index('--output')+1]),dict(packages={'QA':'1'},system='QA'))
            elif worker=='smoke':
                atomic_json(Path(command[command.index('--output')+1]),dict(status='passed',note='synthetic worker only'))
        return dict(returncode=0,timed_out=False,output='QA command finished')
    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()),patch.object(setup,'run',self.fake):return setup.prepare(self.args)
    def test_complete_flow_and_project_lookup_without_pilot_path(self):
        result=self.prepare();root=self.root/'run'
        found=pilot.project_status(self.root/'project')
        self.assertTrue(found['connected']);self.assertEqual(found['pilot'],str(root))
        self.assertEqual(found['run']['profile'],'pilot')
        self.assertEqual(pilot.load(root)['events'][0]['step'],'environment')
        self.assertEqual(found['run']['steps']['installation']['status'],'not_checked')
        self.assertTrue(Path(result['setup_report']).is_file())
        self.assertTrue(Path(result['report']).is_file())
        self.assertTrue(any('lecture.py' in ' '.join(c) for c in self.commands))
        self.assertEqual(len(setup.read(self.root)['attempts']),1)
    def test_failure_before_course_creation_is_reported_and_retry_preserves_it(self):
        def fail(command,timeout):return dict(returncode=17,timed_out=False,output='pip failed: QA')
        with patch.object(setup,'run',fail),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'partial report'):setup.prepare(self.args)
        self.assertFalse((self.root/'run').exists())
        first=setup.read(self.root)['attempts'][0]
        self.assertEqual(first['status'],'failed');self.assertEqual(first['events'][0]['step'],'dependencies')
        self.assertIn('pip failed',(self.root/'attempt-01-dependencies.log').read_text())
        self.prepare();value=setup.read(self.root)
        self.assertEqual(value['attempts'][0],first);self.assertEqual(value['attempts'][1]['status'],'passed')
        self.assertIn('failed',(self.root/'SETUP_REPORT.md').read_text())
    def test_missing_video_creates_partial_report_without_launching_commands(self):
        self.video.unlink()
        with patch.object(setup,'run') as runner:
            with self.assertRaisesRegex(RuntimeError,'local video'):setup.prepare(self.args)
            runner.assert_not_called()
        self.assertTrue((self.root/'SETUP_REPORT.md').is_file())
        self.assertEqual(setup.read(self.root)['attempts'][0]['status'],'failed')
    def test_existing_arbitrary_folder_not_adopted(self):
        self.root.mkdir();(self.root/'notes').write_text('keep')
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual((self.root/'notes').read_text(),'keep');self.assertFalse((self.root/'setup.json').exists())
    def test_log_links_are_refused_and_external_file_unchanged(self):
        self.root.mkdir();setup.save(self.root,dict(schema=setup.SCHEMA,test_only=True,attempts=[]))
        keep=self.base/'keep';keep.write_text('keep')
        (self.root/'attempt-01-dependencies.log').symlink_to(keep)
        with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(keep.read_text(),'keep')
    def test_offline_does_not_install_or_download(self):
        runtime=self.base/'shared';exe=runtime/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        exe.parent.mkdir(parents=True);exe.write_bytes(b'fixture')
        self.args.runtime=runtime;self.args.model_path=self.base/'model';self.args.offline=True
        self.prepare()
        self.assertFalse(any('setup_local.py' in ' '.join(c) or 'download_model.py' in ' '.join(c) for c in self.commands))
    def test_online_rejects_mutating_external_runtime(self):
        self.args.runtime=self.base/'shared'
        with self.assertRaisesRegex(RuntimeError,'own runtime'):self.prepare()
        self.assertEqual(self.commands,[])
    def test_subprocess_failure_and_timeout_are_real(self):
        value=setup.run([sys.executable,'-c','import sys; print("QA failure"); sys.exit(7)'],5)
        self.assertEqual(value['returncode'],7);self.assertIn('QA failure',value['output'])
        value=setup.run([sys.executable,'-c','import time; time.sleep(30)'],.1)
        self.assertTrue(value['timed_out']);self.assertNotEqual(value['returncode'],0)
    def test_output_tail_is_bounded_and_common_credentials_redacted(self):
        value=setup.run([sys.executable,'-c','print("x"*300000); print("https://user:pass@example.test token=abc")'],5)
        self.assertLess(len(value['output']),setup.MAX_OUTPUT+200)
        self.assertNotIn('user:pass',value['output']);self.assertNotIn('token=abc',value['output'])
    def test_too_long_or_nonfinite_smoke_rejected(self):
        for seconds in (0,61,float('nan')):
            self.args.seconds=seconds
            with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(self.commands,[])
    def test_repeated_success_keeps_progress_and_earlier_reports(self):
        self.prepare();root=self.root/'run';old=pilot.load(root)['events'][0]
        state=root/'course/qa-progress-marker';state.write_text('keep user progress')
        self.prepare()
        self.assertEqual(state.read_text(),'keep user progress')
        self.assertEqual(pilot.load(root)['events'][0],old)
        self.assertEqual(len(pilot.load(root)['events']),2)

    def test_corrupt_setup_is_not_overwritten(self):
        self.prepare();path=self.root/'setup.json';path.write_text('{"schema":"unknown"}')
        raw=path.read_bytes()
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(path.read_bytes(),raw)
    def test_busy_setup_does_not_create_another_attempt(self):
        from storage_lock import file_lock
        self.prepare();before=(self.root/'setup.json').read_bytes()
        with file_lock(self.root/'.setup.lock'):
            with self.assertRaises(TimeoutError):self.prepare()
        self.assertEqual((self.root/'setup.json').read_bytes(),before)
    def test_completed_smoke_without_speech_is_not_called_ready(self):
        import hashlib
        folder=self.base/'smoke';folder.mkdir()
        atomic_json(folder/'manifest.json',{})
        atomic_json(folder/'transcript.json',[])
        atomic_json(folder/'tutor_packet.json',dict(frames=[dict(id='qa')],speech=[]))
        sha=hashlib.sha256((folder/'tutor_packet.json').read_bytes()).hexdigest()
        atomic_json(folder/'checkpoint.json',dict(status='complete',stages=dict(packet=dict(sha256=sha))))
        with patch('source_integrity.verify_evidence'):
            with self.assertRaisesRegex(ValueError,'No recognized speech'):
                setup.smoke_check(folder,self.base/'verified.json')
        self.assertFalse((self.base/'verified.json').exists())


class Project(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.run=self.root/'run';pilot.start(self.run)
        self.project=self.root/'project'
    def test_connect_is_explicit_cas_and_idempotent(self):
        with self.assertRaises(ValueError):pilot.connect_project(self.run,self.project,2)
        result=pilot.connect_project(self.run,self.project,0)
        self.assertTrue(result['connected']);revision=result['connection']['revision']
        self.assertEqual(pilot.connect_project(self.run,self.project,revision)['connection']['revision'],revision)
    def test_other_course_or_existing_project_files_are_preserved(self):
        self.project.mkdir();(self.project/'personal').write_text('keep')
        with self.assertRaisesRegex(ValueError,'empty dedicated'):pilot.connect_project(self.run,self.project,0)
        self.assertFalse((self.project/'.dosuti-pilot.json').exists())
        (self.project/'personal').unlink()
        other=self.root/'other';pilot.start(other)
        pilot.connect_project(other,self.project,0)
        before=(self.project/'.dosuti-mentor.json').read_bytes()
        with self.assertRaises(ValueError):pilot.connect_project(self.run,self.project,1)
        self.assertEqual((self.project/'.dosuti-mentor.json').read_bytes(),before)
    def test_new_process_discovers_run_and_respects_disabled_chat(self):
        pilot.connect_project(self.run,self.project,0)
        command=[sys.executable,'-B',str(Path(pilot.__file__)),'project-status','--project',str(self.project)]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['pilot'],str(self.run))
        connection.dispatch(NS(project=self.project,action='disconnect',scope='chat',chat='homework',expected_revision=1))
        self.assertFalse(pilot.project_status(self.project,'homework')['connected'])
        self.assertFalse(pilot.project_status(self.project)['connected']) # ID needed when overrides exist.
        self.assertTrue(pilot.project_status(self.project,'another-chat')['connected'])
    def test_marker_alone_is_not_permission_to_override_course(self):
        pilot.connect_project(self.run,self.project,0)
        other=self.root/'other';pilot.start(other)
        connection.dispatch(NS(project=self.project,action='connect',scope='project',chat=None,course=other/'course',profile='pilot',expected_revision=1))
        self.assertFalse(pilot.project_status(self.project)['connected'])
    def test_project_marker_survives_parent_relocation(self):
        import shutil
        pilot.connect_project(self.run,self.project,0)
        # The marker remains relative; bindings explicitly require reconnect after relocation.
        moved=self.root.with_name(self.root.name+'-moved');shutil.copytree(self.root,moved)
        self.addCleanup(shutil.rmtree,moved)
        result=pilot.project_status(moved/'project')
        self.assertFalse(result['connected']);self.assertEqual(result['pilot'],str(moved/'run'))
    def test_linked_marker_and_missing_course_fail_safely(self):
        pilot.connect_project(self.run,self.project,0)
        marker=self.project/'.dosuti-pilot.json';raw=marker.read_bytes();marker.unlink()
        target=self.root/'target';target.write_bytes(raw);marker.symlink_to(target)
        with self.assertRaises(ValueError):pilot.project_status(self.project)

    def test_interrupted_connection_can_resume_without_overwriting_marker(self):
        with patch.object(connection,'dispatch',side_effect=OSError('QA interruption')):
            with self.assertRaises(OSError):pilot.connect_project(self.run,self.project,0)
        marker=(self.project/'.dosuti-pilot.json').read_bytes()
        result=pilot.connect_project(self.run,self.project,0)
        self.assertTrue(result['connected']);self.assertEqual((self.project/'.dosuti-pilot.json').read_bytes(),marker)
    def test_parallel_owners_cannot_mix_marker_and_binding(self):
        from concurrent.futures import ThreadPoolExecutor
        other=self.root/'other';pilot.start(other)
        def connect(path):
            try:pilot.connect_project(path,self.project,0);return True
            except (ValueError,TimeoutError):return False
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(connect,(self.run,other)))
        self.assertEqual(sorted(results),[False,True])
        self.assertTrue(pilot.project_status(self.project)['connected'])
