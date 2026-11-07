import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import code_stages as stages
import test_context


class CodeStages(unittest.TestCase):
    def setUp(self):
        self.f = test_context.ContextRegression(); self.f.setUp()
        self.folder = self.f.add('stage', 0, 60, [(1, 5, 'test')], [(10, b'frame one'), (40, b'frame two')])
        self.attach('x=1')
    def tearDown(self): self.f.tearDown()
    def args(self, **kw): return NS(**(vars(self.f.args) | kw))
    def data(self): return course.read(self.f.lesson/'lesson.json')
    def attach(self, text):
        p=self.f.directory/'example.py';p.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(self.args(kind='code',source=str(p),none=False,ref=None))
    def value(self, **kw):
        return dict(id='first',title='Initial example',start=0,end=30,
                    code_sha256=self.data()['code']['content_sha256'],file='example.py',screen_file='main.py',
                    match='partial',notes='Only x assignment visible',
                    frames=[dict(time=10,sha256=course.file_hash(self.folder/'frames/0.jpg'))]) | kw
    def save(self, value=None):
        p=self.f.directory/'stage.json';p.write_text(json.dumps(value or self.value()))
        return stages.code_stage(self.args(stage_file=p))['stages']
    def test_context_selects_intersecting_stage_only(self):
        self.save();self.assertEqual(len(self.f.context(20,5)['code_stages']),1)
        self.assertEqual(self.f.context(30,5)['code_stages'],[])

    def test_quality_separates_partial_difference_and_outdated_annotation(self):
        self.save(self.value(match='partial'))
        summary=self.f.context(0,30)['quality_summary']['code']
        self.assertEqual(summary['difference_ids'],[])
        self.assertEqual(summary['partial_ids'],['first'])
        stages.code_stage(self.args(remove='first'))
        self.save(self.value(match='differs'))
        self.assertEqual(self.f.context(0,30)['quality_summary']['code']['difference_ids'],['first'])
        data=self.data();data['code_stages']['first']['source_sha256']='b'*64
        course.save(self.f.lesson/'lesson.json',data)
        summary=self.f.context(0,30)['quality_summary']['code']
        self.assertEqual(summary['difference_ids'],[])
        self.assertEqual(summary['stale_ids'],['first'])
    def test_historical_code_is_preserved_after_new_attachment(self):
        self.save();old=self.data()['code']['snapshot'];self.attach('x=2')
        row=self.f.context(0,30)['code_stages'][0]
        self.assertFalse(row['stale']);self.assertTrue(row['historical']);self.assertEqual(row['snapshot'],old)
        self.assertEqual(Path(row['absolute_file']).read_text(),'x=1')
    def test_frame_tampering_invalidates_stage(self):
        self.save();(self.folder/'frames/0.jpg').write_bytes(b'tamper')
        self.assertTrue(stages.checked(self.f.lesson,self.data())[0]['stale'])
    def test_historical_code_tampering_invalidates_stage(self):
        self.save();p=Path(self.data()['code']['snapshot'])/'example.py';self.attach('x=2')
        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(self.args(topic='Independent practice',stage='practice_checked',details='Preserve progress'))
        progress=self.f.path/'learners/default/progress.sqlite3';progress_before=progress.read_bytes()
        registry=self.f.lesson/'lesson.json';registered=registry.read_bytes()
        foreign=self.f.directory/'foreign.txt';foreign.write_bytes(b'FOREIGN DATA')
        if os.name == 'nt':
            # Windows proves prevention, not detection of a successful tamper.
            before=p.read_bytes();checksum=course.file_hash(p)
            with self.assertRaises(PermissionError) as blocked:p.write_text('bad')
            self.assertEqual(blocked.exception.winerror,5)
            self.assertEqual(p.read_bytes(),before);self.assertEqual(course.file_hash(p),checksum)
            self.assertEqual(registry.read_bytes(),registered)
            historical=self.data()['code']['history'][-1]
            self.assertEqual(historical['inventory']['example.py']['sha256'],checksum)
            self.assertEqual(course.verify_snapshot(historical),'snapshot_hashes_match')
            row=self.f.context(0,30)['code_stages'][0]
            self.assertFalse(row['stale']);self.assertTrue(row['historical'])
        else:
            # POSIX retains the original successful mutation/detection scenario.
            p.chmod(0o600);p.write_text('bad')
            self.assertTrue(self.f.context(0,30)['code_stages'][0]['stale'])
        self.assertEqual((Path(self.data()['code']['snapshot'])/'example.py').read_text(),'x=2')
        self.assertEqual(registry.read_bytes(),registered)
        self.assertEqual(progress.read_bytes(),progress_before)
        self.assertEqual(foreign.read_bytes(),b'FOREIGN DATA')
    def test_invalid_inputs_do_not_publish(self):
        for kw in [dict(start=-1),dict(end=90),dict(frames=[]),dict(file='../example.py'),dict(code_sha256='a'*64),
                   dict(frames=[dict(time=11,sha256='a'*64)]),dict(match='confirmed'),dict(start=True),dict(end=float('nan'))]:
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):self.save(self.value(**kw))
                self.assertFalse(self.data().get('code_stages'))
    def test_overlap_is_ambiguous_not_automatically_selected(self):
        self.save(self.value(match='matches'));self.save(self.value(id='second',match='matches'))
        result=self.f.context(0,30)
        self.assertIn('ambiguous_code_stages',result['warnings'])
        self.assertTrue(all(not row['usable_as_screen_match'] for row in result['code_stages']))
    def test_unknown_partial_and_differs_are_not_matches(self):
        for status in ('unknown','partial','differs'):
            row=self.save(self.value(id=status,match=status))[-1]
            self.assertFalse(row['usable_as_screen_match'])
    def test_duplicate_id_rejected_and_explicit_remove(self):
        self.save()
        with self.assertRaises(ValueError):self.save()
        self.assertEqual(stages.code_stage(self.args(remove='first'))['stages'],[])
    def test_new_unrelated_packet_does_not_invalidate_stage(self):
        self.save();self.f.add('later',60,90,[(70,75,'later')])
        self.assertFalse(stages.checked(self.f.lesson,self.data())[0]['stale'])
    def test_relocation_resolves_stage_file(self):
        self.save();new=self.f.directory/'moved';shutil.move(self.f.path,new)
        directory=new/'lessons'/self.f.args.lesson;data=course.read(directory/'lesson.json')
        row=stages.checked(directory,data)[0]
        self.assertFalse(row['stale']);self.assertTrue(Path(row['absolute_file']).is_relative_to(new))
    def test_video_identity_mismatch_marks_stale(self):
        self.save();data=self.data();data['video']['sha256']='b'*64
        self.assertTrue(stages.checked(self.f.lesson,data)[0]['stale'])

    def pair(self, **kw):
        self.save(self.value(match='matches'))
        self.save(self.value(id='second',start=20,end=50,match='matches',
            frames=[dict(time=40,sha256=course.file_hash(self.folder/'frames/1.jpg'))],**kw))

    def test_narrow_window_preserves_conflict_with_hidden_neighbor(self):
        self.pair()
        result=self.f.context(0,15)
        self.assertEqual([row['id'] for row in result['code_stages']],['first'])
        row=result['code_stages'][0]
        self.assertFalse(row['usable_as_screen_match'])
        self.assertEqual(row['overlapping_stage_ids'],['second'])
        self.assertIn('ambiguous_code_stages',result['warnings'])

    def test_half_open_boundaries_and_adjacent_frames_do_not_conflict(self):
        self.save(self.value(match='matches'))
        self.save(self.value(id='second',start=30,end=60,match='matches',
            frames=[dict(time=40,sha256=course.file_hash(self.folder/'frames/1.jpg'))]))
        for start,stop,identity in [(0,10,'first'),(29.9,30,'first'),(30,40,'second'),(40,50,'second')]:
            with self.subTest(start=start,stop=stop):
                rows=stages.checked(self.f.lesson,self.data(),start,stop)
                self.assertEqual([row['id'] for row in rows],[identity])
                self.assertTrue(rows[0]['usable_as_screen_match'])
                self.assertEqual(rows[0]['overlapping_stage_ids'],[])

    def test_small_overlap_and_repeated_code_remain_ambiguous(self):
        self.save(self.value(end=40.001,match='matches'))
        self.save(self.value(id='second',start=40,end=60,match='matches',
            frames=[dict(time=40,sha256=course.file_hash(self.folder/'frames/1.jpg'))]))
        row=stages.checked(self.f.lesson,self.data(),0,15)[0]
        self.assertFalse(row['usable_as_screen_match'])
        self.assertEqual(row['overlapping_stage_ids'],['second'])

    def test_different_screen_files_do_not_conflict(self):
        self.pair(screen_file='other.py')
        self.assertTrue(stages.checked(self.f.lesson,self.data(),0,15)[0]['usable_as_screen_match'])

    def test_hidden_stale_neighbor_does_not_override_verified_stage(self):
        self.pair();data=self.data();data['code_stages']['second']['source_sha256']='b'*64
        before=json.dumps(data,sort_keys=True)
        row=stages.checked(self.f.lesson,data,0,15)[0]
        self.assertFalse(row['stale']);self.assertTrue(row['usable_as_screen_match'])
        self.assertEqual(row['overlapping_stage_ids'],[])
        self.assertEqual(json.dumps(data,sort_keys=True),before)

    def test_different_code_snapshots_still_conflict_and_unrelated_stage_is_not_verified(self):
        self.save(self.value(match='matches'));self.attach('x=2')
        self.save(self.value(id='second',start=20,end=50,match='matches',
            frames=[dict(time=40,sha256=course.file_hash(self.folder/'frames/1.jpg'))]))
        later=self.f.add('later',60,90,[(70,75,'later')],[(70,b'later frame')])
        self.save(self.value(id='unrelated',start=60,end=90,match='matches',
            frames=[dict(time=70,sha256=course.file_hash(later/'frames/0.jpg'))]))
        with patch.object(stages,'evidence',wraps=stages.evidence) as checked:
            rows=stages.checked(self.f.lesson,self.data(),0,15)
        self.assertEqual({call.args[2]['id'] for call in checked.call_args_list},{'first','second'})
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['historical'])
        self.assertFalse(rows[0]['usable_as_screen_match'])
