"""Text/video share unit semantics; evidence and prerequisite changes stay visible."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import study_units as su
import test_context


class StudyUnits(unittest.TestCase):
    def setUp(self):
        self.f=test_context.ContextRegression();self.f.setUp()
        self.text=self.f.directory/'notes.txt';self.text.write_text('Heading\nA map stores keys and values\nPractice\n')
        self.path=self.f.path/'study-units.json'
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return type(self.f.args)(**(vars(self.f.args)|kw))
    def card(self,name='maps',**kw):
        return dict(id=name,title='Maps',summary='Assistant explanation',objectives=['Explain keys'],
                    criteria=['Solve a new task independently'],prerequisites=[],uncertainties=[],
                    sources=[dict(kind='text',role='external',notes='Read selected lines',path=str(self.text),start_line=1,end_line=2)])|kw
    def save(self,value=None,revision=None):
        p=self.f.directory/'unit-input.json';p.write_text(json.dumps(value or self.card()))
        return su.unit(self.args(unit_file=p,expected_revision=su.load(self.f.path)['revision'] if revision is None else revision))
    def get(self,name='maps'):return su.unit(self.args(unit_id=name))['unit']

    def test_old_course_read_does_not_create_registry(self):
        self.assertEqual(su.unit(self.f.args)['units'],[]);self.assertFalse(self.path.exists())
    def test_document_unit_needs_no_video_or_asr(self):
        self.save();row=self.get();self.assertFalse(row['stale']);self.assertEqual(row['mastery'],'not_assessed')
        self.assertIn('keys',row['source_contexts'][0]['text']);self.assertEqual(row['sources'][0]['role'],'external')
    def test_original_change_does_not_rewrite_pinned_snapshot(self):
        self.save();self.text.write_text('Changed original');self.assertFalse(self.get()['stale'])
    def test_snapshot_corruption_marks_stale_and_hides_context(self):
        self.save();source=self.get()['sources'][0];p=self.f.path/source['snapshot'];p.chmod(0o600);p.write_text('bad')
        row=self.get();self.assertTrue(row['stale']);self.assertEqual(row['source_contexts'],[])
    def test_prerequisite_revision_and_staleness_propagate(self):
        self.save();self.save(self.card('child',prerequisites=['maps']))
        self.save(self.card(title='Changed'));self.assertTrue(self.get('child')['stale'])
        self.save(self.card('child',prerequisites=['maps']));self.assertFalse(self.get('child')['stale'])
        source=self.get()['sources'][0];(self.f.path/source['snapshot']).unlink();self.assertTrue(self.get('child')['stale'])
    def test_missing_self_and_cyclic_prerequisites_rejected(self):
        for prereqs in [['missing'],['maps']]:
            with self.assertRaises(ValueError):self.save(self.card(prerequisites=prereqs))
        self.save();self.save(self.card('child',prerequisites=['maps']));before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.save(self.card(prerequisites=['child']))
        self.assertEqual(self.path.read_bytes(),before)
    def test_video_and_text_share_context_shape(self):
        self.f.add('real',0,60,[(5,10,'Keys')],[(5,b'frame')])
        self.save(self.card(sources=[dict(kind='video',role='teacher',notes='Checked recording',lesson='lesson-01',start=0,end=20)]))
        row=self.get();self.assertFalse(row['stale']);self.assertEqual(row['source_contexts'][0]['context']['speech'][0]['text'],'Keys')
    def test_video_change_marks_stale(self):
        self.f.add('a',0,60,[(5,10,'Before')]);self.save(self.card(sources=[dict(kind='video',role='teacher',notes='Reviewed',lesson='lesson-01',start=0,end=20)]))
        self.f.add('b',0,60,[(5,10,'After')]);self.assertTrue(self.get()['stale'])
    def test_mixed_sources_and_uncertainties_preserved(self):
        self.f.add('a',0,60,[(5,10,'Keys')])
        value=self.card(uncertainties=['Examples conflict'])
        value['sources'].append(dict(kind='video',role='teacher',notes='Partial explanation',lesson='lesson-01',start=0,end=20))
        self.save(value);row=self.get();self.assertEqual(len(row['source_contexts']),2)
        self.assertEqual(row['uncertainties'],['Examples conflict']);self.assertEqual(row['verification'],'assistant_annotation_not_automatic_proof')
    def test_invalid_sources_do_not_publish_registry(self):
        for patch in [dict(start_line=0),dict(end_line=90),dict(path=1),dict(role='confirmed')]:
            value=self.card();value['sources'][0].update(patch)
            with self.subTest(patch=patch),self.assertRaises(ValueError):self.save(value)
            self.assertFalse(self.path.exists())
    def test_stale_cas_does_not_overwrite(self):
        self.save();before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'changed'):self.save(self.card(title='New'),revision=0)
        self.assertEqual(self.path.read_bytes(),before)
    def test_concurrent_writes_cas(self):
        p=self.f.directory/'common.json';p.write_text(json.dumps(self.card()))
        def write(_):
            try:su.unit(self.args(unit_file=p,expected_revision=0));return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(write,range(2)))
        self.assertEqual(sorted(results),[False,True])
    def test_revision_history_preserved(self):
        self.save();self.save(self.card(title='New'))
        data=su.load(self.f.path);self.assertEqual(len(data['history']),1)
        self.assertEqual(data['history'][0]['record']['title'],'Maps')
    def test_move_keeps_text_and_video_units(self):
        self.f.add('a',0,60,[(5,10,'Keys')]);self.save()
        self.save(self.card('video',sources=[dict(kind='video',role='teacher',notes='Reviewed',lesson='lesson-01',start=0,end=20)]))
        moved=self.f.directory/'moved';shutil.move(self.f.path,moved);self.f.path=moved;self.f.args.course=moved
        self.assertFalse(self.get()['stale']);self.assertFalse(self.get('video')['stale'])
    def test_unrelated_bad_source_does_not_block_selected_unit(self):
        self.save();self.text.write_text('Other\nText');self.save(self.card('other'))
        source=self.get('other')['sources'][0];(self.f.path/source['snapshot']).unlink()
        self.assertFalse(self.get()['stale'])
    def test_tampered_annotation_is_not_silently_trusted(self):
        self.save();data=json.loads(self.path.read_text());data['units']['maps']['summary']='Tampered'
        self.path.write_text(json.dumps(data));before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.get()
        with self.assertRaises(ValueError):self.save()
        self.assertEqual(self.path.read_bytes(),before)

    def test_invalid_history_is_preserved(self):
        self.save();self.save(self.card(title='New'));data=json.loads(self.path.read_text())
        data['history'][0]['record']['summary']='Changed history';self.path.write_text(json.dumps(data));before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.get()
        self.assertEqual(self.path.read_bytes(),before)

    def test_assistant_example_never_becomes_external_proof(self):
        value=self.card();value['sources'][0]['role']='assistant';self.save(value)
        self.assertEqual(self.get()['source_contexts'][0]['role'],'assistant')

    def test_empty_learning_criteria_rejected(self):
        with self.assertRaises(ValueError):self.save(self.card(criteria=[]))
        self.assertFalse(self.path.exists())

    def test_identical_prerequisite_resave_keeps_dependent_current(self):
        self.save();self.save(self.card('child',prerequisites=['maps']));self.save()
        self.assertFalse(self.get('child')['stale'])

    def test_snapshot_path_escape_is_rejected(self):
        self.save();data=json.loads(self.path.read_text());row=data['units']['maps']
        row['sources'][0]['snapshot']='../escape';row['content_sha256']=su.digest(su.semantic(row))
        self.path.write_text(json.dumps(data));self.assertTrue(self.get()['stale'])
