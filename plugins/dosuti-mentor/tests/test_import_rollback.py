"""Only unregistered files of the current import may be discarded."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
from pathlib import Path
import threading
import subprocess
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import test_persistence
import course
import material_sources as materials


class ImportRollback(unittest.TestCase):
    def setUp(self):
        self.f=test_persistence.PersistenceRegression();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.directory=self.f.path.resolve()/'lessons/lesson-01'
        self.source=self.f.directory/'source.txt';self.source.write_text('user original')

    def attach(self,kind,source=None):
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(self.f.args(kind=kind,source=str(source or self.source),none=False,ref=None,resource_type='file'))

    def files(self):
        return {str(p.relative_to(self.directory)):p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}

    def test_failed_registration_preserves_existing_materials_and_removes_attempt(self):
        for kind in ('code','homework','resources'):
            with self.subTest(kind=kind):
                self.attach(kind)
                before=self.files()
                with patch.object(course,'update_lesson',side_effect=KeyboardInterrupt('cancel import')):
                    with self.assertRaises(KeyboardInterrupt):self.attach(kind)
                self.assertEqual(self.files(),before)
                self.assertEqual(self.source.read_text(),'user original')

    def test_failed_sealing_removes_even_readonly_snapshot(self):
        seal=course.seal_snapshot
        def fail(folder):seal(folder);raise ValueError('seal failed')
        before=self.files()
        with patch.object(course,'seal_snapshot',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'seal failed'):self.attach('code')
        self.assertEqual(self.files(),before)
        self.assertFalse(list((self.directory/'code').iterdir()))

    def test_rejected_link_does_not_delete_foreign_target(self):
        folder=self.f.directory/'code-source';folder.mkdir()
        try:(folder/'link').symlink_to(self.source)
        except OSError:self.skipTest('Symlink creation unavailable')
        with self.assertRaises(ValueError):self.attach('code',folder)
        self.assertEqual(self.source.read_text(),'user original')
        self.assertFalse(list((self.directory/'code').iterdir()))

    def test_error_after_atomic_save_preserves_registered_copy_and_raw_origin(self):
        source=self.f.directory/'homework.html';source.write_text('<p>Task</p>')
        save=course.save
        def fail(path,value):save(path,value);raise OSError('after atomic save')
        with patch.object(course,'save',side_effect=fail):
            with self.assertRaisesRegex(OSError,'after atomic save'):self.attach('homework',source)
        value=course.read(self.directory/'lesson.json')['homework']
        self.assertTrue(Path(value['file']).is_file())
        self.assertTrue(Path(value['origin']['raw_file']).is_file())
        self.assertEqual(course.homework_integrity(value),'verified')
        self.assertFalse(list((self.directory/'materials').glob('*/extracted-*')))

    def test_resource_text_survives_error_after_its_registration(self):
        self.attach('resources')
        row=course.read(self.directory/'lesson.json')['resources']['items'][0]
        save=course.save
        def fail(path,value):save(path,value);raise OSError('after atomic save')
        with patch.object(course,'save',side_effect=fail):
            with self.assertRaises(OSError):
                materials.resource(self.f.args(action='extract',resource_id=row['id'],expected_revision=None,notes=None))
        current=materials.resource(self.f.args(action='list'))['items'][0]
        self.assertEqual(current['text_integrity'],'verified')
        self.assertEqual(current['source_integrity'],'verified')

    def test_duplicate_import_retains_history(self):
        for _ in range(2):self.attach('code')
        row=course.read(self.directory/'lesson.json')['code']
        paths=[row['snapshot']]+[v['snapshot'] for v in row['history'] if 'snapshot' in v]
        self.assertEqual(len(set(paths)),2)
        self.assertTrue(all(Path(p).is_dir() for p in paths))

    def test_concurrent_failure_cannot_remove_successful_import(self):
        self.attach('code')
        barrier=threading.Barrier(2);local=threading.local();update=course.update_lesson
        def guarded(directory,change):
            barrier.wait(timeout=5)
            if local.fail:raise ValueError('one attempt failed')
            return update(directory,change)
        def worker(fail):
            local.fail=fail
            try:course.attach(self.f.args(kind='code',source=str(self.source),none=False,ref=None,resource_type='file'))
            except ValueError:
                if not fail:raise
        with patch.object(course,'update_lesson',side_effect=guarded),contextlib.redirect_stdout(io.StringIO()),ThreadPoolExecutor(2) as pool:
            jobs=[pool.submit(worker,flag) for flag in (True,False)]
            for job in jobs:job.result(timeout=10)
        row=course.read(self.directory/'lesson.json')['code']
        self.assertEqual(course.verify_snapshot(row),'snapshot_hashes_match')
        self.assertEqual(len(list((self.directory/'code').iterdir())),2)

    def test_file_name_collision_preserves_unowned_file(self):
        target=self.directory/'homework'/('a'*8+'-'+self.source.name)
        target.parent.mkdir();target.write_text('foreign file')
        with patch.object(course.uuid,'uuid4',return_value=NS(hex='a'*32)):
            with self.assertRaises(FileExistsError):self.attach('homework')
        self.assertEqual(target.read_text(),'foreign file')

    def test_failed_prepared_import_removes_only_new_copy(self):
        source=self.f.packet();copytree=course.shutil.copytree
        def fail(src,dst,*a,**kw):
            if Path(src).resolve()==(source/'frames').resolve():raise OSError('copy failed')
            return copytree(src,dst,*a,**kw)
        with patch.object(course.shutil,'copytree',side_effect=fail):
            with self.assertRaises(OSError):course.prepare(self.f.args(import_prepared=source))
        self.assertTrue((source/'manifest.json').exists())
        self.assertFalse(list((self.directory/'prepared').glob('import-*')))

    def test_interrupted_copy_removes_partial_file(self):
        before=self.files()
        def fail(src,dst,*a,**kw):
            Path(dst).write_bytes(b'partial');raise KeyboardInterrupt('copy interrupted')
        with patch.object(course.shutil,'copyfile',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.attach('homework')
        self.assertEqual(self.files(),before)
        self.assertEqual(self.source.read_text(),'user original')

    def test_parallel_successful_duplicates_keep_both_snapshots(self):
        args=self.f.args(kind='code',source=str(self.source),none=False,ref=None,resource_type='file')
        with contextlib.redirect_stdout(io.StringIO()),ThreadPoolExecutor(2) as pool:
            jobs=[pool.submit(course.attach,args) for _ in range(2)]
            for job in jobs:job.result(timeout=10)
        row=course.read(self.directory/'lesson.json')['code']
        paths=[row['snapshot']]+[v['snapshot'] for v in row['history'] if 'snapshot' in v]
        self.assertEqual(len(set(paths)),2)
        self.assertTrue(all(Path(p).is_dir() for p in paths))

    def test_registered_snapshot_in_history_is_not_rolled_back(self):
        update=course.update_lesson
        def fail_after_replacement(directory,change):
            update(directory,change)
            with patch.object(course,'update_lesson',update):self.attach('code')
            raise OSError('after another registration')
        with patch.object(course,'update_lesson',side_effect=fail_after_replacement):
            with self.assertRaises(OSError):self.attach('code')
        row=course.read(self.directory/'lesson.json')['code']
        self.assertEqual(len([p for p in row['history'] if 'snapshot' in p]),1)
        self.assertTrue(all(Path(v['snapshot']).is_dir() for v in [row]+row['history'] if 'snapshot' in v))

    def test_unreadable_registry_preserves_uncertain_attempt(self):
        def fail(directory,change):
            (directory/'lesson.json').write_text('{');raise ValueError('registration failed')
        errors=io.StringIO()
        with patch.object(course,'update_lesson',side_effect=fail),contextlib.redirect_stderr(errors):
            with self.assertRaisesRegex(ValueError,'registration failed'):self.attach('code')
        self.assertIn('preserved',errors.getvalue())
        self.assertEqual(len(list((self.directory/'code').iterdir())),1)
        self.assertEqual(self.source.read_text(),'user original')

    def test_registered_prepared_import_survives_post_commit_error(self):
        source=self.f.packet();save=course.save
        def fail(path,value):save(path,value);raise OSError('after atomic save')
        with patch.object(course,'save',side_effect=fail):
            with self.assertRaises(OSError):course.prepare(self.f.args(import_prepared=source))
        row=course.read(self.directory/'lesson.json')['prepared_fragments'][0]
        self.assertTrue(Path(row['folder']).is_dir())

    def test_interrupted_clone_discards_partial_checkout(self):
        before=self.files()
        def fail(command,**kw):
            target=Path(command[-1]);target.mkdir(parents=True,exist_ok=True)
            (target/'partial').write_text('incomplete clone')
            raise subprocess.CalledProcessError(1,command)
        with patch.object(course.subprocess,'run',side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):self.attach('code','https://github.com/example/repo')
        self.assertEqual(self.files(),before)
        self.assertFalse(list((self.directory/'code').iterdir()))

    def test_resource_cas_failure_removes_new_text_but_preserves_old_raw(self):
        self.attach('resources')
        row=course.read(self.directory/'lesson.json')['resources']['items'][0]
        update=course.update_lesson
        def replace(directory,change):
            update(directory,lambda data:data.update(resources=dict(status='absent',items=[])))
            return update(directory,change)
        with patch.object(course,'update_lesson',side_effect=replace):
            with self.assertRaisesRegex(ValueError,'concurrently'):
                materials.resource(self.f.args(action='extract',resource_id=row['id'],expected_revision=None,notes=None))
        self.assertTrue(Path(row['origin']['raw_file']).is_file())
        self.assertFalse(list((self.directory/'resources').glob('text-*')))

    def test_legacy_case_alias_does_not_break_registration_cleanup(self):
        data=course.read(self.directory/'lesson.json');data['id']=data['id'].upper()
        course.save(self.directory/'lesson.json',data)
        self.attach('homework')
        row=course.read(self.directory/'lesson.json')['homework']
        self.assertTrue(Path(row['file']).exists())
