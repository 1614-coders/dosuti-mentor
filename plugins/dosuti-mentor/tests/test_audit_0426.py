"""Audit regressions: transactional sources, large documents and honest freshness."""
import copy,json,sys,shutil
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import document_store as ds,study_units as su,learning_intent as li,career_evidence as ce,career_route as cr
import test_study_units,test_learning_intent,test_adaptive_learning,test_career_route

class DocumentStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'state.json'
        self.data=dict(schema='qa',value='абв'*200000)
    def tearDown(self):self.tmp.cleanup()
    def test_legacy_read_has_no_side_effects(self):
        self.path.write_text('{"schema":"qa","value":1}');before=self.path.read_bytes()
        self.assertEqual(ds.read(self.path,None,'qa')['value'],1);self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(ds.folder(self.path).exists())
    def test_parts_roundtrip_and_relocation(self):
        ds.write(self.path,self.data,'qa');self.assertEqual(json.loads(self.path.read_text())['schema'],ds.SCHEMA)
        self.assertEqual(ds.read(self.path,None,'qa'),self.data)
        moved=self.root/'moved';moved.mkdir();shutil.move(str(self.path),moved/self.path.name);shutil.move(str(ds.folder(self.path)),moved/ds.folder(self.path).name)
        self.assertEqual(ds.read(moved/self.path.name,None,'qa'),self.data)
    def test_corrupt_or_missing_part_refused_without_rewrite(self):
        ds.write(self.path,self.data,'qa');m=self.path.read_bytes();p=next(ds.folder(self.path).glob('*.part'));p.write_bytes(b'bad')
        with self.assertRaises(ValueError):ds.read(self.path,None,'qa')
        self.assertEqual(self.path.read_bytes(),m)
        p.unlink()
        with self.assertRaises(ValueError):ds.read(self.path,None,'qa')
    def test_manifest_failure_keeps_old_and_removes_new_parts(self):
        ds.write(self.path,dict(schema='qa',value=1),'qa');old=self.path.read_bytes()
        with self.assertRaises(OSError):ds.write(self.path,self.data,'qa',writer=lambda *args:(_ for _ in ()).throw(OSError('QA full disk')))
        self.assertEqual(self.path.read_bytes(),old);self.assertEqual(list(ds.folder(self.path).glob('*.part')),[])
    def test_failed_update_keeps_shared_committed_parts(self):
        ds.write(self.path,self.data,'qa');old=self.path.read_bytes();files={p.name for p in ds.folder(self.path).glob('*')}
        with self.assertRaises(OSError):ds.write(self.path,dict(self.data,extra='new'),'qa',writer=lambda *a:(_ for _ in ()).throw(OSError('QA')))
        self.assertEqual(old,self.path.read_bytes());self.assertEqual(files,{p.name for p in ds.folder(self.path).glob('*')});self.assertEqual(ds.read(self.path,None,'qa'),self.data)
    def test_committed_writer_error_does_not_remove_parts(self):
        def writer(p,v):
            from atomic_files import atomic_json
            atomic_json(p,v);raise OSError('QA after commit')
        with self.assertRaises(OSError):ds.write(self.path,self.data,'qa',writer=writer)
        self.assertEqual(ds.read(self.path,None,'qa'),self.data)
    def test_limit_before_publication_and_wrong_kind(self):
        with self.assertRaises(ValueError):ds.write(self.path,self.data,'qa',limit=50)
        self.assertFalse(self.path.exists());self.assertFalse(ds.folder(self.path).exists())
        ds.write(self.path,self.data,'qa')
        with self.assertRaises(ValueError):ds.read(self.path,None,'other')

class Sources(unittest.TestCase):
    def setUp(self):self.f=test_study_units.StudyUnits();self.f.setUp();self.f.save()
    def tearDown(self):self.f.tearDown()
    def snapshots(self):return {p.name:p.read_bytes() for p in (self.f.f.path/'study-sources').glob('*')}
    def test_count_refusal_does_not_publish(self):
        before=self.snapshots();old=self.f.path.read_bytes();self.f.text.write_text('new\nsource')
        with patch.object(su,'MAX_UNITS',1),self.assertRaises(ValueError):self.f.save(self.f.card(name='two'))
        self.assertEqual(self.snapshots(),before);self.assertEqual(self.f.path.read_bytes(),old)
    def test_second_bad_source_does_not_publish_first(self):
        before=self.snapshots();self.f.text.write_text('new\nsource');card=self.f.card(name='two');bad=copy.deepcopy(card['sources'][0]);bad['path']=str(self.f.f.directory/'missing.txt');card['sources'].append(bad)
        with self.assertRaises(ValueError):self.f.save(card)
        self.assertEqual(self.snapshots(),before)
    def test_commit_failure_rolls_back_new_source_only(self):
        before=self.snapshots();old=self.f.path.read_bytes();self.f.text.write_text('new\nsource')
        with patch.object(su,'atomic_json',side_effect=OSError('QA disk full')),self.assertRaises(OSError):self.f.save(self.f.card(name='two'))
        self.assertEqual(self.snapshots(),before);self.assertEqual(old,self.f.path.read_bytes())
    def test_250_detailed_units_and_revision_migrate_without_hash_changes(self):
        data=su.load(self.f.f.path);row=data['units']['maps']
        for n in range(249):
            u=copy.deepcopy(row);u.update(id='u'+str(n),summary='Подробная тема. '*200);u['content_sha256']=su.digest(su.semantic(u));data['units'][u['id']]=u
        data['revision']=250
        ds.write(self.f.path,data,'study_units');self.assertEqual(su.load(self.f.f.path),data)
        sha=data['units']['u0']['content_sha256'];self.f.save(self.f.card(summary='Changed'))
        new=su.load(self.f.f.path);self.assertEqual(new['units']['u0']['content_sha256'],sha);self.assertEqual(new['history'][0]['record'],row)

