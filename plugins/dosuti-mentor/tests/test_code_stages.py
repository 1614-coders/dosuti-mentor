import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace as NS
import unittest
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
    def test_historical_code_is_preserved_after_new_attachment(self):
        self.save();old=self.data()['code']['snapshot'];self.attach('x=2')
        row=self.f.context(0,30)['code_stages'][0]
        self.assertFalse(row['stale']);self.assertTrue(row['historical']);self.assertEqual(row['snapshot'],old)
        self.assertEqual(Path(row['absolute_file']).read_text(),'x=1')
    def test_frame_tampering_invalidates_stage(self):
        self.save();(self.folder/'frames/0.jpg').write_bytes(b'tamper')
        self.assertTrue(stages.checked(self.f.lesson,self.data())[0]['stale'])
    def test_historical_code_tampering_invalidates_stage(self):
        self.save();p=Path(self.data()['code']['snapshot'])/'example.py';self.attach('x=2');p.chmod(0o600);p.write_text('bad')
        self.assertTrue(self.f.context(0,30)['code_stages'][0]['stale'])
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
