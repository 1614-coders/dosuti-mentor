"""Byte contracts and fault paths of legacy course and atomic writers."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import atomic_files as af
import course


class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'данные с пробелами';self.root.mkdir()
        self.path=self.root/'record.json'

    def writers(self):
        return dict(course=lambda path:course.save(path,dict(text='Привет',number=2)),
                    json=lambda path:af.atomic_json(path,dict(text='Привет',number=2)),
                    text=lambda path:af.atomic_text(path,'Привет\nстрока\r\n'),
                    bytes=lambda path:af.atomic_bytes(path,b'\x00\xff\r\n\n'),
                    chunks=lambda path:af.atomic_chunks(path,iter(['Привет\n','строка\r\n'])))

    def test_exact_bytes_encoding_newlines_and_return_values(self):
        payload=dict(text='Привет',number=2)
        encoded=json.dumps(payload,ensure_ascii=False,indent=2)
        expected=dict(course=encoded.replace('\n',os.linesep).encode('utf-8'),json=encoded.encode('utf-8'),
                      text='Привет\nстрока\r\n'.encode('utf-8'),bytes=b'\x00\xff\r\n\n',chunks='Привет\nстрока\r\n'.encode('utf-8'))
        for name,writer in self.writers().items():
            with self.subTest(writer=name):
                self.assertIsNone(writer(self.path))
                self.assertEqual(self.path.read_bytes(),expected[name])
                self.assertFalse(list(self.root.glob('*.tmp')))

    def test_course_nan_and_strict_atomic_json_remain_distinct(self):
        value=dict(value=float('nan'),positive=float('inf'),negative=-float('inf'))
        course.save(self.path,value)
        expected=json.dumps(value,ensure_ascii=False,indent=2).replace('\n',os.linesep).encode()
        self.assertEqual(self.path.read_bytes(),expected)
        with self.assertRaises(ValueError):af.atomic_json(self.path,value)
        self.assertEqual(self.path.read_bytes(),expected)
        new=self.root/'missing'/'strict.json'
        with self.assertRaises(ValueError):af.atomic_json(new,value)
        self.assertFalse(new.parent.exists())

    def test_serialization_and_anchoring_keep_their_original_side_effect_order(self):
        invalid=dict(value=object())
        for name,writer in [('course',course.save),('json',af.atomic_json)]:
            with self.subTest(writer=name):
                target=self.root/name/'bad.json'
                with self.assertRaises(TypeError):writer(target,invalid)
                self.assertEqual(target.parent.exists(),name=='course')
                self.assertFalse(target.exists())
                self.assertFalse(list(target.parent.glob('*.tmp')))
        target=self.root/'lesson'/'lesson.json'
        def reject(value,path):
            self.assertTrue(path.parent.is_dir())
            self.assertFalse(list(path.parent.glob('*.tmp')))
            raise ValueError('anchor failed')
        with patch.object(course,'anchored',side_effect=reject),self.assertRaisesRegex(ValueError,'anchor failed'):
            course.save(target,{})
        self.assertFalse(target.exists())

    def test_lesson_paths_are_anchored_and_legacy_export_is_preserved(self):
        target=self.root/'lesson.json';source=self.root/'video.mp4';source.write_bytes(b'original')
        value=dict(video=dict(source=str(source),path=str(source)),code={},homework={},resources={})
        expected=course.anchored(value,target)
        self.assertIsNone(course.save(target,value))
        self.assertEqual(target.read_bytes(),json.dumps(expected,ensure_ascii=False,indent=2).replace('\n',os.linesep).encode())
        self.assertEqual(course.read(target)['video']['path'],str(source))
        self.assertEqual(source.read_bytes(),b'original')

    def observe(self,writer,fault=None):
        events=[];opened=[];real_open=Path.open;real_replace=Path.replace;real_unlink=Path.unlink;real_sync=os.fsync
        class Stream:
            def __init__(self,raw):self.raw=raw
            def __enter__(self):self.raw.__enter__();return self
            def __exit__(self,*args):
                events.append('close');result=self.raw.__exit__(*args)
                if fault=='close':raise OSError('close failed')
                return result
            def write(self,value):
                events.append('write')
                if fault in ('write','interrupt'):
                    self.raw.write(value[:1])
                    if fault=='interrupt':raise KeyboardInterrupt('write interrupted')
                    raise OSError('write failed')
                return self.raw.write(value)
            def flush(self):
                events.append('flush')
                if fault=='flush':raise OSError('flush failed')
                return self.raw.flush()
            def fileno(self):return self.raw.fileno()
        def open_file(path,*args,**kw):
            if path.suffix=='.tmp' and args and args[0].startswith('w'):
                events.append('open')
                if fault=='open':raise OSError('open failed')
                raw=real_open(path,*args,**kw);opened.append(raw)
                return Stream(raw)
            return real_open(path,*args,**kw)
        def sync(fd):
            events.append('fsync')
            if fault=='fsync':raise OSError('fsync failed')
            real_sync(fd)
        def replace(path,target):
            events.append('replace')
            self.assertTrue(all(s.closed for s in opened))
            if fault=='replace':raise OSError('replace failed')
            result=real_replace(path,target)
            if fault=='after_replace':raise OSError('after replace')
            return result
        def unlink(path,*a,**kw):
            if path.suffix=='.tmp':events.append('unlink')
            return real_unlink(path,*a,**kw)
        with patch.object(Path,'open',open_file),patch.object(Path,'replace',replace),\
                patch.object(Path,'unlink',unlink),patch.object(os,'fsync',sync):
            writer(self.path)
        return events

    def test_flush_fsync_close_replace_cleanup_order_is_identical(self):
        for name,writer in self.writers().items():
            with self.subTest(writer=name):
                events=self.observe(writer)
                expected=['open']+['write']*(2 if name=='chunks' else 1)+['flush','fsync','close','replace','unlink']
                self.assertEqual(events,expected)

    def test_failures_and_keyboard_interrupt_preserve_old_file_and_remove_own_temp(self):
        foreign=self.root/'foreign.tmp';foreign.write_bytes(b'foreign')
        for name,writer in self.writers().items():
            for fault in ('open','write','flush','fsync','close','replace','interrupt'):
                with self.subTest(writer=name,fault=fault):
                    self.path.write_bytes(b'previous')
                    with self.assertRaises((OSError,KeyboardInterrupt)):self.observe(writer,fault)
                    self.assertEqual(self.path.read_bytes(),b'previous')
                    self.assertEqual(list(self.root.glob('*.tmp')),[foreign])
                    self.assertEqual(foreign.read_bytes(),b'foreign')

    def test_exception_after_replace_keeps_the_complete_new_file(self):
        for name,writer in self.writers().items():
            with self.subTest(writer=name):
                writer(self.path);expected=self.path.read_bytes();self.path.write_bytes(b'previous')
                with self.assertRaisesRegex(OSError,'after replace'):self.observe(writer,'after_replace')
                self.assertEqual(self.path.read_bytes(),expected)
                self.assertFalse(list(self.root.glob('*.tmp')))

    def test_chunk_generator_failure_does_not_publish_partial_output(self):
        self.path.write_bytes(b'previous')
        def chunks():
            yield 'first line\n'
            raise KeyboardInterrupt('generator stopped')
        with self.assertRaises(KeyboardInterrupt):af.atomic_chunks(self.path,chunks())
        self.assertEqual(self.path.read_bytes(),b'previous')
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_file_permissions_match_new_file_under_current_umask(self):
        control=self.root/'control';control.write_bytes(b'mode reference')
        mode=stat.S_IMODE(control.stat().st_mode)
        for name,writer in self.writers().items():
            with self.subTest(writer=name):
                self.path.unlink(missing_ok=True);writer(self.path)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode),mode)
                self.path.chmod(0o600);writer(self.path)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode),mode)

    def test_failed_concurrent_writer_cannot_remove_the_other_writers_result(self):
        real_sync=os.fsync
        for name,writer in self.writers().items():
            with self.subTest(writer=name):
                first=threading.Event();second=threading.Event();local=threading.local()
                def sync(fd):
                    if local.fail:
                        first.set()
                        if not second.wait(5):raise AssertionError('successful writer never completed')
                        raise OSError('first writer failed')
                    return real_sync(fd)
                def write(fail):
                    local.fail=fail
                    if not fail:self.assertTrue(first.wait(5))
                    try:writer(self.path)
                    finally:
                        if not fail:second.set()
                with patch.object(os,'fsync',sync),ThreadPoolExecutor(2) as pool:
                    a=pool.submit(write,True);b=pool.submit(write,False)
                    b.result(timeout=10)
                    with self.assertRaisesRegex(OSError,'first writer failed'):a.result(timeout=10)
                expected=self.path.read_bytes();writer(self.path)
                self.assertEqual(self.path.read_bytes(),expected)
                self.assertFalse(list(self.root.glob('*.tmp')))

    def test_all_writers_delegate_the_transaction_to_the_shared_primitive(self):
        for name,writer in self.writers().items():
            with self.subTest(writer=name),patch.object(af,'atomic_writer',wraps=af.atomic_writer) as shared:
                writer(self.path)
                shared.assert_called_once()

    def test_open_options_keep_system_newlines_separate_from_explicit_lf(self):
        original=Path.open
        for name,writer in self.writers().items():
            with self.subTest(writer=name):
                options=[]
                def opening(path,mode='r',*a,**kw):
                    if path.suffix=='.tmp':options.append((mode,kw))
                    return original(path,mode,*a,**kw)
                with patch.object(Path,'open',opening):writer(self.path)
                self.assertEqual(len(options),1)
                mode,kw=options[0]
                self.assertEqual(mode,'wb' if name=='bytes' else 'w')
                if name=='bytes':self.assertEqual(kw,{})
                else:
                    self.assertEqual(kw['encoding'],'utf-8')
                    if name=='course':self.assertNotIn('newline',kw)
                    else:self.assertEqual(kw['newline'],'\n')

    def test_concurrent_successes_expose_only_complete_json_documents(self):
        for name,writer in [('course',course.save),('json',af.atomic_json)]:
            with self.subTest(writer=name):
                writer(self.path,dict(writer='initial'))
                barrier=threading.Barrier(2);publication=threading.Lock();original_replace=Path.replace
                def replace(path,target):
                    # Windows readers need a closed handle before replacement.
                    # Only coordinate handle lifetime; both writers still stage concurrently.
                    with publication:return original_replace(path,target)
                def save(role):
                    barrier.wait(timeout=5)
                    for n in range(20):writer(self.path,dict(writer=role,index=n,text='Текст\n'*500))
                with patch.object(Path,'replace',replace),ThreadPoolExecutor(2) as pool:
                    jobs=[pool.submit(save,role) for role in ('a','b')]
                    while any(not job.done() for job in jobs):
                        with publication:data=json.loads(self.path.read_bytes())
                        self.assertIn(data['writer'],('initial','a','b'))
                    for job in jobs:job.result(timeout=5)
                self.assertIn(json.loads(self.path.read_bytes())['writer'],('a','b'))
                self.assertFalse(list(self.root.glob('*.tmp')))

    def test_killed_process_keeps_old_target_and_retry_preserves_unknown_orphan(self):
        code="""import sys,time,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import atomic_files,course
