"""Portable contract models and native NTFS local-homework security tests."""
from contextlib import closing, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import course
import material_sources as ms
import registry_update as registry
import windows_managed_home as n
import windows_material_import as single
import windows_resource_text as resource
import windows_homework as hw

CASES=(
    ('lf',b'\n    if ready:\n\twork()\n\n',b'\n    if ready:\n\twork()\n\n'),
    ('crlf',b'\r\n    if ready:\r\n\twork()\r\n\r\n',b'\r\n    if ready:\r\n\twork()\r\n\n'),
    ('mixed',b'\tfirst\r\n    next\nlast\r\n',b'\tfirst\r\n    next\nlast\n'),
    ('no_final_newline',b'\tif ready:\r\n\t\twork()',b'\tif ready:\r\n\t\twork()\n'),
    ('blank_lines',b'  notes\r\n\r\n\tcode\n\n\n',b'  notes\r\n\r\n\tcode\n\n\n'),
    ('bom',b'\xef\xbb\xbf\twork()\r\n',b'\twork()\n'),
)


class HomeworkModels(unittest.TestCase):
    def test_normalization_is_exact_posix_contract_for_all_line_endings_and_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'notes.txt'
            for name,raw,expected in CASES:
                with self.subTest(name=name):
                    path.write_bytes(raw)
                    value,method=ms.text_content(path)
                    self.assertEqual(hw.normalize(raw),expected)
                    self.assertEqual(hw.normalize(raw),value.encode('utf-8'))
                    self.assertEqual(method,'utf8_text')

    def test_input_and_normalized_size_bound_and_rejected_binary_formats(self):
        for raw in (b'%PDF-1.4',b'PK\x03\x04zip',b'x\0y',b'\xff',b'<html>text</html>',
                    b' <!doctype html><p>text</p>',b' \r\n'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):hw.normalize(raw)
        with patch.object(hw,'MAX_BYTES',4):
            with self.assertRaises(ValueError):hw.normalize(b'12345')
            with self.assertRaises(ValueError):hw.normalize(b'1234')
            self.assertEqual(hw.normalize(b'123\n'),b'123\n')

    def test_scope_refuses_remote_prepared_other_kinds_and_document_suffixes_before_io(self):
        args=NS(kind='homework',source='notes.txt')
        self.assertEqual(hw.source_path(args).name,'notes.txt')
        for change in (dict(kind='code'),dict(kind='resources'),dict(source='https://example.test/x.txt'),
                       dict(material_text='prepared'),dict(material_origin={}),dict(import_prepared='folder'),
                       dict(source='x.PDF'),dict(source='x.html'),dict(source='x.htm'),dict(source='x.zip')):
            with self.subTest(change=change),patch.object(hw,'owned_io') as work:
                with self.assertRaises(ValueError):hw.attach_homework(NS(**(vars(args)|change)),Path.cwd())
                work.assert_not_called()

    def test_material_dispatch_is_local_homework_only_posix_and_remote_paths_unchanged(self):
        args=NS(kind='homework',source='notes.txt')
        with patch.object(ms.sys,'platform','win32'),patch.object(course,'lesson_path',return_value=Path.cwd()), \
                patch.object(hw,'attach_homework',return_value='new') as attach, \
                patch.object(ms,'acquire',side_effect=AssertionError('No POSIX Attempt')):
            self.assertEqual(ms.attach_material.__wrapped__(args),'new')
            attach.assert_called_once_with(args,Path.cwd())
        for platform,args in [('darwin',args),('win32',NS(kind='homework',source='https://example.test/x.txt')),
                              ('win32',NS(kind='code',source='notes.txt')),
                              ('win32',NS(kind='homework',source='notes.pdf')),
                              ('win32',NS(kind='homework',source='notes.html')),
                              ('win32',NS(kind='homework',source='notes.zip'))]:
            with self.subTest(platform=platform,kind=args.kind),patch.object(ms.sys,'platform',platform), \
                    patch.object(course,'lesson_path',return_value=Path.cwd()),patch.object(hw,'attach_homework') as attach, \
                    patch.object(ms,'acquire',side_effect=ValueError('original path')) as acquire:
                with self.assertRaisesRegex(ValueError,'original path'):ms.attach_material.__wrapped__(args)
                acquire.assert_called_once();attach.assert_not_called()

    def test_public_plain_homework_keeps_byte_exact_singlefile_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'notes.txt';source.write_bytes(b'\xef\xbb\xbf\twork()\r\n')
            args=NS(kind='homework',source=str(source),none=False)
            with patch.object(course,'attach_local',return_value='byte exact') as attach, \
                    patch.object(ms,'attach_material',side_effect=AssertionError('No public normalization')):
                self.assertEqual(course.attach(args),'byte exact')
                attach.assert_called_once_with(args)

    def test_commit_requires_closed_writers_and_expected_revision_homework_cas(self):
        store=hw.Homework(Path.cwd()/'lesson-01')
        store.writers.add(123)
        with patch.object(registry,'update') as update:
            with self.assertRaisesRegex(ValueError,'Writable homework'):store.commit()
            update.assert_not_called()
        store.writers.clear()
        store.expected_revision=7;store.expected_homework=dict(status='old');store.lesson_id=('volume','id')
        store.material=dict(status='provided',file=str(store.directory/'homework/new/x.txt'),sha256='a'*64,
                            revision='sha256:'+'a'*64,origin=dict(source='notes.txt'))
        store.verify=Mock()
        receipt=registry.Receipt('lesson-01',8,'b'*64,'new','a'*64,'old','stage')
        def update(directory,change,**kw):
            self.assertEqual(kw['expected_revision'],7)
            self.assertEqual(kw['registry'].expected_identity,store.lesson_id)
            with self.assertRaises(registry.RevisionConflict):change(dict(id='lesson-01',homework=dict(status='changed')))
            value=dict(id='lesson-01',homework=deepcopy(store.expected_homework))
            change(value)
            self.assertEqual(value['homework']['history'],[store.expected_homework])
            return receipt
        store.read_registry=lambda:dict(homework=deepcopy(store.material))
        with patch.object(registry,'update',side_effect=update):
            self.assertIs(store.commit(),receipt)

    def test_unknown_receipt_is_not_replayed_and_cleanup_retains_primary(self):
        receipt=registry.Receipt('lesson',1,'b'*64,'new','a'*64,'old','stage')
        error=registry.CommitInterrupted(receipt,'unknown');primary=ValueError('primary');error.__cause__=primary
        cleanup=OSError('close')
        fake=NS(directory=Path.cwd(),prepare=Mock(),commit=Mock(side_effect=error),
                close=Mock(side_effect=cleanup),receipt=None)
        with patch.object(hw,'Homework',return_value=fake),patch.object(hw,'owned_io',side_effect=lambda fn:fn()):
            with self.assertRaises(registry.CommitInterrupted) as caught:
                hw.attach_homework(NS(kind='homework',source='notes.txt'),Path.cwd())
        self.assertIs(caught.exception,error);self.assertIs(error.__cause__,primary)
        self.assertEqual(error.cleanup_errors,(cleanup,));fake.commit.assert_called_once();fake.close.assert_called_once()

    def test_successful_commit_followed_by_close_failure_keeps_receipt(self):
        receipt=registry.Receipt('lesson',1,'b'*64,'new','a'*64,'old','stage')
        cleanup=OSError('close after commit')
        fake=NS(directory=Path.cwd(),prepare=Mock(),commit=Mock(return_value=receipt),
                close=Mock(side_effect=cleanup),receipt=receipt)
        with patch.object(hw,'Homework',return_value=fake),patch.object(hw,'owned_io',side_effect=lambda fn:fn()):
            with self.assertRaises(registry.CommitInterrupted) as caught:
                hw.attach_homework(NS(kind='homework',source='notes.txt'),Path.cwd())
        self.assertIs(caught.exception.receipt,receipt)
        self.assertEqual(caught.exception.outcome,'committed')
        self.assertEqual(caught.exception.phase,'homework-close')
        self.assertIs(caught.exception.__cause__,cleanup)
        fake.prepare.assert_called_once();fake.commit.assert_called_once();fake.close.assert_called_once()


