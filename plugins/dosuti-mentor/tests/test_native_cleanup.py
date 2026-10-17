"""Never prune active/current/unverified builds, and recover interrupted pruning."""
import copy,json,os,sys,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import native_cache as nc,native_cleanup as gc
import test_native_cache

class Cleanup(unittest.TestCase):
    def setUp(self):
        self.f=test_native_cache.NativeCache();self.f.setUp()
        self.source_patch=patch.object(gc,'SOURCE',self.f.source);self.source_patch.start()
        self.platform_patch=patch('native_cleanup.platform.system',return_value='Darwin');self.platform_patch.start()
        self.old_source=self.f.root/'old.swift';self.old_source.write_text(self.f.source.read_text())
        self.old=self.f.get();self.age()
        self.f.source.write_text('QA current source');self.current=self.f.get()
    def tearDown(self):self.platform_patch.stop();self.source_patch.stop();self.f.tearDown()
    def age(self):
        ago=time.time_ns()-14*86400*10**9
        for path in (self.old,self.old.parent/'build.json'):os.utime(path,ns=(ago,ago))
    def plan(self):return gc.plan(root=self.f.cache)
    def apply(self,value):return gc.apply(value,root=self.f.cache)
    def fingerprint(self):return {str(p):p.read_bytes() for p in self.f.cache.rglob('*') if p.is_file()}
    def test_plan_and_prune_keep_current_and_lock_inode(self):
        before=self.fingerprint();value=self.plan();self.assertEqual(before,self.fingerprint())
        self.assertEqual([r['key'] for r in value['entries'] if r['decision']=='eligible'],[self.old.parent.name])
        lock=self.old.parent/'build.lock';inode=lock.stat().st_ino
        result=self.apply(value);self.assertGreater(result['reclaimed_bytes'],0)
        self.assertFalse(self.old.exists());self.assertFalse((self.old.parent/'build.json').exists())
        self.assertTrue(self.old.parent.is_dir());self.assertEqual(lock.stat().st_ino,inode)
        self.assertTrue(self.current.exists());self.assertFalse(result['courses_touched'])
        with nc.executable(self.old_source,self.f.cache) as path:self.assertTrue(path.exists())
        self.assertEqual(self.f.build.call_count,3)
    def test_recent_or_future_builds_preserved(self):
        for shift in (0,86400*10**9):
            stamp=time.time_ns()+shift
            for path in (self.old,self.old.parent/'build.json'):os.utime(path,ns=(stamp,stamp))
            self.assertEqual(self.plan()['reclaimable_bytes'],0)
    def test_changed_binary_preserved_after_plan(self):
        value=self.plan();self.old.write_bytes(b'changed');before=self.fingerprint()
        result=self.apply(value);self.assertEqual(result['reclaimed_bytes'],0);self.assertEqual(before,self.fingerprint())
    def test_build_becoming_current_preserved(self):
        value=self.plan();self.f.source.write_text(self.old_source.read_text())
        result=self.apply(value);self.assertEqual(result['reclaimed_bytes'],0);self.assertTrue(self.old.exists())
    def test_active_execution_skipped(self):
        ready=threading.Event();release=threading.Event()
        def use():
            with nc.executable(self.old_source,self.f.cache):
                ready.set();self.assertTrue(release.wait(10))
        value=self.plan()
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(use)
            try:
                self.assertTrue(ready.wait(5));result=self.apply(value)
                self.assertEqual(result['reclaimed_bytes'],0);self.assertTrue(self.old.exists())
            finally:release.set();future.result(timeout=10)
    def test_symlink_and_hardlink_preserved(self):
        target=self.f.root/'outside';target.write_bytes(b'keep')
        self.old.unlink();self.old.symlink_to(target)
        self.assertEqual(self.plan()['reclaimable_bytes'],0);self.assertEqual(target.read_bytes(),b'keep')
        self.old.unlink();os.link(target,self.old)
        self.assertEqual(self.plan()['reclaimable_bytes'],0)
    def test_unknown_files_preserved(self):
        (self.old.parent/'keep.txt').write_text('keep')
        self.assertEqual(self.plan()['reclaimable_bytes'],0)
    def test_forged_paths_rejected_before_any_deletion(self):
        value=self.plan();bad=copy.deepcopy(next(r for r in value['entries'] if r['decision']=='eligible'));bad['key']='../outside'
        value['entries'].append(bad);value['sha256']=gc.signature({k:v for k,v in value.items() if k!='sha256'})
        before=self.fingerprint()
        with self.assertRaises(ValueError):self.apply(value)
        self.assertEqual(before,self.fingerprint())
    def test_wrong_root_and_plan_checksum_refused(self):
        value=self.plan();value['root']=str(self.f.root)
        with self.assertRaises(ValueError):self.apply(value)
        value=self.plan();value['sha256']='0'*64
        with self.assertRaises(ValueError):self.apply(value)
        self.assertTrue(self.old.exists())
    def test_interrupted_binary_removal_resumes_from_same_plan(self):
        value=self.plan();original=Path.unlink
        def fail(path,*a,**kw):
            if path==self.old:raise OSError('QA unlink failed')
            return original(path,*a,**kw)
        with patch.object(Path,'unlink',fail):
            result=self.apply(value)
        self.assertEqual(result['reclaimed_bytes'],0);self.assertTrue(result['entries'][0]['incomplete_eviction'])
        self.assertTrue((self.old.parent/'evicted.json').exists())
        result=self.apply(value);self.assertTrue(result['entries'][0]['resumed'])
        self.assertFalse(self.old.exists());self.assertFalse((self.old.parent/'evicted.json').exists())
    def test_runtime_recovers_marker_after_metadata_unlink_failure(self):
        value=self.plan();marker=self.old.parent/'evicted.json';original=Path.unlink
        def fail(path,*a,**kw):
            if path==marker:raise OSError('QA marker removal failed')
            return original(path,*a,**kw)
        with patch.object(Path,'unlink',fail):result=self.apply(value)
        self.assertTrue(marker.exists());self.assertFalse(self.old.exists())
        with nc.executable(self.old_source,self.f.cache) as path:self.assertTrue(path.exists())
        self.assertFalse(marker.exists());self.assertEqual(self.f.build.call_count,3)
    def test_absent_cache_and_windows_do_not_create_files(self):
        absent=self.f.root/'absent';self.assertEqual(gc.plan(root=absent)['status'],'absent');self.assertFalse(absent.exists())
        with patch('native_cleanup.platform.system',return_value='Windows'),patch.object(nc,'host_root',side_effect=AssertionError('No uid lookup')):
            self.assertEqual(gc.plan()['status'],'unsupported_platform')
            with self.assertRaises(ValueError):gc.apply({})
    def test_nonprivate_root_refused(self):
        self.f.cache.chmod(0o755)
        with self.assertRaises(ValueError):self.plan()
    def test_modified_eviction_marker_not_completed(self):
        value=self.plan();(self.old.parent/'build.json').replace(self.old.parent/'evicted.json')
        marker=self.old.parent/'evicted.json';data=json.loads(marker.read_text());data['binary_sha256']='0'*64;marker.write_text(json.dumps(data))
        result=self.apply(value);self.assertEqual(result['reclaimed_bytes'],0);self.assertTrue(self.old.exists())
        with self.assertRaises(ValueError):
            with nc.executable(self.old_source,self.f.cache):pass
