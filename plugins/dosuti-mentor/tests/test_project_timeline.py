"""Project identity/order are explicit; snapshots never imply a missing video narrative."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import project_timeline as pt
import test_study_units

class Timeline(unittest.TestCase):
    def setUp(self):
        self.f=test_study_units.StudyUnits();self.f.setUp();self.root=self.f.f.path;self.counter=0
        with contextlib.redirect_stdout(io.StringIO()):course.add_lesson(self.f.args(lesson='lesson-02',title='Second',order=2))
        self.path=self.root/'project-timelines.json'
    def tearDown(self):self.f.tearDown()
    def attach(self,lesson,files,repo=None):
        self.counter+=1;folder=self.f.f.directory/('src'+str(self.counter));folder.mkdir()
        for name,text in files.items():
            p=folder/name;p.parent.mkdir(exist_ok=True,parents=True);p.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):course.attach(self.f.args(lesson=lesson,kind='code',source=str(folder),none=False,ref=None,repository=repo))
        data=course.read(self.root/'lessons'/lesson/'lesson.json');return data['code'] if repo is None else data['code_repositories'][repo]
    def step(self,name,lesson,**kw):return dict(id=name,lesson=lesson,repository='default',version='current',gap_before=False,notes='Synthetic test code; screen state unknown')|kw
    def card(self,**kw):return dict(id='app',title='Explicit series',tracks=[dict(id='backend',title='Backend',steps=[self.step('a','lesson-01'),self.step('b','lesson-02')])],uncertainties=['Why code changed is not established'])|kw
    def ready(self):
        self.attach('lesson-01',{'A.java':'class A {}','Old.java':'same','Gone.java':'gone'})
        self.attach('lesson-02',{'A.java':'class A { int x; }','New.java':'same','Added.java':'new'})
    def save(self,card=None,revision=None):
        p=self.f.f.directory/'timeline-input.json';p.write_text(json.dumps(card or self.card()))
        return pt.timeline(self.f.args(timeline_file=p,expected_revision=pt.load(self.root)['revision'] if revision is None else revision))
    def get(self):return pt.timeline(self.f.args(timeline_id='app'))['timeline']
    def test_old_course_read_does_not_create_registry(self):
        self.assertEqual(pt.timeline(self.f.args())['timelines'],[]);self.assertFalse(self.path.exists())
    def test_observed_changes_not_inferred_cause_or_screen_match(self):
        self.ready();self.save();x=self.get();edge=x['tracks'][0]['steps'][1]['comparison'];self.assertFalse(x['stale'])
        self.assertEqual(edge['change_count'],4);self.assertEqual({r['status'] for r in edge['changes']},{'modified','removed','added','same_content_move_candidate'})
        self.assertEqual(edge['changes'][-1]['from_file'],'Old.java');self.assertEqual(x['tracks'][0]['steps'][1]['lecture_match'],'unverified')
    def test_ambiguous_move_never_pairs_duplicate_content(self):
        self.attach('lesson-01',{'A':'same','B':'same'});self.attach('lesson-02',{'C':'same','D':'same'});self.save()
        edge=self.get()['tracks'][0]['steps'][1]['comparison'];self.assertEqual(edge['ambiguous_move_count'],1)
        self.assertEqual(len(edge['changes']),4);self.assertFalse(any(c['status']=='same_content_move_candidate' for c in edge['changes']))
    def test_empty_file_addition_and_removal_visible(self):
        self.attach('lesson-01',{'A':'','Keep':'x'});self.attach('lesson-02',{'B':'','Keep':'x'});self.save()
        edge=self.get()['tracks'][0]['steps'][1]['comparison'];self.assertEqual(edge['change_count'],1);self.assertEqual(edge['changes'][0]['status'],'same_content_move_candidate')
    def test_gap_does_not_invent_intermediate_changes(self):
        self.ready();card=self.card();card['tracks'][0]['steps'][1]['gap_before']=True;self.save(card)
        edge=self.get()['tracks'][0]['steps'][1]['comparison'];self.assertEqual(edge['status'],'gap');self.assertNotIn('changes',edge)
    def test_current_update_marks_endpoint_stale(self):
        self.ready();self.save();self.attach('lesson-02',{'A.java':'changed'})
        x=self.get();self.assertTrue(x['stale']);self.assertEqual(x['tracks'][0]['steps'][1]['comparison']['status'],'unavailable')
    def test_historical_pin_survives_new_current(self):
        self.ready();data=course.read(self.root/'lessons/lesson-02/lesson.json');card=self.card();card['tracks'][0]['steps'][1]['version']=data['code']['content_sha256'];self.save(card)
        self.attach('lesson-02',{'Other':'new'});x=self.get();self.assertFalse(x['stale']);self.assertTrue(x['tracks'][0]['steps'][1]['historical'])
    def test_multiple_named_repositories_and_explicit_order(self):
        self.ready();self.attach('lesson-01',{'X':'one'},'examples');self.attach('lesson-02',{'X':'two'},'examples')
        card=self.card();card['tracks'].append(dict(id='examples',title='Examples',steps=[self.step('b','lesson-02',repository='examples'),self.step('a','lesson-01',repository='examples')]))
        self.save(card);x=self.get();self.assertEqual(x['tracks'][1]['steps'][0]['lesson'],'lesson-02');self.assertEqual(x['tracks'][1]['steps'][1]['comparison']['before_step'],'b')
    def test_tampered_snapshot_blocks_comparison(self):
        self.ready();self.save();data=course.read(self.root/'lessons/lesson-01/lesson.json');p=Path(data['code']['snapshot'])/'A.java';p.chmod(0o600);p.write_text('Bad')
        x=self.get();self.assertTrue(x['stale']);self.assertEqual(x['tracks'][0]['steps'][1]['comparison']['status'],'unavailable')
    def test_declared_inventory_hash_cannot_lie(self):
        self.ready();course.update_lesson(self.root/'lessons/lesson-01',lambda d:d['code'].update(content_sha256='0'*64))
        with self.assertRaises(ValueError):self.save()
        self.assertFalse(self.path.exists())
    def test_different_repository_cannot_substitute_pinned_snapshot(self):
        self.ready();sha=course.read(self.root/'lessons/lesson-01/lesson.json')['code']['content_sha256'];self.attach('lesson-01',{'Unrelated':'x'},'other')
        card=self.card();card['tracks'][0]['steps'][0].update(repository='other',version=sha)
        with self.assertRaises(ValueError):self.save(card)
    def test_same_lesson_two_explicit_historical_snapshots(self):
        sha=self.attach('lesson-01',{'A':'old'})['content_sha256'];self.attach('lesson-01',{'A':'new'})
        card=self.card();card['tracks'][0]['steps']=[self.step('old','lesson-01',version=sha),self.step('new','lesson-01')];self.save(card)
        self.assertEqual(self.get()['tracks'][0]['steps'][1]['comparison']['changes'][0]['status'],'modified')
    def test_listing_not_source_integrity_claim(self):
        self.ready();self.save();self.attach('lesson-02',{'Other':'changed'})
        listing=pt.timeline(self.f.args());self.assertNotIn('stale',listing['timelines'][0]);self.assertIn('listing_only',listing['verification'])
    def test_registry_revision_and_tampering_preserve_history(self):
        self.ready();self.save();self.save(self.card(title='Revised'));data=pt.load(self.root);self.assertEqual(len(data['history']),1)
        data['timelines']['app']['title']='tamper';self.path.write_text(json.dumps(data));before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.save(revision=2)
        self.assertEqual(self.path.read_bytes(),before)
    def test_concurrent_writers_only_one_revision(self):
        self.ready();p=self.f.f.directory/'common.json';p.write_text(json.dumps(self.card()))
        def write(_):
            try:pt.timeline(self.f.args(timeline_file=p,expected_revision=0));return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:result=list(pool.map(write,range(2)))
        self.assertEqual(sorted(result),[False,True]);self.assertEqual(pt.load(self.root)['revision'],1)
    def test_move_preserves_references(self):
        self.ready();self.save();moved=self.root.with_name('moved');shutil.move(self.root,moved);self.f.f.path=moved;self.f.f.args.course=moved;self.root=moved
        self.assertFalse(self.get()['stale'])
    def test_invalid_steps_do_not_publish(self):
        self.ready()
        for change in [dict(version='HEAD'),dict(repository='missing'),dict(lesson='../outside'),dict(gap_before=1),dict(notes='')]:
            card=self.card();card['tracks'][0]['steps'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.save(card)
            self.assertFalse(self.path.exists())
    def test_large_diff_bounded_and_counted(self):
        self.attach('lesson-01',{'keep':'same'});self.attach('lesson-02',{'keep':'same'}|{str(i):str(i) for i in range(220)});self.save()
        edge=self.get()['tracks'][0]['steps'][1]['comparison'];self.assertEqual(edge['change_count'],220);self.assertEqual(len(edge['changes']),200);self.assertTrue(edge['truncated'])
