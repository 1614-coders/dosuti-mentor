"""Research snapshots and the journal commit must agree after failed writes."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import sys
import subprocess
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import topic_research as tr
from study_units import digest
import test_topic_research


class ResearchRollback(unittest.TestCase):
    def setUp(self):
        self.f=test_topic_research.Research();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.save(self.f.request())
        self.folder=self.f.path.parent

    def files(self):
        return {str(p.relative_to(self.folder)):p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}

    def target(self):
        return self.folder/'snapshots'/(hashlib.sha256(self.f.text.read_bytes()).hexdigest()+'.txt')

    def test_history_limit_is_checked_before_a_new_snapshot_is_published(self):
        self.f.save(self.f.capture())
        data=json.loads(self.f.path.read_text())
        # Populate a real, checksum-valid journal close to the existing byte limit.
        while True:
            event=dict(revision=len(data['events'])+1,at='QA',value=self.f.review(notes='x'*4000))
            event['sha256']=digest(event)
            candidate=dict(data,revision=event['revision'],events=data['events']+[event])
            if len(json.dumps(candidate,ensure_ascii=False,indent=2).encode())>899000:break
            data=candidate
        self.f.path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
        tr.replay(data)
        self.f.text.write_text('A different source')
        before=self.files()
        dumps=json.dumps
        def encode(value,*args,**kw):
            if isinstance(value,dict) and value.get('schema')==tr.SCHEMA and value.get('revision')==data['revision']+1:
                self.assertFalse(self.target().exists(),'snapshot was published before the size check')
            return dumps(value,*args,**kw)
        with patch.object(tr.json,'dumps',side_effect=encode),self.assertRaisesRegex(ValueError,'history limit'):
            self.f.save(self.f.capture(id='new',notes='n'*4000))
        self.assertEqual(self.files(),before)
        self.assertEqual(self.f.read()['revision'],data['revision'])

    def test_failed_or_cancelled_journal_write_removes_only_new_snapshot_and_retry_works(self):
        before=self.files();original=self.f.text.read_bytes()
        for error in (OSError('journal write failed'),KeyboardInterrupt('cancel capture')):
            with self.subTest(error=type(error).__name__),patch.object(tr,'atomic_json',side_effect=error):
                with self.assertRaises(type(error)):self.f.save(self.f.capture(),1)
            self.assertEqual(self.files(),before)
            self.assertEqual(self.f.text.read_bytes(),original)
            self.assertEqual(self.f.read()['revision'],1)
        result=self.f.save(self.f.capture(),1)
        self.assertEqual(result['revision'],2)
        self.assertEqual(self.target().read_bytes(),original)

    def test_snapshot_sealing_failure_rolls_back_new_file(self):
        before=self.files();chmod=Path.chmod;failed=False
        def seal(path,mode,*a,**kw):
            nonlocal failed
            chmod(path,mode,*a,**kw)
            if path==self.target() and not failed:
                failed=True
                raise OSError('seal failed')
        with patch.object(Path,'chmod',seal),self.assertRaisesRegex(OSError,'seal failed'):
            self.f.save(self.f.capture(),1)
        self.assertEqual(self.files(),before)

    def test_error_after_journal_commit_preserves_registered_snapshot(self):
        writer=tr.atomic_json
        def save_then_fail(path,data):
            writer(path,data)
            raise OSError('after replace')
        with patch.object(tr,'atomic_json',side_effect=save_then_fail),self.assertRaisesRegex(OSError,'after replace'):
            self.f.save(self.f.capture(),1)
        current=self.f.read()
        self.assertEqual(current['revision'],2)
        self.assertEqual(current['sources'][0]['integrity'],'verified')
        before=self.files()
        with self.assertRaisesRegex(ValueError,'changed'):self.f.save(self.f.capture(id='retry'),1)
        self.assertEqual(self.files(),before)
        self.assertEqual(self.f.save(self.f.review(),2)['sources'][0]['text'],self.f.text.read_bytes().decode('utf-8'))

    def test_existing_registered_or_unregistered_snapshot_is_not_owned_by_retry(self):
        target=self.target();target.parent.mkdir();target.write_bytes(self.f.text.read_bytes());target.chmod(0o444)
        before=self.files()
        with patch.object(tr,'atomic_json',side_effect=OSError('registration failed')),self.assertRaises(OSError):
            self.f.save(self.f.capture(),1)
        self.assertEqual(self.files(),before)
        self.f.save(self.f.capture(),1);before=self.files()
        with patch.object(tr,'atomic_json',side_effect=OSError('registration failed')),self.assertRaises(OSError):
            self.f.save(self.f.capture(id='duplicate'),2)
        self.assertEqual(self.files(),before)
        self.assertEqual(self.f.read()['sources'][0]['integrity'],'verified')

    def test_unreadable_journal_preserves_uncertain_snapshot(self):
        def corrupt(path,data):
            path.write_text('{')
            raise OSError('ambiguous commit')
        with patch.object(tr,'atomic_json',side_effect=corrupt),self.assertRaisesRegex(OSError,'ambiguous commit'):
            self.f.save(self.f.capture(),1)
        self.assertEqual(self.target().read_bytes(),self.f.text.read_bytes())
        self.assertEqual(self.f.path.read_text(),'{')

    def test_rollback_does_not_delete_a_replaced_snapshot(self):
        target=self.target();replacement=target.with_suffix('.replacement')
        def replace_then_fail(path,data):
            replacement.write_bytes(b'other operation')
            target.chmod(0o600);target.unlink();replacement.rename(target)
            raise OSError('replaced during failure')
        with patch.object(tr,'atomic_json',side_effect=replace_then_fail),self.assertRaisesRegex(OSError,'replaced during failure'):
            self.f.save(self.f.capture(),1)
        self.assertEqual(target.read_bytes(),b'other operation')
        self.assertEqual(self.f.read()['revision'],1)

    def test_parallel_success_after_failure_keeps_its_snapshot(self):
        ready=threading.Event();attempted=threading.Event();writer=tr.atomic_json
        inputs=[]
        for identity in ('failing','winning'):
            path=self.f.f.f.directory/(identity+'.json')
            path.write_text(json.dumps(self.f.capture(id=identity)));inputs.append(path)
        def save(path):
            if path==inputs[1]:attempted.set()
            return tr.research(self.f.args(event_file=path,expected_revision=1))
        def commit(path,data):
            if data['events'][-1]['value'].get('id')=='failing':
                ready.set()
                if not attempted.wait(5):raise AssertionError('second writer did not start')
                raise OSError('first registration failed')
            return writer(path,data)
        with patch.object(tr,'atomic_json',side_effect=commit),ThreadPoolExecutor(2) as pool:
            first=pool.submit(save,inputs[0])
            self.assertTrue(ready.wait(5),'first writer never reached commit')
            second=pool.submit(save,inputs[1])
            with self.assertRaisesRegex(OSError,'first registration failed'):first.result(timeout=10)
            result=second.result(timeout=10)
        self.assertEqual(result['revision'],2)
        self.assertEqual([s['id'] for s in result['sources']],['winning'])
        self.assertEqual(result['sources'][0]['integrity'],'verified')
        self.assertEqual(self.target().read_bytes(),self.f.text.read_bytes())
        self.assertEqual(len(list(self.target().parent.iterdir())),1)

    def test_staging_write_or_rename_interruption_leaves_no_unregistered_files(self):
        before=self.files();rename=Path.rename
        def rename_then_fail(path,target):
            rename(path,target)
            raise KeyboardInterrupt('after snapshot rename')
        for failure in ('write','before_rename','after_rename'):
            with self.subTest(failure=failure):
                if failure=='write':guard=patch.object(tr.os,'fsync',side_effect=KeyboardInterrupt('flush interrupted'))
                elif failure=='before_rename':guard=patch.object(Path,'rename',side_effect=OSError('rename failed'))
                else:guard=patch.object(Path,'rename',rename_then_fail)
                with guard,self.assertRaises((OSError,KeyboardInterrupt)):self.f.save(self.f.capture(),1)
                self.assertEqual(self.files(),before)
                self.assertEqual(self.f.read()['revision'],1)

    def test_two_cli_processes_keep_one_cas_winner_and_no_loser_snapshot(self):
        inputs=[]
        for index in range(2):
            source=self.f.f.f.directory/('source-'+str(index)+'.txt');source.write_text('Source '+str(index))
            event=self.f.f.f.directory/('event-'+str(index)+'.json')
            event.write_text(json.dumps(self.f.capture(id='source-'+str(index),text_file=str(source))))
            inputs.append(event)
        entry=Path(tr.__file__).with_name('course.py')
        # Start two real CLI processes before releasing either from stdin.
        code="import runpy,sys;from pathlib import Path;entry=sys.argv.pop(1);sys.path.insert(0,str(Path(entry).parent));sys.stdin.readline();runpy.run_path(entry,run_name='__main__')"
        jobs=[]
        try:
            for event in inputs:
                jobs.append(subprocess.Popen([sys.executable,'-B','-c',code,str(entry),'research','--course',str(self.f.root),
                    '--research-id','maps','--event-file',str(event),'--expected-revision','1'],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8'))
            for job in jobs:job.stdin.write('start\n');job.stdin.flush()
            outputs=[job.communicate(timeout=20) for job in jobs]
            self.assertEqual(sum(job.returncode==0 for job in jobs),1,outputs)
            loser=next(i for i,job in enumerate(jobs) if job.returncode!=0)
            self.assertIn('Research changed',outputs[loser][1])
        finally:
            for job in jobs:
                if job.poll() is None:job.kill();job.communicate(timeout=5)
        result=self.f.read()
        self.assertEqual(result['revision'],2)
        self.assertEqual(len(result['sources']),1)
        self.assertEqual(result['sources'][0]['integrity'],'verified')
        snapshots=list((self.folder/'snapshots').iterdir())
        self.assertEqual(len(snapshots),1)
        self.assertEqual(snapshots[0].name,result['sources'][0]['sha256']+'.txt')

    def test_same_snapshot_in_another_research_is_not_rolled_back(self):
        other=self.f.f.args(research_id='other')
        event=self.f.f.f.directory/'other.json'
        for revision,value in enumerate((self.f.request(),self.f.capture())):
            event.write_text(json.dumps(value))
            tr.research(type(other)(**(vars(other)|dict(event_file=event,expected_revision=revision))))
        other_folder=self.f.root/'research/other'
        before={p:p.read_bytes() for p in other_folder.rglob('*') if p.is_file()}
        with patch.object(tr,'atomic_json',side_effect=OSError('failed registration')),self.assertRaises(OSError):
            self.f.save(self.f.capture(),1)
        self.assertEqual({p:p.read_bytes() for p in other_folder.rglob('*') if p.is_file()},before)
        self.assertFalse(self.target().exists())
        self.assertEqual(tr.research(other)['sources'][0]['integrity'],'verified')

    def test_restored_snapshot_referenced_by_old_journal_survives_failed_new_capture(self):
        self.f.save(self.f.capture(),1)
        target=self.target();target.chmod(0o600);target.unlink()
        before=self.f.path.read_bytes()
        with patch.object(tr,'atomic_json',side_effect=OSError('new capture failed')),self.assertRaises(OSError):
            self.f.save(self.f.capture(id='new'),2)
        self.assertEqual(self.f.path.read_bytes(),before)
        self.assertEqual(target.read_bytes(),self.f.text.read_bytes())
        self.assertEqual(self.f.read()['sources'][0]['integrity'],'verified')

    def test_process_killed_after_snapshot_publication_can_retry_without_losing_history(self):
        import time
        event=self.f.f.f.directory/'kill-event.json';event.write_text(json.dumps(self.f.capture()))
        ready=self.f.f.f.directory/'published'
        scripts=Path(tr.__file__).parent
        code="""import sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import topic_research as tr
