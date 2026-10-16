"""Build once per source/toolchain in private host storage; reject modified executables."""
import json,os,sys,tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import native_cache as nc

class NativeCache(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cache=self.root/'cache';self.source=self.root/'vision.swift';self.source.write_text('QA source')
        self.tool=patch.object(nc,'toolchain',return_value=(Path('/usr/bin/false'),dict(path='QA compiler',version=1)));self.tool.start()
        self.builder=patch.object(nc,'build',side_effect=lambda source,binary,modules,compiler:binary.write_bytes(b'QA executable, never actually executed'))
        self.build=self.builder.start()
    def tearDown(self):self.builder.stop();self.tool.stop();self.tmp.cleanup()
    def get(self):
        with nc.executable(self.source,self.cache) as p:return p
    def test_reuse_build_and_private_permissions(self):
        p=self.get();self.assertEqual(p,self.get());self.assertEqual(self.build.call_count,1);self.assertEqual(self.cache.stat().st_mode&0o777,0o700)
        self.assertEqual(p.stat().st_mode&0o777,0o700)
    def test_source_or_toolchain_change_has_separate_build(self):
        a=self.get();self.source.write_text('QA new source');b=self.get();self.assertNotEqual(a,b)
        with patch.object(nc,'toolchain',return_value=(Path('/usr/bin/false'),dict(path='QA compiler',version=2))):self.assertNotEqual(b,self.get())
        self.assertEqual(self.build.call_count,3)
    def test_corrupt_binary_never_rebuilt_or_returned(self):
        p=self.get();p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):self.get()
        self.assertEqual(self.build.call_count,1)
    def test_corrupt_manifest_refused(self):
        p=self.get();manifest=p.parent/'build.json';d=json.loads(manifest.read_text());d['identity']['source_sha256']='x';manifest.write_text(json.dumps(d))
        with self.assertRaises(ValueError):self.get()
    def test_symlink_and_hardlink_binary_refused(self):
        p=self.get();p.unlink();target=self.root/'target';target.write_bytes(b'target');p.symlink_to(target)
        with self.assertRaises(ValueError):self.get()
        p.unlink();os.link(target,p)
        with self.assertRaises(ValueError):self.get()
    def test_nonprivate_or_symlink_root_refused(self):
        self.cache.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError,'private'):self.get()
        self.cache.rmdir();self.cache.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):self.get()
    def test_group_writable_binary_refused(self):
        p=self.get();p.chmod(0o720)
        with self.assertRaises(ValueError):self.get()
    def test_build_failure_does_not_publish(self):
        self.build.side_effect=RuntimeError('QA compiler fail')
        with self.assertRaises(RuntimeError):self.get()
        self.assertEqual(list(self.cache.rglob('vision')),[]);self.assertEqual(list(self.cache.rglob('build.json')),[])
    def test_manifest_write_failure_does_not_leave_executable(self):
        with patch.object(nc,'atomic_json',side_effect=OSError('QA disk full')),self.assertRaises(OSError):self.get()
        self.assertEqual(list(self.cache.rglob('vision')),[])
    def test_source_change_during_build_refused(self):
        def change(source,binary,*args):binary.write_bytes(b'QA');source.write_text('Changed during build')
        self.build.side_effect=change
        with self.assertRaisesRegex(ValueError,'source changed'):self.get()
        self.assertEqual(list(self.cache.rglob('build.json')),[])
    def test_incomplete_publication_never_executed(self):
        p=self.get();(p.parent/'build.json').unlink()
        with self.assertRaisesRegex(ValueError,'Incomplete'):self.get()
    def test_concurrent_process_equivalent_threads_only_build_once(self):
        with ThreadPoolExecutor(4) as pool:paths=list(pool.map(lambda _:self.get(),range(4)))
        self.assertEqual(len(set(paths)),1);self.assertEqual(self.build.call_count,1)

    def test_toolchain_preserves_swiftc_symlink_driver_name(self):
        self.tool.stop()
        driver=self.root/'swift-driver';driver.write_text('QA driver');alias=self.root/'swiftc';alias.symlink_to(driver)
        import subprocess
        with patch.object(nc.subprocess,'run',return_value=subprocess.CompletedProcess([],0,str(alias)+'\n','')):
            compiler,identity=nc.toolchain()
        self.assertEqual(compiler,alias);self.assertEqual(identity['resolved_path'],str(driver.resolve()))
