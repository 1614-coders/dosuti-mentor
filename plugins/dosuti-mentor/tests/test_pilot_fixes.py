"""Regression reproductions of destructive-export, lifecycle and orphan timer defects."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_pilot_evidence as evidence_fixture
import pilot
import pilot_activity as activity
import pilot_assessment as assessment
import pilot_cleanup as cleanup
import pilot_links
import source_integrity

class Fixes(unittest.TestCase):
    setUp=evidence_fixture.Evidence.setUp
    plan=evidence_fixture.Evidence.plan
    answer=evidence_fixture.Evidence.answer
    def test_sqlite_501_events_fully_exported_before_cleanup(self):
        folder=self.root/'course/learners/pilot';folder.mkdir(parents=True)
        with sqlite3.connect(folder/'progress.sqlite3') as db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, details TEXT)')
            db.executemany('INSERT INTO events VALUES (?,?)',[(i,'FIRST_UNIQUE' if i==0 else f'event-{i}') for i in range(501)])
        result=cleanup.plan(self.root,self.base/'cleanup.json');report=Path(result['report']).read_text()
        self.assertIn('FIRST_UNIQUE',report);self.assertIn('event-500',report)
        value=json.loads(Path(result['plan']).read_text());self.assertTrue(value['coverage']['complete'])
        row=next(e for e in value['coverage']['entries'] if e['path'].endswith('sqlite3'))
        self.assertEqual((row['detected'],row['exported']),(501,501))
        cleanup.apply(self.root,Path(result['plan']));self.assertFalse(self.root.exists())
    def test_segmented_501_events_and_missing_part_block_cleanup(self):
        from learning_segments import encoded
        folder=self.root/'course/learners/pilot';folder.mkdir(parents=True)
        parts=folder/'unit-learning-segments';parts.mkdir()
        data=[dict(revision=i+1,value='UNIQUE_BEGIN' if i==0 else f'v{i}') for i in range(500)]
        raw=encoded(data)
        import hashlib
        checksum=hashlib.sha256(raw).hexdigest();part=parts/(checksum+'.json');part.write_bytes(raw)
        (folder/'unit-learning.json').write_text(json.dumps(dict(schema='unit_learning_v3',revision=501,segments=[dict(sha256=checksum,count=500)],events=[dict(revision=501,value='UNIQUE_END')])))
        report=pilot.export(self.root)
        self.assertTrue(report['coverage']['complete']);text=Path(report['report']).read_text()
        self.assertIn('UNIQUE_BEGIN',text);self.assertIn('UNIQUE_END',text)
        part.unlink()
        with self.assertRaisesRegex(ValueError,'Incomplete'): cleanup.plan(self.root,self.base/'refused.json')
        self.assertTrue(self.root.exists());self.assertFalse((self.root/cleanup.MARKER).exists())
    def test_report_budget_or_corruption_prevents_deletion(self):
        (self.root/'course/learners/pilot').mkdir(parents=True)
        (self.root/'course/learners/pilot/preferences.json').write_text('{bad')
        with self.assertRaisesRegex(ValueError,'Incomplete'): cleanup.plan(self.root,self.base/'bad.json')
        self.assertTrue(self.material.exists())
        with patch('pilot_report.MAX_TOTAL',1):
            result=pilot.export(self.root)
        self.assertFalse(result['coverage']['complete'])
    def test_setup_pending_freezes_all_entry_points_and_status_recovers(self):
        outer=self.base/'setup';outer.mkdir();run=outer/'run';pilot.start(run)
        (outer/'setup.json').write_text(json.dumps(dict(schema='pilot_setup_v1',test_only=True,owner='fixture',attempts=[])))
        result=cleanup.plan(run,self.base/'setup-cleanup.json');target=run/'course/course.json';original=Path.unlink
        def interrupt(path,*args,**kwargs):
            if path==target: raise OSError('Injected interruption')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',interrupt):
            with self.assertRaises(OSError): cleanup.apply(run,result['plan'])
        for fn in (lambda:pilot.load(run),lambda:activity.start(run,'teaching'),lambda:assessment.record(run,0,'plan',self.plan())):
            with self.assertRaisesRegex(ValueError,'cleanup is pending'): fn()
        script=Path(pilot.__file__)
        result2=subprocess.run([sys.executable,str(script),'status','--pilot',str(run)],capture_output=True,text=True)
        self.assertEqual(result2.returncode,0,result2.stderr);self.assertTrue(json.loads(result2.stdout)['pending'])
        cleanup.apply(run,result['plan']);self.assertFalse(outer.exists())
    def test_legacy_cleanup_plan_cannot_bypass_coverage(self):
        result=cleanup.plan(self.root,self.base/'cleanup.json');p=Path(result['plan']);value=json.loads(p.read_text())
        value.pop('seal');value['schema']='pilot_cleanup_v1';value.pop('coverage');value['seal']=cleanup.seal(value);p.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'legacy'): cleanup.apply(self.root,p)
        self.assertTrue(self.material.exists())
    def test_real_killed_worker_recovery_cli_and_live_worker_protection(self):
        script=Path(pilot.__file__)
        code="import sys,time;sys.path.insert(0,sys.argv[1]);from pathlib import Path;from pilot_activity import span\nwith span('asr',Path(sys.argv[2])):\n print('STARTED',flush=True)\n time.sleep(120)\n"
        process=subprocess.Popen([sys.executable,'-u','-c',code,str(script.parent),str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'STARTED')
            item=activity.read(self.root)['activities'][0]
            cmd=[sys.executable,str(script),'activity-finish','--pilot',str(self.root),'--activity',item['id'],'--result','interrupted','--detail','Killed QA worker','--session','recovery-chat']
            result=subprocess.run(cmd,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(activity.read(self.root)['activities'][0]['status'],'running')
            process.kill();process.wait(timeout=5)
            result=subprocess.run(cmd,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            record=activity.read(self.root)['activities'][0]
            self.assertIsNone(record['seconds']);self.assertEqual(record['duration_state'],'unknown')
            self.assertEqual(record['recovered_session'],'recovery-chat')
            self.assertIn('неизвестно',Path(pilot.export(self.root)['report']).read_text())
            self.assertTrue(cleanup.plan(self.root,self.base/'after-recovery.json'))
        finally:
            if process.poll() is None: process.kill();process.wait(timeout=5)
            process.stdout.close();process.stderr.close()
    def test_legacy_unknown_timer_needs_explicit_recovery(self):
        item=activity.start(self.root,'asr',method='monotonic')
        with self.assertRaisesRegex(ValueError,'owner unknown'): activity.finish(self.root,item['id'],'interrupted','Legacy QA worker stopped')
        value=activity.finish(self.root,item['id'],'interrupted','Legacy QA worker stopped',allow_unknown_owner=True)
        self.assertIsNone(value['seconds']);self.assertEqual(value['owner_verification'],'legacy_owner_unverifiable')
    def test_hash_diagnostic_metadata_and_file_replacement(self):
        before=source_integrity.identity(self.material)
        for index in (1,2,3,4):
            after=list(before);after[index]+=1
            with patch.object(source_integrity,'identity',side_effect=[before,tuple(after)]):
                with self.assertRaises(ValueError) as captured: source_integrity.file_hash(self.material)
            message=str(captured.exception)
            self.assertIn(str(self.material),message);self.assertIn(('device','inode','size','mtime_ns','ctime_ns')[index],message)
            self.assertIn('before',message);self.assertIn('after',message)
    def test_resume_current_plan_skips_answered_and_rejects_stale(self):
        assessment.record(self.root,0,'plan',self.plan())
        assessment.record(self.root,1,'answer',self.answer())
        view=pilot.view(self.root,pilot.load(self.root))
        self.assertEqual(view['continuation']['lessons'][0]['next_question']['id'],'theory1')
        script=Path(pilot.__file__)
        result=subprocess.run([sys.executable,str(script),'status','--pilot',str(self.root),'--session','chat-b'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['continuation']['lessons'][0]['next_question']['id'],'theory1')
        self.material.write_text('changed')
        self.assertIsNone(pilot.view(self.root,pilot.load(self.root))['continuation']['lessons'][0]['next_question'])
    def test_links_unlinked_other_profile_missing_and_not_applicable_are_read_only(self):
        a=self.answer();before=self.material.read_bytes()
        self.assertEqual(pilot_links.reconcile(self.root,a)['status'],'unlinked')
        a['learning_ref']=dict(kind='not_applicable',reason='Synthetic fixture')
        self.assertEqual(pilot_links.reconcile(self.root,a)['status'],'not_applicable')
        ref=dict(kind='unit',profile='friend',target='maps',event=1,source_sha256='a'*64);a['learning_ref']=ref
        self.assertEqual(pilot_links.reconcile(self.root,a)['status'],'mismatch')
        a['learning_ref']=dict(ref,profile='pilot')
        self.assertEqual(pilot_links.reconcile(self.root,a)['status'],'not_recorded')
        self.assertFalse((self.root/'course/learners').exists());self.assertEqual(before,self.material.read_bytes())
    def test_unit_link_actual_assessment_same_attempt_and_changed_source(self):
        from test_unit_learning import UnitLearning
        helper=UnitLearning();helper.setUp();self.addCleanup(helper.tearDown)
        state=helper.save(profile='pilot',session='chat-a',value=helper.event(answer='3, put replaced 2'))
        shutil.copytree(helper.f.f.path,self.root/'course',dirs_exist_ok=True)
        ref=dict(kind='unit',profile='pilot',target='maps',event=1,source_sha256=state['unit']['content_sha256'])
        a=self.answer(learning_ref=ref)
        journal=self.root/'course/learners/pilot/unit-learning.json';before=journal.read_bytes()
        self.assertEqual(pilot_links.reconcile(self.root,a)['status'],'linked')
        self.assertEqual(pilot_links.reconcile(self.root,dict(a,answer='another attempt'))['status'],'mismatch')
        self.assertEqual(before,journal.read_bytes())
        snapshot=self.root/'course'/state['unit']['sources'][0]['snapshot'];snapshot.chmod(0o600);snapshot.write_text('corrupted')
        self.assertEqual(pilot_links.reconcile(self.root,a)['status'],'stale')
    def test_waiting_writer_rechecks_pending_after_lifecycle_lock(self):
        from pilot_lifecycle import guard
        from atomic_files import atomic_json
        code="""import sys