@unittest.skipUnless(os.name=='nt','Requires native Windows x64/NTFS; never count models as native acceptance')
class NativeHomework(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.course=self.root/'course';self.lesson=self.course/'lessons/lesson-01';self.lesson.mkdir(parents=True)
        self.registry=self.lesson/'lesson.json'
        self.registry.write_text(json.dumps(dict(id='lesson-01',revision=0,video=dict(source='https://example.test/video'),
            code={},homework=dict(status='not_asked'),resources=dict(items=[]))),encoding='utf-8')
        self.before=self.registry.read_bytes()
        self.source=self.root/'домашка with spaces.txt';self.raw=CASES[1][1];self.source.write_bytes(self.raw)
        self.foreign=self.root/'foreign';self.foreign.mkdir()
        (self.foreign/self.source.name).write_bytes(b'FOREIGN DATA')
        self.progress=self.course/'learners/default/progress.sqlite3';self.progress.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.progress)) as db,db:
            db.execute('CREATE TABLE progress (value TEXT)')
            db.execute('INSERT INTO progress VALUES (?)',('EXISTING PROGRESS',))
        self.progress_bytes=self.progress.read_bytes()

    def args(self):return NS(course=self.course,lesson='lesson-01',profile='default',kind='homework',
                            source=str(self.source),none=False,ref=None)

    def attach(self):
        if threading.current_thread() is not threading.main_thread():
            return ms.attach_material(self.args())  # Do not race over process-global stdout in contention tests.
        with redirect_stdout(io.StringIO()):return ms.attach_material(self.args())

    def material(self):return course.read(self.registry)['homework']

    def intact(self):
        self.assertEqual(self.source.read_bytes(),self.raw)
        self.assertEqual(self.progress.read_bytes(),self.progress_bytes)
        self.assertEqual((self.foreign/self.source.name).read_bytes(),b'FOREIGN DATA')

    def snapshots(self):
        return {p for category in ('materials','homework') if (self.lesson/category).exists()
                for p in (self.lesson/category).iterdir()}

    def test_normalized_round_trip_all_endings_bom_hashes_revision_and_history(self):
        previous=[]
        for name,raw,expected in CASES:
            with self.subTest(name=name):
                self.raw=raw;self.source.write_bytes(raw)
                self.attach();value=self.material()
                origin=value['origin']
                self.assertEqual(Path(origin['raw_file']).read_bytes(),raw)
                self.assertEqual(origin['sha256'],hashlib.sha256(raw).hexdigest())
                self.assertEqual(origin['size'],len(raw))
                self.assertEqual(origin['extraction'],'utf8_text')
                self.assertEqual(Path(value['file']).read_bytes(),expected)
                self.assertNotEqual(Path(value['file']).parent,Path(origin['raw_file']).parent)
                self.assertEqual(value['sha256'],hashlib.sha256(expected).hexdigest())
                self.assertEqual(value['revision'],'sha256:'+value['sha256'])
                self.assertEqual(len(value['history']),len(previous))
                for old,historical in zip(previous,value['history']):
                    self.assertEqual(old['file'],historical['file'])
                    self.assertEqual(Path(historical['file']).read_bytes(),old['bytes'])
                    self.assertEqual(Path(historical['origin']['raw_file']).read_bytes(),old['raw'])
                previous.append(dict(file=value['file'],bytes=expected,raw=raw))
                self.intact()

    def test_public_course_attach_remains_byte_exact_then_normalized_adapter_keeps_history(self):
        self.raw=CASES[-1][1];self.source.write_bytes(self.raw)
        with redirect_stdout(io.StringIO()):course.attach(self.args())
        original=self.material()
        self.assertEqual(Path(original['file']).read_bytes(),self.raw)
        self.assertEqual(original['sha256'],hashlib.sha256(self.raw).hexdigest())
        self.assertNotIn('raw_file',original['origin'])
        self.attach();value=self.material()
        self.assertEqual(Path(value['file']).read_bytes(),CASES[-1][2])
        self.assertEqual(value['history'][-1],{key:value for key,value in original.items() if key!='history'});self.intact()

    def test_both_snapshots_source_and_lesson_are_pinned_and_writers_retired(self):
        commit=hw.Homework.commit;reached=[]
        def attack(store):
            store.verify();reached.append(True)
            self.assertFalse(store.writers)
            self.assertNotEqual(store.inputs[0].file_id,store.file_id)
            for target in (self.source,Path(store.material['origin']['raw_file']),
                           Path(store.material['file']),Path(store.material['file']).parent,self.lesson):
                with self.assertRaises(OSError) as blocked:target.rename(target.with_name(target.name+'-moved'))
                self.assertIn(blocked.exception.winerror,(5,32))
            for value in store.inputs:
                store.security.verify_seal(value.directory,True);store.security.verify_seal(value.handle,False)
            store.security.verify_seal(store.file,False);store.security.verify_seal(store.snapshot,True)
            for folder in (Path(store.material['origin']['raw_file']).parent,Path(store.material['file']).parent):
                with self.assertRaises(OSError):(folder/'unexpected').write_bytes(b'BAD')
            return commit(store)
        with patch.object(hw.Homework,'commit',attack):self.attach()
        self.assertEqual(reached,[True]);self.intact()

    def test_source_mutation_during_read_and_wrong_stamp_never_register(self):
        read=hw.Homework.read;hits=[]
        def attack(store,handle):
            data=read(store,handle)
            if data==self.raw and not hits:
                hits.append(True)
                with self.assertRaises(n.NativeError) as blocked:
                    store.open(store.source_session,store.source_session.home,self.source.name,False,2)
                self.assertEqual(blocked.exception.status,0xC0000043)  # STATUS_SHARING_VIOLATION of held source.
            return data
        with patch.object(hw.Homework,'read',attack):self.attach()
        self.assertEqual(hits,[True]);self.intact()
        before=self.registry.read_bytes()
        stamp=hw.Homework.stamp;observed=[]
        def changed(store,handle):
            value=stamp(store,handle)
            if getattr(store,'source_handle',None)==handle:
                observed.append(True)
                if len(observed)>1:return value[:-1]+(value[-1]^1,)
            return value
        with patch.object(hw.Homework,'stamp',changed):
            with self.assertRaisesRegex(ValueError,'source changed'):self.attach()
        self.assertGreaterEqual(len(observed),2)
        self.assertEqual(self.registry.read_bytes(),before);self.intact()

    def test_symlink_junction_hardlink_and_directory_are_refused(self):
        link=self.root/'linked.txt';os.symlink(self.source,link)
        junction=self.root/'junction'
        result=subprocess.run(['cmd','/d','/c','mklink','/J',str(junction),str(self.foreign)],
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        for source in (link,junction/self.source.name):
            with self.subTest(source=source),patch.object(self,'args',return_value=NS(
                    course=self.course,lesson='lesson-01',kind='homework',source=str(source))):
                with self.assertRaises((ValueError,n.NativeError)) as caught:self.attach()
                if isinstance(caught.exception,n.NativeError):self.assertEqual(caught.exception.status,0xC000050B)
                else:self.assertIn('reparse',str(caught.exception))
        hard=self.root/'hard.txt';os.link(self.source,hard)
        try:
            with patch.object(self,'args',return_value=NS(course=self.course,lesson='lesson-01',kind='homework',source=str(hard))):
                with self.assertRaisesRegex(ValueError,'one link'):self.attach()
        finally:hard.unlink()
        directory=self.root/'directory.txt';directory.mkdir()
        with patch.object(self,'args',return_value=NS(course=self.course,lesson='lesson-01',kind='homework',source=str(directory))):
            with self.assertRaises(n.NativeError) as caught:self.attach()
            self.assertEqual(caught.exception.status,0xC00000BA)
        self.assertEqual(self.registry.read_bytes(),self.before);self.intact()

    def test_raw_and_prepared_collisions_are_exclusive_without_retries(self):
        for category in ('materials','homework'):
            with self.subTest(category=category):
                parent=self.lesson/category;parent.mkdir(exist_ok=True)
                folder=parent/('a'*12);folder.mkdir()
                sentinel=folder/'foreign.txt';sentinel.write_bytes(b'FOREIGN SNAPSHOT')
                calls=[]
                original=resource.uuid.uuid4
                def nonce():
                    calls.append(True)
                    if category=='materials' or len(calls)>=3:return NS(hex='a'*32)
                    return original()
                with patch.object(hw.uuid,'uuid4',nonce):
                    with self.assertRaises(n.NativeError) as caught:self.attach()
                self.assertEqual(caught.exception.status,0xC0000035)
                self.assertEqual(len(calls),1 if category=='materials' else 3)
                self.assertEqual(sentinel.read_bytes(),b'FOREIGN SNAPSHOT')
                self.assertEqual(self.registry.read_bytes(),self.before);self.intact()

    def test_write_flush_seal_and_readback_faults_preserve_raw_and_foreign_data(self):
        for phase in ('raw','homework'):
            for owner,name in ((n.Native,'write'),(n.Native,'flush'),(single.Security,'seal')):
                original=getattr(owner,name);reached=[]
                active=[False]
                write_homework=hw.Homework.write_homework
                def writing(store,payload):
                    active[0]=True
                    return write_homework(store,payload)
                def injected(obj,*args,**kw):
                    if active[0]==(phase=='homework'):
                        reached.append(True);raise OSError('injected '+phase+' '+name)
                    return original(obj,*args,**kw)
                with self.subTest(phase=phase,name=name),patch.object(hw.Homework,'write_homework',writing), \
                        patch.object(owner,name,injected),patch.object(hw.registry,'update') as update:
                    with self.assertRaisesRegex(OSError,'injected '+phase+' '+name):self.attach()
                    self.assertEqual(reached,[True]);update.assert_not_called()
                    self.assertEqual(self.registry.read_bytes(),self.before);self.intact()
            read=hw.Homework.read;reached=[]
            def corrupt(store,handle):
                value=read(store,handle)
                if store.writers and bool(getattr(store,'preparing_homework',False))==(phase=='homework'):
                    reached.append(True);return value+b'CORRUPT'
                return value
            with patch.object(hw.Homework,'read',corrupt):
                with self.assertRaisesRegex(OSError,'readback mismatch'):self.attach()
            self.assertEqual(reached,[True]);self.assertEqual(self.registry.read_bytes(),self.before);self.intact()

    def test_registry_failure_preserves_both_residues_and_explicit_retry_does_not_adopt(self):
        with patch.object(hw.registry,'update',side_effect=OSError('registration failed')) as update:
            with self.assertRaisesRegex(OSError,'registration failed'):self.attach()
            update.assert_called_once()
        residue=self.snapshots()
        self.assertEqual(len(residue),2)
        before={p/q.name:q.read_bytes() for p in residue for q in p.iterdir()}
        self.assertEqual(self.registry.read_bytes(),self.before)
        self.attach()
        self.assertEqual(len(self.snapshots()),4)
        self.assertTrue(residue.issubset(self.snapshots()))
        self.assertEqual({p:p.read_bytes() for p in before},before);self.intact()

    def test_two_writers_reach_commit_barrier_one_revision_cas_wins(self):
        commit=hw.Homework.commit;barrier=threading.Barrier(2,timeout=15);reached=[]
        def competing(store):
            reached.append(store.expected_revision);barrier.wait()
            return commit(store)
        def writer():
            try:self.attach();return 'committed'
            except registry.RevisionConflict:return 'conflict'
        with patch.object(hw.Homework,'commit',competing),ThreadPoolExecutor(max_workers=2) as pool:
            jobs=[pool.submit(writer) for _ in range(2)]
            outcomes=[job.result(timeout=30) for job in jobs]
        self.assertEqual(reached,[0,0]);self.assertCountEqual(outcomes,['committed','conflict'])
        self.assertEqual(course.read(self.registry)['revision'],1)
        self.assertEqual(len(self.snapshots()),4);self.intact()

    def test_wrong_lesson_identity_refuses_before_any_snapshot(self):
        data=course.read(self.registry);data['id']='foreign'
        self.registry.write_text(json.dumps(data),encoding='utf-8');before=self.registry.read_bytes()
        with self.assertRaisesRegex(ValueError,'lesson identity'):self.attach()
        self.assertEqual(self.registry.read_bytes(),before);self.assertEqual(self.snapshots(),set());self.intact()

    def test_postcommit_receipt_recovery_and_readback_preserve_both_snapshots(self):
        verify=hw.Homework.verify;receipts=[]
        def fail(store):
            if store.receipt is not None:
                receipts.append(store.receipt);raise OSError('homework after commit')
            return verify(store)
        with patch.object(hw.Homework,'verify',fail):
            with self.assertRaises(registry.CommitInterrupted) as caught:self.attach()
        self.assertEqual(caught.exception.outcome,'committed');self.assertEqual(str(caught.exception.__cause__),'homework after commit')
        self.assertIs(caught.exception.receipt,receipts[0])
        self.assertEqual(registry.recover(self.lesson,receipts[0]),'committed')
        value=self.material()
        self.assertEqual(Path(value['origin']['raw_file']).read_bytes(),self.raw)
        self.assertEqual(Path(value['file']).read_bytes(),hw.normalize(self.raw))
        self.assertEqual(len(self.snapshots()),2);self.intact()

    def test_utf8_size_and_document_refusals_do_not_replace_existing_homework(self):
        self.attach();before=self.registry.read_bytes()
        for raw in (b'%PDF-1.4',b'PK\x03\x04zip',b'<html>login</html>',b'bad\0binary',b'\xff',b' \n'):
            self.source.write_bytes(raw)
            with self.subTest(raw=raw),self.assertRaises(ValueError):self.attach()
            self.assertEqual(self.registry.read_bytes(),before)
        self.source.write_bytes(self.raw)
        with patch.object(hw,'MAX_BYTES',len(self.raw)-1):
            with self.assertRaises(ValueError):self.attach()
        self.assertEqual(self.registry.read_bytes(),before);self.intact()

    def test_all_handles_close_once_before_successful_return(self):
        close=single.SingleFile.close;seen=[]
        def checked(store):
            result=close(store)
            self.assertTrue(all(not s.handles for s in store.sessions));self.assertFalse(store.writers)
            seen.append(store)
            return result
        with patch.object(single.SingleFile,'close',checked):self.attach()
        self.assertGreaterEqual(len(seen),1);self.intact()


if __name__=='__main__':unittest.main()
