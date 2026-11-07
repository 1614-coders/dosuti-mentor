"""Portable PDF intake contracts and real Windows NTFS security cases."""
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
import pdf_fixtures
import registry_update as registry
import safe_material_import as safe
import windows_managed_home as n
import windows_material_import as single
import windows_pdf_intake as pdf

PAYLOAD = pdf_fixtures.textbook()  # Existing real PDF fixture; no parser/dependency required.


class PDFModels(unittest.TestCase):
    def test_classification_is_pdf_bytes_not_text_html_zip_or_a_parser(self):
        pdf.validate_pdf(PAYLOAD)
        for value in (b'', b'<html>PDF</html>', b'UTF-8 notes', b'PK\x03\x04', b'%PDF'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'PDF signature'):
                pdf.validate_pdf(value)
        with patch.object(pdf, 'MAX_BYTES', len(PAYLOAD)-1):
            with self.assertRaisesRegex(ValueError, '32 MiB'): pdf.validate_pdf(PAYLOAD)

    def test_only_one_local_pdf_resource_is_accepted_before_native_io(self):
        args = NS(kind='resources', source='notes.PDF', resource_type='file')
        self.assertEqual(pdf.source_path(args).name, 'notes.PDF')
        for change in (dict(kind='code'), dict(kind='homework'), dict(resource_type='video'),
                       dict(source='https://example.test/a.pdf'), dict(source='notes.html'),
                       dict(material_text='extracted'), dict(material_origin={})):
            with self.subTest(change=change), patch.object(pdf, 'owned_io') as run:
                with self.assertRaises(ValueError): pdf.attach_pdf(NS(**(vars(args)|change)), Path.cwd())
                run.assert_not_called()

    def test_windows_dispatch_and_posix_boundary(self):
        args = NS(kind='resources', source='notes.pdf', resource_type='article')
        with patch.object(safe.sys, 'platform', 'win32'), patch.object(pdf, 'attach_pdf', return_value='PDF') as run:
            self.assertEqual(safe.attach(args, Path.cwd()), 'PDF')
            run.assert_called_once_with(args, Path.cwd())
        with patch.object(safe.sys, 'platform', 'linux'), patch.object(safe, 'current', side_effect=ValueError('POSIX path')) as current, \
                patch.object(pdf, 'attach_pdf') as run:
            with self.assertRaisesRegex(ValueError, 'POSIX path'): safe.attach(args, Path.cwd())
            current.assert_called_once(); run.assert_not_called()

    def test_registered_fetch_uses_handle_backend_not_path_integrity(self):
        row = dict(id='pdf', kind='file', origin=dict(media_type='application/pdf'))
        args = NS(action='fetch', resource_id='pdf')
        with patch.object(ms.sys, 'platform', 'win32'), patch.object(course, 'lesson_path', return_value=Path.cwd()), \
                patch.object(course, 'read', return_value=dict(resources=dict(items=[row]))), \
                patch.object(pdf, 'fetch_pdf', return_value={'verified':True}) as run, \
                patch.object(ms, 'integrity', side_effect=AssertionError('No pathname rehash')):
            self.assertEqual(ms._resource.__wrapped__(args), {'verified':True})
            run.assert_called_once_with(args, Path.cwd(), row)
        with self.assertRaises(ValueError): pdf.fetch_pdf(NS(action='extract'), Path.cwd(), row)

    def test_read_rejects_foreign_pointer_size_or_stale_row_before_snapshot_open(self):
        row=dict(id='pdf',kind='file',source=str(Path.cwd()/'resources/one/a.pdf'),
                 origin=dict(source=str(Path.cwd()/'a.pdf'),raw_file=str(Path.cwd()/'resources/one/a.pdf'),
                             size=10,sha256='a'*64,media_type='application/pdf'))
        for change in (dict(size=True),dict(size=-1),dict(raw_file=str(Path.cwd()/'foreign.pdf')),
                       dict(source='https://example.test/a.pdf')):
            altered=deepcopy(row); altered['origin'].update(change)
            store=pdf.PDFReader(Path.cwd(),NS(),altered)
            session=NS(api=NS(),home=123,inspect=Mock(return_value=(('volume','id'),0)))
            with self.subTest(change=change),patch.object(store,'pin',return_value=session), \
                    patch.object(pdf,'Security'),patch.object(store,'read_registry',return_value=dict(
                        resources=dict(items=[altered]))),patch.object(store,'checked_input') as checked:
                with self.assertRaisesRegex(ValueError,'Invalid registered PDF origin'): store.prepare_pdf()
                checked.assert_not_called()

    def test_commit_requires_retired_writers(self):
        store = pdf.PDFIntake(Path.cwd()); store.writers.add(123)
        with patch.object(registry, 'update') as update:
            with self.assertRaisesRegex(ValueError, 'Writable PDF'): store.commit(NS(), Path('x.pdf'))
            update.assert_not_called()

    def test_registry_receives_expected_revision_bound_identity_and_exact_origin(self):
        store = pdf.PDFIntake(Path.cwd()/'lesson-01')
        store.folder, store.name = store.directory/'resources'/'one', 'x.pdf'
        store.checksum, store.size = hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD)
        store.expected_revision, store.lesson_id = 7, ('volume','identity')
        store.verify = Mock(return_value=PAYLOAD)
        store.read_registry = lambda: dict(resources=dict(items=[deepcopy(store.item)]))
        receipt = registry.Receipt('lesson-01',8,'b'*64,'new','a'*64,'old','stage')
        def update(directory, change, **kwargs):
            self.assertEqual(kwargs['expected_revision'],7)
            self.assertEqual(kwargs['registry'].expected_identity,store.lesson_id)
            data=dict(id='lesson-01',resources=dict(items=[]))
            change(data)
            self.assertEqual(data['resources']['items'],[store.item])
            return receipt
        with patch.object(registry,'update',side_effect=update):
            self.assertIs(store.commit(NS(kind='resources',resource_type='file'),Path('x.pdf')),receipt)
        self.assertEqual(store.item['origin'],dict(source='x.pdf',raw_file=str(store.folder/'x.pdf'),
            sha256=store.checksum,size=len(PAYLOAD),media_type='application/pdf'))
        self.assertNotIn('extraction',store.item)

    def test_unknown_commit_preserves_primary_receipt_cleanup_and_never_replays(self):
        receipt=registry.Receipt('lesson',1,'b'*64,'new','a'*64,'old','stage')
        error=registry.CommitInterrupted(receipt,'unknown')
        primary=ValueError('primary'); error.__cause__=primary
        cleanup=OSError('close')
        attempt=NS(directory=Path.cwd(),receipt=None,close=Mock(side_effect=cleanup))
        operation=Mock(side_effect=error)
        with patch.object(pdf,'owned_io',side_effect=lambda fn:fn()):
            with self.assertRaises(registry.CommitInterrupted) as caught: pdf.execute(attempt,operation)
        self.assertIs(caught.exception,error); self.assertIs(error.__cause__,primary)
        self.assertIs(error.receipt,receipt); self.assertEqual(error.cleanup_errors,(cleanup,))
        operation.assert_called_once(); attempt.close.assert_called_once()

    def test_pdf_stays_excluded_from_existing_utf8_backend(self):
        from windows_resource_text import eligible
        row=dict(kind='file',source='x.pdf',origin=dict(source='x.pdf',raw_file='x.pdf',
                    media_type='application/pdf',sha256='a'*64))
        self.assertFalse(eligible(row,NS(action='extract',visual_pages=None)))
        for kind in ('code','homework'):
            with patch.object(single,'owned_io') as run:
                with self.assertRaises(ValueError): single.attach_single(NS(kind=kind,source='x.pdf'),Path.cwd())
                run.assert_not_called()