from pathlib import Path
from contextlib import contextmanager
sys.path.insert(0,sys.argv[1])
import pilot,pilot_lifecycle
original=pilot_lifecycle.file_lock
@contextmanager
def waiting(*a,**kw):
 print('WAITING',flush=True)
 with original(*a,**kw): yield
pilot_lifecycle.file_lock=waiting
try: pilot.record(Path(sys.argv[2]),0,dict(step='teaching',status='passed',observation='Synthetic fixture',tester_feedback=''))
except ValueError as e:
 print(str(e),flush=True)
 sys.exit(0)
sys.exit(3)
"""
        with guard(self.root):
            process=subprocess.Popen([sys.executable,'-u','-c',code,str(Path(pilot.__file__).parent),str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(),'WAITING')
                atomic_json(self.root/cleanup.MARKER,dict(seal='fixture',owner='fixture'))
            except BaseException:
                process.kill();process.wait();raise
        try:
            stdout,stderr=process.communicate(timeout=5)
            self.assertEqual(process.returncode,0,stderr);self.assertIn('cleanup is pending',stdout)
            self.assertEqual(json.loads((self.root/'pilot.json').read_text())['revision'],0)
        finally:
            if process.poll() is None: process.kill();process.wait()
    def test_two_cleanup_applies_are_serialized(self):
        from storage_lock import file_lock
        result=cleanup.plan(self.root,self.base/'cleanup.json');path=Path(result['plan'])
        with file_lock(path.with_name(path.name+'.apply.lock')):
            with self.assertRaises(TimeoutError): cleanup.apply(self.root,path)
        self.assertTrue(self.material.exists())
    def test_hash_replacement_detected_without_weakening_integrity(self):
        original=source_integrity.hashlib.file_digest
        replacement=self.base/'replacement';replacement.write_text('Replacement bytes')
        def replace_after_hash(stream,*a,**kw):
            value=original(stream,*a,**kw);replacement.replace(self.material);return value
        with patch.object(source_integrity.hashlib,'file_digest',replace_after_hash):
            with self.assertRaisesRegex(ValueError,'inode') as error: source_integrity.file_hash(self.material)
        self.assertIn(str(self.material),str(error.exception));self.assertEqual(self.material.read_text(),'Replacement bytes')
    def test_new_processes_read_their_own_unit_checkpoint_without_writing(self):
        from test_unit_learning import UnitLearning
        helper=UnitLearning();helper.setUp();self.addCleanup(helper.tearDown)
        for session,next_step in (('chat-a','Next A'),('chat-b','Next B')):
            helper.save(profile='pilot',session=session,checkpoint=True,value=dict(recap='Synthetic QA checkpoint',next_step=next_step,pending_question=None))
        shutil.copytree(helper.f.f.path,self.root/'course',dirs_exist_ok=True)
        journal=self.root/'course/learners/pilot/unit-learning.json';before=journal.read_bytes()
        for session,next_step in (('chat-a','Next A'),('chat-b','Next B')):
            result=subprocess.run([sys.executable,str(Path(pilot.__file__)),'status','--pilot',str(self.root),'--session',session],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads(result.stdout)['continuation']
            self.assertEqual(value['session'],session)
            self.assertTrue(value['unit_checkpoints'][0]['valid'])
            self.assertEqual(value['unit_checkpoints'][0]['continuation']['next_step'],next_step)
        self.assertEqual(before,journal.read_bytes())
