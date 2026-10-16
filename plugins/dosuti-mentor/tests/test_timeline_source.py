"""Bounded transition evidence, explicit endpoint screen bindings and preserved attempts."""
import contextlib
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import code_stages
import project_timeline as pt
import unit_learning as ul
import test_project_timeline

class TimelineSource(unittest.TestCase):
    def setUp(self):
        self.t=test_project_timeline.Timeline();self.t.setUp();self.t.ready();self.t.save();self.f=self.t.f;self.root=self.t.root
        self.source=dict(kind='timeline',role='teacher',notes='Synthetic fixture, not actual teacher history',timeline_id='app',track_id='backend',before='a',after='b',stages=dict(before=None,after=None))
    def tearDown(self):self.t.tearDown()
    def save(self,source=None):return self.f.save(self.f.card(sources=[source or self.source]))
    def get(self):return self.f.get()
    def stage(self,identity='screen',match='partial'):
        directory=self.root/'lessons/lesson-01';sha=course.read(directory/'lesson.json')['code']['content_sha256']
        packet=self.f.f.lesson/'prepared/screen'
        if not packet.exists():packet=self.f.f.add('screen',0,60,[(5,10,'Synthetic speech')],[(5,b'frame')])
        value=dict(id=identity,title='Synthetic code screen',start=0,end=20,code_sha256=sha,file='A.java',screen_file='A.java',match=match,notes='Synthetic frame fixture only',frames=[dict(time=5,sha256=course.file_hash(packet/'frames/0.jpg'))])
        p=self.f.f.directory/'stage.json';p.write_text(json.dumps(value));code_stages.code_stage(self.f.args(stage_file=p));return packet
    def test_context_does_not_promote_snapshot_diff_to_video_story(self):
        self.save();x=self.get()['source_contexts'][0];self.assertEqual(x['comparison']['status'],'observed');self.assertFalse(x['continuous_video_evolution_verified'])
        self.assertEqual(x['endpoints']['before']['lecture_match'],'unverified')
    def test_gap_remains_gap_in_learning_unit(self):
        card=self.t.card();card['tracks'][0]['steps'][1]['gap_before']=True;self.t.save(card);self.save()
        x=self.get()['source_contexts'][0];self.assertEqual(x['comparison']['status'],'gap');self.assertNotIn('changes',x['comparison'])
    def test_changed_transition_invalidates_unit_and_child(self):
        self.save();self.f.save(self.f.card('child',prerequisites=['maps']))
        card=self.t.card();card['tracks'][0]['steps'][1]['notes']='Revised interpretation';self.t.save(card)
        self.assertTrue(self.get()['stale']);self.assertEqual(self.get()['source_contexts'],[]);self.assertTrue(self.f.get('child')['stale'])
    def test_identical_save_and_unrelated_series_do_not_invalidate(self):
        self.save();self.t.save();self.t.save(self.t.card(id='other'));self.assertFalse(self.get()['stale'])
    def test_unrelated_track_changes_do_not_invalidate(self):
        self.save();card=self.t.card();card['tracks'].append(dict(id='other',title='Other explicit line',steps=[self.t.step('x','lesson-01'),self.t.step('y','lesson-02')]))
        self.t.save(card);self.assertFalse(self.get()['stale'])
    def test_appended_step_does_not_change_selected_adjacent_pair(self):
        self.save();card=self.t.card();card['tracks'][0]['steps'].append(self.t.step('c','lesson-01'));self.t.save(card);self.assertFalse(self.get()['stale'])
    def test_inserted_intermediate_step_invalidates_old_adjacency(self):
        self.save();card=self.t.card();card['tracks'][0]['steps'].insert(1,self.t.step('between','lesson-01'));self.t.save(card);self.assertTrue(self.get()['stale'])
    def test_current_snapshot_change_invalidates(self):
        self.save();self.t.attach('lesson-02',{'Other':'new'});self.assertTrue(self.get()['stale'])
    def test_pinned_survives_new_current_snapshot(self):
        card=self.t.card()
        for step in card['tracks'][0]['steps']:step['version']=course.read(self.root/'lessons'/step['lesson']/'lesson.json')['code']['content_sha256']
        self.t.save(card);self.save();self.t.attach('lesson-02',{'Other':'new'});self.assertFalse(self.get()['stale']);self.assertTrue(self.get()['source_contexts'][0]['endpoints']['after']['historical'])
    def test_stage_partial_not_full_match(self):
        self.stage();source=self.source|dict(stages=dict(before='screen',after=None));self.save(source)
        stage=self.get()['source_contexts'][0]['endpoints']['before']['stage'];self.assertEqual(stage['match'],'partial');self.assertFalse(stage['usable_as_screen_match'])
    def test_stage_cannot_bind_other_endpoint(self):
        self.stage()
        with self.assertRaises(ValueError):self.save(self.source|dict(stages=dict(before=None,after='screen')))
    def test_stage_matches_still_not_continuous_video_proof(self):
        self.stage(match='matches');self.save(self.source|dict(stages=dict(before='screen',after=None)))
        x=self.get()['source_contexts'][0];self.assertTrue(x['endpoints']['before']['stage']['usable_as_screen_match']);self.assertFalse(x['continuous_video_evolution_verified'])
    def test_stage_annotation_and_removal_invalidate(self):
        self.stage();self.save(self.source|dict(stages=dict(before='screen',after=None)))
        course.update_lesson(self.root/'lessons/lesson-01',lambda d:d['code_stages']['screen'].update(notes='Changed annotation'))
        self.assertTrue(self.get()['stale'])
        course.update_lesson(self.root/'lessons/lesson-01',lambda d:d['code_stages'].clear());self.assertTrue(self.get()['stale'])
    def test_stage_frame_damage_invalidates(self):
        packet=self.stage();self.save(self.source|dict(stages=dict(before='screen',after=None)));(packet/'frames/0.jpg').write_bytes(b'bad');self.assertTrue(self.get()['stale'])
    def test_overlap_changes_stage_evidence_without_promotion(self):
        self.stage(match='matches');self.save(self.source|dict(stages=dict(before='screen',after=None)));self.stage(identity='overlap',match='matches')
        self.assertTrue(self.get()['stale'])
    def test_history_retained_but_stale_write_rejected_then_restored(self):
        self.save();args=self.f.args(unit_id='maps',history_limit=100);x=ul.learn_unit(args)
        p=self.f.f.directory/'answer.json';p.write_text(json.dumps(dict(stage='practice_with_help',details='Synthetic QA',question='QA task',answer='QA answer',hints=1,criteria=[1])))
        ul.learn_unit(self.f.args(unit_id='maps',event_file=p,expected_revision=0,expected_unit_sha256=x['unit']['content_sha256']))
        journal=self.root/'learners/default/unit-learning.json';before=journal.read_bytes()
        card=self.t.card();card['tracks'][0]['steps'][1]['gap_before']=True;self.t.save(card)
        x=ul.learn_unit(args);self.assertEqual(x['learning']['outdated_attempt_count'],1)
        with self.assertRaises(ValueError):ul.learn_unit(self.f.args(unit_id='maps',event_file=p,expected_revision=1,expected_unit_sha256=x['unit']['content_sha256']))
        self.assertEqual(journal.read_bytes(),before);self.t.save();self.assertEqual(ul.learn_unit(args)['learning']['current_attempt_count'],1)
    def test_tampered_registry_invalidates_without_rewriting(self):
        self.save();p=self.t.path;d=json.loads(p.read_text());d['timelines']['app']['title']='tampered';p.write_text(json.dumps(d));before=p.read_bytes()
        self.assertTrue(self.get()['stale']);self.assertEqual(p.read_bytes(),before)
    def test_invalid_reference_and_extra_fields_rejected(self):
        for change in [dict(track_id='missing'),dict(before='b',after='a'),dict(before='a',after='a'),dict(stages={}),dict(timeline_id='../app'),dict(start=0)]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.save(self.source|change)
    def test_move_preserves_unit_and_transition(self):
        self.save();moved=self.root.with_name('moved');shutil.move(self.root,moved);self.f.f.path=moved;self.f.f.args.course=moved
        self.assertFalse(self.get()['stale'])
    def test_timeline_write_serializes_with_unit_learning(self):
        names=[];real=pt.file_lock
        @contextlib.contextmanager
        def observed(path):
            names.append(Path(path).name)
            with real(path):yield
        with patch.object(pt,'file_lock',observed):self.t.save()
        self.assertEqual(names,['.study-units.lock','.project-timelines.lock'])
