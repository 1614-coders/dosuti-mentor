"""Source identity and screen correspondence must stay distinct across revisions."""
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
import code_stages
import material_sources as ms
import unit_learning
import test_study_units


class UnitSources(unittest.TestCase):
    def setUp(self):self.f=test_study_units.StudyUnits();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return self.f.args(**kw)
    def data(self):return course.read(self.f.f.lesson/'lesson.json')
    def attach(self,kind,text,repository=None):
        p=self.f.f.directory/('A.java' if kind=='code' else kind+'.txt');p.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):course.attach(self.args(kind=kind,source=str(p),none=False,ref=None,repository=repository,resource_type='article'))
        return self.data()
    def code(self,**kw):
        return dict(kind='code',role='teacher',notes='Manual source review; screen correspondence unknown',lesson='lesson-01',repository='streams',version='current',file='A.java',start_line=1,end_line=2)|kw
    def save(self,source):return self.f.save(self.f.card(sources=[source]))
    def get(self):return self.f.get()
    def resource(self,review=True):
        item=self.attach('resources','Article\nExplain keys\n')['resources']['items'][-1]
        rows=ms.resource(self.args(action='extract',resource_id=item['id']))['items'];item=rows[-1]
        if review:ms.resource(self.args(action='review',resource_id=item['id'],expected_revision=item['revision'],notes='Read article'))
        return dict(kind='resource',role='external',notes='Compared article',lesson='lesson-01',resource_id=item['id'],start_line=1,end_line=2)
    def stage(self,checksum,**kw):
        self.packet=self.f.f.add('stage',0,60,[(5,10,'Example')],[(5,b'frame')])
        value=dict(id='stage',title='Example',start=0,end=20,code_sha256=checksum,file='A.java',screen_file='A.java',match='partial',notes='Partial code on screen',frames=[dict(time=5,sha256=course.file_hash(self.packet/'frames/0.jpg'))])|kw
        p=self.f.f.directory/'stage.json';p.write_text(json.dumps(value));code_stages.code_stage(self.args(stage_file=p))

    def test_named_repository_and_no_inferred_screen_match(self):
        self.attach('code','class Main {\n}\n');self.attach('code','class Stream {\n}\n','streams')
        self.save(self.code());row=self.get();ctx=row['source_contexts'][0]
        self.assertIn('Stream',ctx['text']);self.assertEqual(ctx['lecture_match'],'unverified')
        self.assertFalse(ctx['usable_as_screen_match'])
        self.attach('code','class Other {\n}\n');self.assertFalse(self.get()['stale'])
    def test_current_code_change_invalidates(self):
        self.attach('code','class Stream {\n}\n','streams');self.save(self.code())
        self.attach('code','class Changed {\n}\n','streams');self.assertTrue(self.get()['stale'])
    def test_pinned_code_stays_historical_in_named_repository(self):
        sha=self.attach('code','class Old {\n}\n','streams')['code_repositories']['streams']['content_sha256']
        self.save(self.code(version=sha));self.attach('code','class New {\n}\n','streams')
        row=self.get();self.assertFalse(row['stale']);self.assertTrue(row['source_contexts'][0]['historical'])
        self.assertIn('Old',row['source_contexts'][0]['text'])
    def test_hash_in_other_repository_cannot_substitute(self):
        sha=self.attach('code','class A {\n}\n')['code']['content_sha256']
        self.attach('code','class B {\n}\n','streams')
        with self.assertRaises(ValueError):self.save(self.code(version=sha))
    def test_pinned_snapshot_tampering_invalidates(self):
        code=self.attach('code','class Old {\n}\n','streams')['code_repositories']['streams']
        self.save(self.code(version=code['content_sha256']));p=Path(code['snapshot'])/'A.java';p.chmod(0o600);p.write_text('Bad')
        self.assertTrue(self.get()['stale'])
    def test_partial_stage_not_promoted_and_changes_invalidate(self):
        sha=self.attach('code','class A {\n}\n','streams')['code_repositories']['streams']['content_sha256']
        self.stage(sha);self.save(self.code(stage='stage'));ctx=self.get()['source_contexts'][0]
        self.assertEqual(ctx['lecture_match'],'partial');self.assertFalse(ctx['usable_as_screen_match'])
        course.update_lesson(self.f.f.lesson,lambda d:d['code_stages']['stage'].update(match='differs'))
        self.assertTrue(self.get()['stale'])
    def test_stage_must_match_selected_code(self):
        sha=self.attach('code','class A {\n}\n')['code']['content_sha256'];self.stage(sha)
        self.attach('code','class B {\n}\n','streams')
        with self.assertRaises(ValueError):self.save(self.code(stage='stage'))
    def test_pinned_stage_survives_new_current_but_frame_damage_invalidates(self):
        sha=self.attach('code','class A {\n}\n','streams')['code_repositories']['streams']['content_sha256']
        self.stage(sha);self.save(self.code(version=sha,stage='stage'));self.attach('code','class B {\n}\n','streams')
        self.assertFalse(self.get()['stale']);(self.packet/'frames/0.jpg').write_bytes(b'bad');self.assertTrue(self.get()['stale'])
    def test_resource_requires_review(self):
        source=self.resource(False)
        with self.assertRaisesRegex(ValueError,'review'):self.save(source)
    def test_resource_review_changed_not_timestamp_invalidates(self):
        source=self.resource();self.save(source)
        course.update_lesson(self.f.f.lesson,lambda d:d['resources']['items'][0]['review'].update(updated='later'))
        self.assertFalse(self.get()['stale'])
        course.update_lesson(self.f.f.lesson,lambda d:d['resources']['items'][0]['review'].update(notes='Different interpretation'))
        self.assertTrue(self.get()['stale'])
    def test_resource_cache_damage_invalidates(self):
        self.save(self.resource());item=self.data()['resources']['items'][0]
        p=Path(item['extracted_file']);p.chmod(0o600);p.write_text('Bad');self.assertTrue(self.get()['stale'])
    def test_homework_change_and_restore(self):
        self.attach('homework','Task A\nExplain\n')
        self.save(dict(kind='homework',role='teacher',notes='Read assignment',lesson='lesson-01',start_line=1,end_line=2))
        self.attach('homework','Task B\nImplement\n');self.assertTrue(self.get()['stale'])
        self.attach('homework','Task A\nExplain\n');self.assertFalse(self.get()['stale'])
    def test_source_change_keeps_attempt_history(self):
        self.attach('code','class A {\n}\n','streams');self.save(self.code())
        x=unit_learning.learn_unit(self.args(unit_id='maps'));sha=x['unit']['content_sha256']
        p=self.f.f.directory/'answer.json';p.write_text(json.dumps(dict(stage='solved_independently',details='QA',question='QA?',answer='QA answer',hints=0,criteria=[1])))
        unit_learning.learn_unit(self.args(unit_id='maps',event_file=p,expected_revision=0,expected_unit_sha256=sha))
        self.attach('code','class B {\n}\n','streams')
        x=unit_learning.learn_unit(self.args(unit_id='maps',history_limit=100))['learning']
        self.assertEqual(x['outdated_attempt_count'],1);self.assertEqual(len(x['history']),1)
        self.assertFalse(x['all_criteria_demonstrated'])
    def test_move_keeps_all_structured_sources(self):
        self.attach('code','class A {\n}\n','streams');source=self.resource();self.attach('homework','Task\nAnswer\n')
        homework=dict(kind='homework',role='teacher',notes='Reviewed',lesson='lesson-01',start_line=1,end_line=2)
        self.f.save(self.f.card(sources=[self.code(),source,homework]))
        moved=self.f.f.directory/'moved';shutil.move(self.f.f.path,moved);self.f.f.path=moved;self.f.f.args.course=moved
        row=self.get();self.assertFalse(row['stale']);self.assertEqual(len(row['source_contexts']),3)
    def test_unsafe_file_and_invalid_lines_do_not_publish(self):
        self.attach('code','class A {\n}\n','streams')
        for kw in [dict(file='../A.java'),dict(start_line=0),dict(version='unknown'),dict(repository='missing'),dict(end_line=500)]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.save(self.code(**kw))
            self.assertFalse(self.f.path.exists())
