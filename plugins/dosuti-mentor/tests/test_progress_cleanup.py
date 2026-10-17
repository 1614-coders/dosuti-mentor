"""Abandoned journal recovery must not delete progress or live writes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_unit_learning as fixture
import progress_cleanup as cleanup
import learning_segments as segments
from atomic_files import atomic_json


class ProgressCleanup(unittest.TestCase):
    def setUp(self):
        self.f=fixture.UnitLearning();self.f.setUp();self.f.save()
        self.folder=self.f.path.parent
        self.temp=self.folder/('unit-learning.json.'+'a'*32+'.tmp')
        self.temp.write_bytes(self.f.path.read_bytes())
    def tearDown(self):self.f.tearDown()
    def run_cleanup(self,apply=False):return cleanup.cleanup(self.f.args(apply=apply))
    def orphan(self):
        event=json.loads(self.f.path.read_text())['events'][0]
        raw=segments.encoded([event]);sha=hashlib.sha256(raw).hexdigest()
        folder=self.folder/'unit-learning-segments';folder.mkdir(exist_ok=True)
        path=folder/(sha+'.json');path.write_bytes(raw)
        return path,sha
    def test_plan_and_apply_preserve_confirmed_answers(self):
        before=self.f.path.read_bytes();orphan,sha=self.orphan()
        plan=self.run_cleanup();self.assertEqual(len(plan['candidates']),2)
        self.assertTrue(self.temp.exists());self.assertTrue(orphan.exists())
        self.assertEqual(self.run_cleanup(True)['removed'],plan['candidates'])
        self.assertFalse(self.temp.exists());self.assertFalse(orphan.exists())
        self.assertEqual(before,self.f.path.read_bytes());self.assertEqual(self.f.read()['learning']['revision'],1)
        self.assertEqual(self.run_cleanup(True)['removed'],[])
    def test_storage_diagnostics_and_public_cli(self):
        from storage_report import inventory
        root=self.f.args().course
        self.assertEqual(inventory(root)['recovery_artifacts']['unit_journal_temporaries'],1)
        command=[sys.executable,str(Path(cleanup.__file__).parent/'course.py'),'cleanup-progress','--course',str(root),'--profile','default']
        result=subprocess.run(command,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['candidates']),1);self.assertTrue(self.temp.exists())
        result=subprocess.run(command+['--apply'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['removed']),1);self.assertFalse(self.temp.exists())
    def test_referenced_segment_never_removed(self):
        path,sha=self.orphan()
        atomic_json(self.f.path,dict(schema=segments.SCHEMA,revision=1,segments=[dict(sha256=sha,count=1)],events=[]))
        self.run_cleanup(True);self.assertTrue(path.exists())
        self.assertTrue(self.f.read()['learning']['valid'])
    def test_unknown_temp_blocks_orphan_removal(self):
        path,sha=self.orphan();self.temp.write_text('{broken')
        result=self.run_cleanup(True)
        self.assertEqual(result['removed'],[]);self.assertTrue(path.exists());self.assertTrue(self.temp.exists())
    def test_unrelated_files_and_other_profiles_preserved(self):
        unrelated=self.folder/'notes.tmp';unrelated.write_text('private')
        other=self.folder.parent/'other';other.mkdir();other_temp=other/self.temp.name;other_temp.write_bytes(self.temp.read_bytes())
        self.run_cleanup(True);self.assertTrue(unrelated.exists());self.assertTrue(other_temp.exists())
    def test_symlink_and_hardlink_preserved(self):
        self.temp.unlink();outside=self.f.f.f.directory/'outside.json';outside.write_bytes(self.f.path.read_bytes())
        self.temp.symlink_to(outside);self.run_cleanup(True)
        self.assertTrue(self.temp.is_symlink());self.assertTrue(outside.exists())
        self.temp.unlink();os.link(outside,self.temp);self.run_cleanup(True)
        self.assertTrue(self.temp.exists());self.assertEqual(outside.stat().st_nlink,2)
    def test_corrupt_current_journal_blocks_all_deletion(self):
        self.f.path.write_text('{broken')
        with self.assertRaises(ValueError):self.run_cleanup(True)
        self.assertTrue(self.temp.exists())
    def test_changed_candidate_is_not_removed(self):
        original=cleanup.checked
        count=0
        def changed(path,*args):
            nonlocal count
            if path==self.temp:
                count+=1
                if count==3:self.temp.write_text('changed after planning')
            return original(path,*args)
        with patch.object(cleanup,'checked',changed),self.assertRaises(ValueError):self.run_cleanup(True)
        self.assertTrue(self.temp.exists())
    def test_real_active_writer_lock_blocks_cleanup(self):
        code='''
import sys,time
sys.path.insert(0,sys.argv[1])
from storage_lock import file_lock
with file_lock(sys.argv[2]):
 print('LOCKED',flush=True)
 time.sleep(60)
'''
        process=subprocess.Popen([sys.executable,'-u','-c',code,str(Path(cleanup.__file__).parent),str(self.folder/'unit-learning.lock')],stdout=subprocess.PIPE,text=True)
        try:
            import queue,threading
            lines=queue.Queue()
            reader=threading.Thread(target=lambda:lines.put(process.stdout.readline()),daemon=True);reader.start()
            self.assertEqual(lines.get(timeout=10).strip(),'LOCKED')
            reader.join(timeout=1)
            with self.assertRaises(TimeoutError):self.run_cleanup(True)
            self.assertTrue(self.temp.exists())
            process.kill();process.wait(timeout=5)
            self.assertEqual(len(self.run_cleanup(True)['removed']),1)
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=5)
            process.stdout.close()