real_sync=os.fsync
def stop(fd):
    real_sync(fd)
    Path(sys.argv[4]).write_text('flushed before replacement')
    time.sleep(60)
os.fsync=stop
writer=course.save if sys.argv[3]=='course' else atomic_files.atomic_json
writer(Path(sys.argv[2]),dict(text='new complete data'))
"""
        for name,writer in [('course',course.save),('json',af.atomic_json)]:
            with self.subTest(writer=name):
                ready=self.root/(name+'.ready');self.path.write_bytes(b'{"previous":true}')
                previous_orphans=set(self.root.glob('*.tmp'))
                worker=subprocess.Popen([sys.executable,'-B','-c',code,str(course.SCRIPT_DIR),str(self.path),name,str(ready)],
                                        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+10
                    while not ready.exists():
                        self.assertIsNone(worker.poll(),'writer exited before fsync barrier')
                        self.assertLess(time.monotonic(),deadline,'writer never reached fsync')
                        time.sleep(0.01)
                    self.assertEqual(self.path.read_bytes(),b'{"previous":true}')
                    worker.kill();worker.communicate(timeout=5)
                finally:
                    if worker.poll() is None:worker.kill();worker.communicate(timeout=5)
                orphans=set(self.root.glob('*.tmp'))-previous_orphans
                self.assertEqual(len(orphans),1)
                orphan=orphans.pop();raw=orphan.read_bytes()
                self.assertEqual(json.loads(raw),dict(text='new complete data'))
                writer(self.path,dict(text='retry succeeded'))
                self.assertEqual(json.loads(self.path.read_bytes()),dict(text='retry succeeded'))
                self.assertEqual(orphan.read_bytes(),raw)

    def test_open_reader_sharing_denial_preserves_old_target_and_retry_succeeds(self):
        # Pin the OS behavior separately: it also applies to the old Path.replace primitive.
        old=b'{"previous":true}';probe=self.root/'replace-probe'
        self.path.write_bytes(old);probe.write_bytes(b'probe')
        with self.path.open('rb') as reader:
            if os.name=='nt':
                with self.assertRaises(PermissionError):probe.replace(self.path)
            else:probe.replace(self.path)
            self.assertEqual(reader.read(),old)
        probe.unlink(missing_ok=True)
        for name,writer in [('course',course.save),('json',af.atomic_json)]:
            with self.subTest(writer=name):
                self.path.write_bytes(old)
                with self.path.open('rb') as reader:
                    if os.name=='nt':
                        with self.assertRaises(PermissionError):writer(self.path,dict(updated=True))
                        self.assertEqual(self.path.read_bytes(),old)
                    else:writer(self.path,dict(updated=True))
                    self.assertEqual(reader.read(),old)
                self.assertFalse(list(self.root.glob('*.tmp')))
                writer(self.path,dict(retry=True))
                self.assertEqual(json.loads(self.path.read_bytes()),dict(retry=True))