def interrupted(path,data):
    Path(sys.argv[4]).write_text('snapshot published before journal')
    time.sleep(60)
tr.atomic_json=interrupted
tr.research(SimpleNamespace(course=Path(sys.argv[2]),research_id='maps',event_file=Path(sys.argv[3]),expected_revision=1))
"""
        before=self.f.path.read_bytes()
        worker=subprocess.Popen([sys.executable,'-B','-c',code,str(scripts),str(self.f.root),str(event),str(ready)],
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while not ready.exists():
                self.assertIsNone(worker.poll(),'worker exited before controlled stop')
                self.assertLess(time.monotonic(),deadline,'worker did not reach journal publication')
                time.sleep(0.01)
            self.assertEqual(self.target().read_bytes(),self.f.text.read_bytes())
            worker.kill();worker.communicate(timeout=5)
        finally:
            if worker.poll() is None:worker.kill();worker.communicate(timeout=5)
        self.assertEqual(self.f.path.read_bytes(),before)
        self.assertEqual(self.f.read()['revision'],1)
        result=self.f.save(self.f.capture(),1)
        self.assertEqual(result['revision'],2)
        self.assertEqual(result['sources'][0]['integrity'],'verified')
        self.assertEqual(len(list(self.target().parent.iterdir())),1)