@unittest.skipUnless(os.name=='nt','Requires native Windows x64/NTFS; models are not native acceptance')
class NativePDFIntake(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.directory=self.root/'course'/'lessons'/'lesson-01'; self.directory.mkdir(parents=True)
        self.registry=self.directory/'lesson.json'
        self.registry.write_text(json.dumps(dict(id='lesson-01',revision=0,code={},homework={},
            resources=dict(items=[]),video=dict(source='https://example.test/video'))),encoding='utf-8')
        self.before=self.registry.read_bytes()
        self.source=self.root/'пособие with spaces.PDF'; self.source.write_bytes(PAYLOAD)
        self.foreign=self.root/'foreign'; self.foreign.mkdir()
        (self.foreign/self.source.name).write_bytes(b'FOREIGN DATA')
        self.progress=self.root/'course'/'learners'/'default'/'progress.sqlite3'
        self.progress.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.progress)) as db, db:
            db.execute('CREATE TABLE progress (value TEXT)')
            db.execute('INSERT INTO progress VALUES (?)',('EXISTING PROGRESS',))
        self.progress_bytes=self.progress.read_bytes()

    def args(self): return NS(kind='resources',resource_type='file',source=str(self.source))

    def attach(self):
        from material_import import _CURRENT, cleanup
        state=dict(directory=self.directory,owned=[]); token=_CURRENT.set(state)
        try:
            with redirect_stdout(io.StringIO()): result=safe.attach(self.args(),self.directory)
            cleanup(state)
            return result
        finally:
            try:
                if state.get('safe') is not None: state['safe'].close()
            finally: _CURRENT.reset(token)

    def row(self): return course.read(self.registry)['resources']['items'][-1]

    def fetch(self,row=None):
        original=row or self.row()
        return pdf.fetch_pdf(NS(action='fetch'),self.directory,original)

    def intact(self):
        self.assertEqual(self.source.read_bytes(),PAYLOAD)
        self.assertEqual(self.progress.read_bytes(),self.progress_bytes)
        self.assertEqual((self.foreign/self.source.name).read_bytes(),b'FOREIGN DATA')

    def test_round_trip_exact_bytes_sha_origin_and_read_only_fetch(self):
        close=single.SingleFile.close; retired=[]
        def checked_close(store):
            close(store)
            self.assertTrue(all(not session.handles for session in store.sessions))
            self.assertFalse(store.writers)
            retired.append(store)
        with patch.object(single.SingleFile,'close',checked_close): receipt=self.attach()
        self.assertGreaterEqual(len(retired),1)
        row=self.row(); raw=Path(row['origin']['raw_file'])
        self.assertIsInstance(receipt,registry.Receipt)
        self.assertEqual(raw.read_bytes(),PAYLOAD)
        self.assertEqual(row['origin'],dict(source=str(self.source),raw_file=str(raw),
            sha256=hashlib.sha256(PAYLOAD).hexdigest(),size=len(PAYLOAD),media_type='application/pdf'))
        before=self.registry.read_bytes()
        with patch.object(pdf.PDFIntake,'prepare',side_effect=AssertionError('No replay')), \
                patch.object(ms,'integrity',side_effect=AssertionError('No pathname hash')):
            for _ in range(2):
                self.assertEqual(self.fetch()['items'][-1]['source_integrity'],'verified')
        self.assertEqual(self.registry.read_bytes(),before); self.intact()

    def test_sealing_and_source_output_lesson_bindings_hold_through_commit(self):
        commit=pdf.PDFIntake.commit; hits=[]
        def attack(store,*args):
            store.verify(); hits.append(True)
            self.assertFalse(store.writers)
            store.security.verify_seal(store.file,False)
            store.security.verify_seal(store.snapshot,True)
            for target in (store.folder/store.name,store.folder,self.directory):
                with self.assertRaises(OSError) as blocked: target.rename(target.with_name(target.name+'-moved'))
                self.assertIn(blocked.exception.winerror,(5,32))
            with self.assertRaises(OSError): (store.folder/'foreign.txt').write_bytes(b'BAD')
            with self.assertRaises(OSError): (store.folder/store.name).write_bytes(b'BAD')
            return commit(store,*args)
        with patch.object(pdf.PDFIntake,'commit',attack): self.attach()
        self.assertEqual(hits,[True]); self.fetch(); self.intact()

    def test_source_substitution_during_read_is_blocked_not_just_early_refusal(self):
        read=pdf.PDFIntake.read; hits=[]
        def attack(store,handle):
            data=read(store,handle)
            if data==PAYLOAD and not store.writers and not hits:
                hits.append(True)
                with self.assertRaises(OSError) as blocked: self.source.rename(self.root/'moved.pdf')
                self.assertIn(blocked.exception.winerror,(5,32))
            return data
        with patch.object(pdf.PDFIntake,'read',attack): self.attach()
        self.assertEqual(hits,[True]); self.intact()

    def test_source_symlink_junction_and_hardlink_are_refused(self):
        link=self.root/'linked.pdf'; os.symlink(self.source,link)
        junction=self.root/'junction'
        result=subprocess.run(['cmd','/d','/c','mklink','/J',str(junction),str(self.foreign)],
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        for source in (link,junction/self.source.name):
            with self.subTest(source=source),patch.object(self,'args',return_value=NS(
                    kind='resources',resource_type='file',source=str(source))):
                with self.assertRaises((ValueError,n.NativeError)) as caught: self.attach()
                if isinstance(caught.exception,n.NativeError):
                    self.assertEqual(caught.exception.status,0xC000050B)  # STATUS_REPARSE_POINT_ENCOUNTERED
                else: self.assertIn('reparse',str(caught.exception))
                self.assertEqual(self.registry.read_bytes(),self.before)
        hard=self.root/'hard.pdf'; os.link(self.source,hard)
        try:
            with patch.object(self,'args',return_value=NS(kind='resources',resource_type='file',source=str(hard))):
                with self.assertRaisesRegex(ValueError,'one link'): self.attach()
            self.assertEqual(self.registry.read_bytes(),self.before)
        finally: hard.unlink()
        directory=self.root/'directory.pdf'; directory.mkdir()
        with patch.object(self,'args',return_value=NS(kind='resources',resource_type='file',source=str(directory))):
            with self.assertRaises(n.NativeError) as caught: self.attach()
            self.assertEqual(caught.exception.status,0xC00000BA)  # STATUS_FILE_IS_A_DIRECTORY
        self.assertEqual(self.registry.read_bytes(),self.before)
        self.intact()

    def test_collision_preserves_foreign_snapshot_and_never_retries(self):
        parent=self.directory/'resources'; parent.mkdir()
        folder=parent/('a'*12); folder.mkdir()
        sentinel=folder/'foreign.pdf'; sentinel.write_bytes(b'FOREIGN SNAPSHOT')
        with patch.object(pdf.uuid,'uuid4',return_value=NS(hex='a'*32)) as nonce:
            with self.assertRaises(n.NativeError) as caught: self.attach()
        self.assertEqual(caught.exception.status,0xC0000035); nonce.assert_called_once()
        self.assertEqual(sentinel.read_bytes(),b'FOREIGN SNAPSHOT')
        self.assertEqual(self.registry.read_bytes(),self.before); self.intact()

    def test_write_flush_seal_and_readback_faults_never_register(self):
        for owner,name in ((n.Native,'write'),(n.Native,'flush'),(single.Security,'seal')):
            with self.subTest(name=name),patch.object(owner,name,side_effect=OSError('PDF '+name+' failed')), \
                    patch.object(pdf.registry,'update') as update:
                with self.assertRaisesRegex(OSError,'PDF '+name+' failed'): self.attach()
                update.assert_not_called()
                self.assertEqual(self.registry.read_bytes(),self.before); self.intact()
        read=pdf.PDFIntake.read
        def corrupt(store,handle):
            data=read(store,handle)
            return data+b'BAD' if store.writers else data
        with patch.object(pdf.PDFIntake,'read',corrupt):
            with self.assertRaisesRegex(OSError,'PDF readback mismatch'): self.attach()
        self.assertEqual(self.registry.read_bytes(),self.before); self.intact()

    def test_registration_failure_retains_residue_explicit_new_attempt_does_not_adopt_it(self):
        with patch.object(pdf.registry,'update',side_effect=OSError('PDF registration failed')) as update:
            with self.assertRaisesRegex(OSError,'PDF registration failed'): self.attach()
            update.assert_called_once()
        self.assertEqual(self.registry.read_bytes(),self.before)
        residue=next((self.directory/'resources').iterdir())
        old={p.name:p.read_bytes() for p in residue.iterdir()}
        self.attach()
        self.assertNotEqual(Path(self.row()['source']).parent,residue)
        self.assertEqual({p.name:p.read_bytes() for p in residue.iterdir()},old); self.intact()

    def test_two_writers_reach_commit_barrier_exactly_one_revision_wins(self):
        commit=pdf.PDFIntake.commit; barrier=threading.Barrier(2,timeout=15); reached=[]
        def race(store,*args):
            reached.append(store.expected_revision); barrier.wait()
            return commit(store,*args)
        def writer():
            try: return self.attach()
            except registry.RevisionConflict as error: return error
        with patch.object(pdf.PDFIntake,'commit',race),ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(writer) for _ in range(2)]
            results=[f.result(timeout=30) for f in futures]
        self.assertEqual(reached,[0,0])
        self.assertEqual(sum(isinstance(x,registry.Receipt) for x in results),1)
        self.assertEqual(sum(isinstance(x,registry.RevisionConflict) for x in results),1)
        self.assertEqual(course.read(self.registry)['revision'],1)
        self.assertEqual(len(course.read(self.registry)['resources']['items']),1)
        self.assertEqual(len(list((self.directory/'resources').iterdir())),2)
        self.fetch(); self.intact()

    def test_wrong_lesson_registry_identity_fails_before_snapshot(self):
        value=course.read(self.registry); value['id']='other-lesson'
        self.registry.write_text(json.dumps(value),encoding='utf-8'); before=self.registry.read_bytes()
        with self.assertRaisesRegex(ValueError,'lesson identity'): self.attach()
        self.assertFalse((self.directory/'resources').exists())
        self.assertEqual(self.registry.read_bytes(),before); self.intact()

    def test_postcommit_receipt_recovery_and_fetch_do_not_replay(self):
        read=pdf.PDFIntake.read_registry; receipts=[]
        def fail(store):
            if store.receipt is not None:
                receipts.append(store.receipt); raise OSError('PDF after commit')
            return read(store)
        with patch.object(pdf.PDFIntake,'read_registry',fail):
            with self.assertRaises(registry.CommitInterrupted) as caught: self.attach()
        self.assertEqual(caught.exception.outcome,'committed')
        self.assertEqual(str(caught.exception.__cause__),'PDF after commit')
        self.assertIs(caught.exception.receipt,receipts[0])
        self.assertEqual(registry.recover(self.directory,receipts[0]),'committed')
        before=self.registry.read_bytes(); folders=set((self.directory/'resources').iterdir())
        with patch.object(pdf.PDFIntake,'prepare',side_effect=AssertionError('No recovery replay')): self.fetch()
        self.assertEqual(self.registry.read_bytes(),before)
        self.assertEqual(set((self.directory/'resources').iterdir()),folders); self.intact()

    def test_repeat_read_refuses_sha_size_or_resource_cas_changes(self):
        self.attach(); row=self.row(); before=self.registry.read_bytes()
        for change in (dict(sha256='0'*64),dict(size=len(PAYLOAD)+1),dict(raw_file=str(self.foreign/self.source.name))):
            altered=deepcopy(row); altered['origin'].update(change)
            value=course.read(self.registry); value['resources']['items']=[altered]
            self.registry.write_text(json.dumps(course.anchored(value,self.registry)),encoding='utf-8')
            with self.assertRaises(ValueError): self.fetch(altered)
        self.registry.write_bytes(before)
        stale=deepcopy(row); stale['status']='concurrent'
        with self.assertRaisesRegex(ValueError,'concurrently'): self.fetch(stale)
        self.assertEqual(self.registry.read_bytes(),before); self.intact()

    def test_snapshot_substitution_during_verified_repeat_read_is_blocked(self):
        self.attach(); verify=pdf.PDFReader.verify_input; hits=[]
        def attack(store,value):
            data=verify(store,value)
            if not hits:
                hits.append(True)
                for target in (value.path,value.path.parent):
                    with self.assertRaises(OSError) as blocked: target.rename(target.with_name(target.name+'-moved'))
                    self.assertIn(blocked.exception.winerror,(5,32))
            return verify(store,value)
        with patch.object(pdf.PDFReader,'verify_input',attack): self.fetch()
        self.assertEqual(hits,[True]); self.intact()

    def test_signature_size_and_unexpected_child_fail_without_registry_damage(self):
        self.source.write_bytes(b'<html>not PDF</html>')
        try:
            with self.assertRaisesRegex(ValueError,'PDF signature'): self.attach()
        finally: self.source.write_bytes(PAYLOAD)
        with patch.object(pdf,'MAX_BYTES',len(PAYLOAD)-1):
            with self.assertRaisesRegex(ValueError,'32 MiB'): self.attach()
        seal=single.Security.seal; created=[]
        def extra(security,handle,directory):
            if directory:
                folder=next((self.directory/'resources').iterdir())
                path=folder/'foreign-extra'; path.write_bytes(b'FOREIGN CHILD'); created.append(path)
            return seal(security,handle,directory)
        with patch.object(single.Security,'seal',extra):
            with self.assertRaisesRegex(ValueError,'inventory changed'): self.attach()
        self.assertEqual(len(created),1); self.assertEqual(created[0].read_bytes(),b'FOREIGN CHILD')
        self.assertEqual(self.registry.read_bytes(),self.before); self.intact()


if __name__=='__main__': unittest.main()
