import contextlib,io,json,os,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,learning_tools as lt,profile_storage
import test_context

class IntegrityFixes(unittest.TestCase):
    def setUp(self):
        self.f=test_context.ContextRegression();self.f.setUp();self.a=self.f.args
        self.folder=self.f.add('qa',0,60,[(1,5,'test')])
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return NS(**(vars(self.a)|kw))
    def test_links_at_each_profile_component(self):
        outside=self.f.directory/'outside';outside.mkdir()
        for component in ('learners','profile','db','journal','wal','shm'):
            with self.subTest(component=component):
                root=self.f.directory/component;root.mkdir();p=root/'learners'/'default'
                if component=='learners':(root/'learners').symlink_to(outside,target_is_directory=True)
                elif component=='profile':p.parent.mkdir();p.symlink_to(outside,target_is_directory=True)
                else:
                    p.mkdir(parents=True);target=outside/component;target.write_bytes(b'unchanged')
                    suffix={'db':'','journal':'-journal','wal':'-wal','shm':'-shm'}[component]
                    (p/('progress.sqlite3'+suffix)).symlink_to(target)
                before={f.name:f.read_bytes() for f in outside.iterdir() if f.is_file()}
                with self.assertRaises(ValueError):profile_storage.open_progress(root,'default')
                self.assertEqual(before,{f.name:f.read_bytes() for f in outside.iterdir() if f.is_file()})
    def test_hard_link_rejected(self):
        p=self.a.course/'learners/default';p.mkdir(parents=True);target=self.f.directory/'other.db';target.touch();os.link(target,p/'progress.sqlite3')
        with self.assertRaises(ValueError):course.db(self.a)
    def test_path_swap_during_connect_does_not_initialize_other_database(self):
        target=self.f.directory/'bob.sqlite3';sqlite3.connect(target).close();original=sqlite3.connect
        p=self.a.course/'learners/default/progress.sqlite3'
        def swap(*a,**k):
            p.rename(p.with_suffix('.old'));p.symlink_to(target);return original(*a,**k)
        with patch.object(profile_storage.sqlite3,'connect',side_effect=swap):
            with self.assertRaises(ValueError):course.db(self.a)
        with original(target) as db:self.assertEqual(db.execute('select name from sqlite_master').fetchall(),[])
    def test_normal_database_still_works(self):
        with course.db(self.a) as db:self.assertEqual(db.execute('select count(*) from events').fetchone()[0],0)
    def test_corrupt_cursor_returns_invalid_and_preserves_bytes(self):
        p=lt.learner(self.a)/'cursor.json';p.parent.mkdir(parents=True)
        for raw in ['{','[]','{"revision":1,"state":{"lesson":17}}','{"revision":-1,"state":{}}']:
            p.write_text(raw);r=lt.cursor(self.a);self.assertFalse(r['valid']);self.assertEqual(r['reason'],'invalid_checkpoint');self.assertEqual(p.read_text(),raw)
    def test_each_cursor_field_validated(self):
        value=dict(lesson=self.a.lesson,time=10,topic='t',recap='r',next_step='n',pending_question='q',misconceptions=[])
        inp=self.f.directory/'input.json';inp.write_text(json.dumps(value));lt.resume(self.args(state_file=inp,expected_revision=0))
        p=lt.learner(self.a)/'cursor.json';good=json.loads(p.read_text())
        for key,val in [('lesson',17),('time',float('nan')),('topic',[]),('packet_hashes',[None]),('source_sha256','bad'),('misconceptions','bad')]:
            bad=json.loads(json.dumps(good));bad['state'][key]=val;p.write_text(json.dumps(bad))
            self.assertEqual(lt.cursor(self.a)['reason'],'invalid_checkpoint')
        p.write_text(json.dumps(good));self.assertTrue(lt.cursor(self.a)['valid'])
    def test_corrupt_cursor_cannot_be_silently_overwritten(self):
        p=lt.learner(self.a)/'cursor.json';p.parent.mkdir(parents=True);p.write_text('{"revision":0,"state":{"lesson":17}}');before=p.read_bytes()
        inp=self.f.directory/'in.json';inp.write_text(json.dumps(dict(lesson=self.a.lesson,time=10,topic='t',recap='r',next_step='n',pending_question='q',misconceptions=[])))
        with self.assertRaises(ValueError):lt.resume(self.args(state_file=inp,expected_revision=0))
        self.assertEqual(p.read_bytes(),before)
    def attach(self,content=None):
        p=self.f.directory/'code.py';p.write_text(content or '')
        with contextlib.redirect_stdout(io.StringIO()):course.attach(self.args(kind='code',source=str(p),none=content is None,ref=None))
        return course.read(self.f.lesson/'lesson.json')['code'].get('content_sha256')
    def notes(self,checksum):
        fragment=course.read(self.f.lesson/'lesson.json')['prepared_fragments'][0]
        p=Path(fragment['folder'])/'visual_notes.json'
        p.write_text(json.dumps(dict(observations=[dict(range=[0,10],source_kind='attached_code',source_sha256=checksum,verification='confirmed'),dict(range=[0,10],source_kind='screen',verification='confirmed')],uncertainties=[])))
    def context(self):
        from lecture_context import assemble
        return assemble(self.f.lesson,course.read(self.f.lesson/'lesson.json'),0,10)
    def test_changed_code_note_is_historical_not_current(self):
        h=self.attach('x=1');self.notes(h);self.attach('x=2');c=self.context();n=c['visual_notes']['observations'][0]
        self.assertTrue(n['stale']);self.assertEqual(n['source_status'],'historical');self.assertEqual(n['verification'],'requires_review');self.assertFalse(n['usable_as_current_evidence']);self.assertEqual(n['original_verification'],'confirmed')
        self.assertIn('attached_code_notes_stale_or_unverifiable',c['warnings']);self.assertEqual(c['visual_notes']['observations'][1]['verification'],'confirmed')
    def test_identical_code_remains_current(self):
        h=self.attach('x=1');self.notes(h);self.attach('x=1');self.assertFalse(self.context()['visual_notes']['observations'][0]['stale'])
    def test_removed_code_invalidates_note(self):
        h=self.attach('x=1');self.notes(h);self.attach();self.assertTrue(self.context()['visual_notes']['observations'][0]['stale'])
    def test_missing_hash_is_unverifiable(self):
        self.attach('x=1');self.notes(None);self.assertEqual(self.context()['visual_notes']['observations'][0]['source_status'],'unverifiable')
