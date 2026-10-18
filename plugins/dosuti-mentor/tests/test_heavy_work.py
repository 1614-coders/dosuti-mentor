"""Cross-process model admission, crash release and compatibility."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import heavy_work as heavy
import managed_storage as storage
import lecture
from storage_lock import file_lock

SCRIPTS=Path(heavy.__file__).parent

class HeavyWork(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=storage.initialize(Path(self.tmp.name).resolve()/'данные с пробелами')
    def worker(self,code,*args):
        p=subprocess.Popen([sys.executable,'-u','-c',f'import sys;sys.path.insert(0,{str(SCRIPTS)!r});'+code,*map(str,args)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def close():
            if p.poll() is None:p.kill();p.wait(timeout=10)
            p.stdout.close();p.stderr.close()
        self.addCleanup(close);return p
    def test_waiter_does_not_enter_worker_and_crash_releases_slot(self):
        holder=self.worker('from pathlib import Path;from heavy_work import slot;import time\nwith slot(Path(sys.argv[1])):\n print("held",flush=True);time.sleep(60)',self.root)
        self.assertEqual(holder.stdout.readline().strip(),'held')
        entered=self.root/'entered'
        waiter=self.worker('from pathlib import Path;from types import SimpleNamespace;import lecture\n'
            'sys.modules["processing_job"]=SimpleNamespace(run_job=lambda a,f: Path(sys.argv[2]).write_text("model admitted"))\n'
            'lecture.run_job(SimpleNamespace(output=Path(sys.argv[1])/"lesson"),"ffmpeg")',self.root,entered)
        time.sleep(.5);self.assertIsNone(waiter.poll());self.assertFalse(entered.exists())
        holder.kill();holder.wait(timeout=10)
        waiter.wait(timeout=10)
        self.assertEqual(waiter.returncode,0,waiter.stderr.read())
        self.assertTrue(entered.exists())
    def test_timeout_preserves_progress_and_released_slot_reusable(self):
        progress=self.root/'progress.json';progress.write_text('preserve')
        with file_lock(self.root/'.heavy.lock'):
            with self.assertRaises(TimeoutError):
                with heavy.slot(self.root,timeout=0):self.fail('must wait')
        with heavy.slot(self.root):pass
        self.assertEqual(progress.read_text(),'preserve')
    def test_nested_and_exception_release(self):
        with self.assertRaises(ValueError):
            with heavy.slot(self.root):
                with heavy.slot(self.root):raise ValueError('original')
        with heavy.slot(self.root):pass
    def test_legacy_lecture_has_no_new_home_or_lock(self):
        from types import SimpleNamespace
        old=Path(self.tmp.name).resolve()/'legacy';old.mkdir()
        with patch.dict(sys.modules,{'processing_job':SimpleNamespace(run_job=lambda a,f:'ready')}),patch.dict(os.environ,{},clear=True):
            self.assertEqual(lecture.run_job(SimpleNamespace(output=old),'ffmpeg'),'ready')
        self.assertEqual(list(old.iterdir()),[])
