import contextlib,io,json,sys,shutil
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,term_notes,test_context

class TermNotes(unittest.TestCase):
    def setUp(self):
        self.f=test_context.ContextRegression();self.f.setUp()
        self.folder=self.f.add('a',0,60,[(10,20,'Return limony or flaks')],[(15,b'Flux visible')])
        self.input=self.f.directory/'note.json'
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return NS(**(vars(self.f.args)|kw))
    def value(self,**kw):return dict(id='flux',start=10,end=20,asr_text='flaks',term='Flux',basis='screen',notes='Visible type; speech not independently heard',frames=[dict(time=15,sha256=course.file_hash(self.folder/'frames/0.jpg'))],code=None)|kw
    def save(self,**kw):
        self.input.write_text(json.dumps(self.value(**kw)));return term_notes.term_note(self.args(note_file=self.input))['term_notes'][0]
    def data(self):return course.read(self.f.lesson/'lesson.json')
    def test_screen_interpretation_keeps_speech_unchanged(self):
        before=(self.folder/'transcript.json').read_bytes();row=self.save()
        r=self.f.context(10,10);self.assertEqual(r['speech'][0]['text'],'Return limony or flaks')
        self.assertEqual((self.folder/'transcript.json').read_bytes(),before);self.assertEqual(row['interpretation_status'],'supported_term_interpretation');self.assertFalse(row['verbatim_speech_confirmed'])
    def test_hypothesis_has_no_supported_status(self):
        self.assertEqual(self.save(basis='hypothesis',frames=[])['interpretation_status'],'hypothesis')
    def test_outside_window_omitted(self):
        self.save();self.assertEqual(self.f.context(30,5)['term_notes'],[])
    def test_invalid_evidence_rejected(self):
        for kw in [dict(asr_text='not said'),dict(frames=[]),dict(frames=[dict(time=16,sha256='a'*64)]),dict(start=-1),dict(end=float('nan')),dict(basis='audio_confirmed'),dict(basis='hypothesis'),dict(term='')]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.save(**kw)
    def test_frame_damage_marks_stale(self):
        self.save();(self.folder/'frames/0.jpg').write_bytes(b'changed')
        self.assertTrue(term_notes.checked(self.f.lesson,self.data())[0]['stale'])
    def test_rebuilt_changed_speech_invalidates(self):
        self.save();self.f.add('replacement',0,60,[(10,20,'Different flaks statement')],[(15,b'Flux visible')])
        self.assertTrue(term_notes.checked(self.f.lesson,self.data())[0]['stale'])
    def test_duplicate_id_and_explicit_remove(self):
        self.save()
        with self.assertRaises(ValueError):self.save()
        self.assertEqual(term_notes.term_note(self.args(remove='flux'))['term_notes'],[])
    def test_relocation(self):
        self.save();new=self.f.directory/'moved';shutil.move(self.f.path,new)
        d=new/'lessons/lesson-01';self.assertFalse(term_notes.checked(d,course.read(d/'lesson.json'))[0]['stale'])
    def attach(self,text):
        p=self.f.directory/'Example.java';p.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):course.attach(self.args(kind='code',source=str(p),none=False,ref=None))
        return self.data()['code']['content_sha256']
    def test_code_term_and_replacement(self):
        h=self.attach('Flux<String> values;');row=self.save(basis='attached_code',frames=[],code=dict(sha256=h,file='Example.java'));self.assertFalse(row['stale'])
        self.attach('Mono<String> value;');self.assertTrue(term_notes.checked(self.f.lesson,self.data())[0]['stale'])
    def test_code_missing_term_rejected(self):
        h=self.attach('Mono<String> value;')
        with self.assertRaises(ValueError):self.save(basis='attached_code',frames=[],code=dict(sha256=h,file='Example.java'))
    def test_added_unrelated_packet_preserves(self):
        self.save();self.f.add('later',60,90,[(70,80,'Later')]);self.assertFalse(term_notes.checked(self.f.lesson,self.data())[0]['stale'])
