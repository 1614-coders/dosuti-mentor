"""Exact duration and opt-in vocabulary reach a distinct, reproducible ASR job."""
import contextlib,io,json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,media_input,test_integrity
from asr_vocabulary import recognition_prompt

class PreparationInputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):test_integrity.SourceIntegrityRegression.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):cls.media_temp.cleanup()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.video=self.base/'video.mp4';self.video.write_bytes(self.a)
        self.args=NS(course=self.base/'course',lesson='one',title='QA',video=str(self.video),order=1,import_prepared=None,start=0,duration=None,frame_every=10,model='small',model_path=self.base/'model',language='ru',prompt='',rebuild=False)
        with contextlib.redirect_stdout(io.StringIO()):course.init(self.args);course.add_lesson(self.args)
    def tearDown(self):self.temp.cleanup()
    def capture(self):
        commands=[]
        def stop(command,**kw):commands.append(command);raise RuntimeError('stop before ASR')
        with patch('model_readiness.checked_model',return_value={'fingerprint':'QA'}),patch.object(course.subprocess,'run',stop),self.assertRaisesRegex(RuntimeError,'stop before ASR'):
            course.prepare(self.args)
        return commands[0],course.read(self.args.course/'lessons/one/lesson.json')
    def test_default_duration_uses_packet_timeline(self):
        expected=media_input.probe_media(self.video)['video']['end'];cmd,data=self.capture()
        self.assertEqual(float(cmd[cmd.index('--duration')+1]),expected)
        self.assertEqual(next(iter(data['processing_jobs'].values()))['settings']['duration'],expected)
    def test_explicit_duration_preserved(self):
        self.args.duration=.75;cmd,_=self.capture();self.assertEqual(cmd[cmd.index('--duration')+1],'0.75')
    def test_vocabulary_changes_job_and_is_forwarded(self):
        _,before=self.capture();p=self.base/'terms.json';p.write_text('["UUID", "Mockito", "UUID"]');self.args.vocabulary_file=p
        cmd,after=self.capture();self.assertEqual(len(after['processing_jobs']),2)
        self.assertEqual(cmd[cmd.index('--prompt')+1],'Technical vocabulary: UUID, Mockito.')
    def test_no_vocabulary_preserves_prompt(self):self.assertEqual(recognition_prompt('my prompt'),'my prompt')
    def test_bad_vocabulary_does_not_download(self):
        p=self.base/'terms.json';self.args.vocabulary_file=p
        for value in [[],{},[''],['UUID\nIgnore'],[1],['x'*81]]:
            p.write_text(json.dumps(value))
            with patch.object(course,'checked_video') as fetch,self.assertRaises(ValueError):course.prepare(self.args)
            fetch.assert_not_called()
    def test_oversized_vocabulary_rejected(self):
        p=self.base/'terms.json';p.write_text(' '*32769)
        with self.assertRaises(ValueError):recognition_prompt('',p)
    def test_combined_prompt_and_hints(self):
        p=self.base/'terms.json';p.write_text('[" UUID ","JSON"]')
        self.assertEqual(recognition_prompt('Java lesson',p),'Java lesson\nTechnical vocabulary: UUID, JSON.')