class Intent(unittest.TestCase):
    def test_large_history_keeps_plan_goal_and_session_revisions(self):
        f=test_learning_intent.LearningIntent();f.setUp()
        try:
            f.goal();f.propose();f.accept();f.save({'session':'QA task'},session='a');old=f.read(session='a')
            for n in range(115):f.save({'session':str(n)+'я'*3990},session='b')
            x=f.read(session='a');self.assertEqual(x['plan'],old['plan']);self.assertEqual(x['session_task'],old['session_task']);self.assertEqual(x['goal_revision'],old['goal_revision']);self.assertEqual(x['revision'],119)
            self.assertEqual(json.loads(f.path.read_text())['schema'],ds.SCHEMA)
            before=f.path.read_bytes()
            with self.assertRaises(ValueError):f.save({'session':'stale'},revision=4,session='a')
            self.assertEqual(before,f.path.read_bytes())
        finally:f.tearDown()

class Recheck(unittest.TestCase):
    def test_punctuation_repeat_refused_new_question_allowed(self):
        f=test_adaptive_learning.Adaptive();f.setUp()
        try:
            f.save();before=f.f.path.read_bytes()
            with self.assertRaisesRegex(ValueError,'fresh'):f.save(f.later(question='What does a map store?!'),test_adaptive_learning.DAY1)
            self.assertEqual(before,f.f.path.read_bytes());self.assertTrue(f.save(f.later(),test_adaptive_learning.DAY1)['learning']['valid'])
        finally:f.tearDown()
    def test_old_cosmetic_recheck_remains_readable(self):
        f=test_adaptive_learning.Adaptive();f.setUp()
        try:
            f.save();f.save(f.later(),test_adaptive_learning.DAY1);data=json.loads(f.f.path.read_text());data['events'][1]['value']['question']='What does a map store?!';f.f.path.write_text(json.dumps(data));before=f.f.path.read_bytes()
            self.assertTrue(f.read(test_adaptive_learning.DAY2)['learning']['valid']);self.assertEqual(before,f.f.path.read_bytes())
        finally:f.tearDown()

class Freshness(unittest.TestCase):
    def test_boundary_future_and_archived(self):
        card=dict(postings=[dict(inclusion='included',kind='vacancy',status='active',observed_on='2026-01-01')])
        self.assertFalse(ce.freshness(card,'2026-01-30')['needs_review']);self.assertTrue(ce.freshness(card,'2026-01-31')['needs_review'])
        self.assertEqual(ce.freshness(card,'2025-12-31')['status'],'future_observation');card['postings'][0]['status']='archived';self.assertEqual(ce.freshness(card,'2026-10-16')['status'],'not_applicable')
    def test_old_market_proposal_blocked_but_foundation_not_blocked(self):
        f=test_career_route.Route();f.setUp()
        try:
            f.f.save(f.f.card());before=f.path.read_bytes()
            with self.assertRaisesRegex(ValueError,'freshness'):f.save()
            self.assertEqual(before,f.path.read_bytes())
            card=f.card();card['steps']=card['steps'][:1];self.assertTrue(f.save(card)['proposal']['valid'])
        finally:f.tearDown()
    def test_ageing_accepted_plan_keeps_history_and_marks_task(self):
        f=test_career_route.Route();f.setUp()
        try:
            f.save();f.change(dict(accept_proposal=True));li.commit(f.args(session='a',expected_revision=3,reason='QA task'),dict(session='QA task'));before=f.path.read_bytes()
            with patch('course.now',return_value='2027-12-31T00:00:00Z'):
                x=cr.route(f.args(session='a'));self.assertTrue(x['intent']['session_task']['needs_review']);self.assertTrue(x['plan']['needs_review']);self.assertEqual(x['plan']['freshness']['status'],'recheck_due')
                with self.assertRaises(ValueError):f.save()
            self.assertEqual(before,f.path.read_bytes())
        finally:f.tearDown()
