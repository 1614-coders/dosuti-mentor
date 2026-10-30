"""0.8.3: offline speech fixtures and real FFmpeg samples, not real ASR/OCR accuracy."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import tutor_packet
import media_input
import screen_evidence as screens
import test_context as contexts
import test_screen as video_fixtures

class SpeechEvidence(unittest.TestCase):
    def setUp(self):
        self.f=contexts.ContextRegression();self.f.setUp();self.addCleanup(self.f.tearDown)
    def test_packet_orders_speech_without_changing_original(self):
        folder=self.f.add('order',0,20,[(10,12,'Then HashMap.'),(1,3,'Definition first.')])
        original=(folder/'transcript.json').read_bytes()
        packet=tutor_packet.build_packet(folder)
        self.assertEqual([r['start'] for r in packet['speech']],[1,10])
        self.assertEqual((folder/'transcript.json').read_bytes(),original)
    def test_single_uncertain_segment_is_marked_and_preserved(self):
        folder=self.f.add('uncertain',0,20,[(1,3,'Original uncertain term.')])
        rows=course.read(folder/'transcript.json');rows[0].update(avg_logprob=-2,no_speech_prob=.9)
        course.save(folder/'transcript.json',rows)
        manifest=course.read(folder/'manifest.json');manifest['evidence_sha256']['transcript.json']=course.file_hash(folder/'transcript.json');course.save(folder/'manifest.json',manifest)
        packet=tutor_packet.build_packet(folder)
        row=packet['speech'][0]
        self.assertEqual(row['text'],'Original uncertain term.')
        self.assertIn('suspicious_asr_metrics',row['quality_warnings'])
        self.assertEqual(row['verification'],'asr_unverified')
    def test_gaps_and_frame_links_do_not_invent_silence_or_meaning(self):
        folder=self.f.add('links',0,20,[(2,4,'Term'),(10,12,'Code')],[(1,b'a'),(3,b'b'),(11,b'c')])
        packet=tutor_packet.build_packet(folder)
        self.assertEqual(packet['speech_gaps'],[dict(start=0,end=2),dict(start=4,end=10),dict(start=12,end=20)])
        self.assertEqual(packet['speech'][0]['frame_ids'],['f00001','f00002'])
        self.assertFalse(packet['speech'][0]['frame_alignment']['meaning_verified'])
        # Rebuild registration so the real context validates packet integrity.
        self.f.save_packet(folder,packet)
        view=self.f.context(0,20)
        ids={f['id'] for f in view['frames']}
        self.assertTrue(all(set(r['frame_ids'])<=ids for r in view['speech']))
        self.assertEqual(view['speech_gaps'],packet['speech_gaps'])
    def test_blank_speech_is_not_a_confirmed_definition(self):
        folder=self.f.add('blank',0,20,[(1,3,'   ')])
        row=tutor_packet.build_packet(folder)['speech'][0]
        self.assertIn('empty_asr_text',row['quality_warnings'])
        self.assertEqual(row['verification'],'asr_unverified')

    def test_alignment_is_bounded_and_never_uses_a_future_frame_as_prior(self):
        from media_alignment import align
        speech=[dict(start=0,end=.5,text='No prior sample'),dict(start=1,end=50,text='Long segment')]
        frames=[dict(id=f'f{i}',approx_seconds=i) for i in range(1,50)]
        align(speech,frames)
        self.assertEqual(speech[0]['frame_ids'],[])
        self.assertEqual(len(speech[1]['frame_ids']),20)
        self.assertTrue(speech[1]['frame_alignment']['truncated'])
        self.assertFalse(speech[1]['frame_alignment']['meaning_verified'])

class MediaSamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        video_fixtures.ScreenEvidence.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):cls.media_temp.cleanup()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
    def test_adaptive_frames_capture_transition_missed_by_periodic(self):
        rows=screens.extract_frames(self.media/'flash.mp4',self.root/'frames',0,3,2,self.ffmpeg,scene_changes=True)
        from PIL import Image
        red=[]
        for row in rows:
            with Image.open(self.root/'frames'/row['file']) as im:
                r,g,b=im.getpixel((80,45))
                if r>180 and g<30 and b<30:red.append(row)
        self.assertTrue(red)
        self.assertTrue(all(.75<=r['source_seconds']<1.25 for r in red))
        self.assertEqual([r['source_seconds'] for r in rows],sorted(r['source_seconds'] for r in rows))
    def test_audio_only_preparation_has_no_fictitious_frames(self):
        source=self.root/'аудио урок.wav'
        subprocess.run([self.ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=1',str(source)],check=True,capture_output=True)
        info=media_input.probe_media(source)
        self.assertIsNone(info['video'])
        output=self.root/'prepared'
        result=subprocess.run([sys.executable,'-B',str(Path(course.__file__).with_name('lecture.py')),str(source),'--output',str(output),'--duration','1','--prepare-only'],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        manifest=course.read(output/'manifest.json')
        self.assertEqual(manifest['frames'],[])
        self.assertIn('Audio-only source; no visual evidence.',manifest['limitations'])
        self.assertTrue((output/'audio.wav').is_file())
    def test_partial_asr_failure_retries_from_verified_audio_without_publishing_words(self):
        import processing_job
        source=self.root/'voice.wav'
        subprocess.run([self.ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=1',str(source)],check=True,capture_output=True)
        model_dir=self.root/'model';model_dir.mkdir();output=self.root/'job'
        args=NS(video=source,output=output,start=0,duration=1,frame_every=10,model=str(model_dir),
                language='ru',prompt='',prepare_only=False,resume=False)
        def segment(start,end,text):
            return NS(start=start,end=end,text=text,avg_logprob=-.2,no_speech_prob=.1,compression_ratio=1)
        def interrupted():
            yield segment(0,.3,'Partial term')
            raise RuntimeError('ASR fixture interrupted')
        model=NS(feature_extractor=object(),transcribe=lambda *a,**kw:(interrupted(),NS(language='ru')))
        from contextlib import nullcontext
        with contextlib.redirect_stdout(io.StringIO()), \
             patch.object(processing_job,'model_lock',side_effect=lambda p:nullcontext()), \
             patch.object(processing_job,'inspect_model',return_value=dict(fingerprint='synthetic-model')), \
             patch('faster_whisper.WhisperModel',return_value=model), \
             patch('bounded_features.BoundedFeatureExtractor',side_effect=lambda x:x), \
             patch('bounded_vad.install'):
            with self.assertRaisesRegex(RuntimeError,'fixture interrupted'):processing_job.run_job(args,self.ffmpeg)
            self.assertFalse((output/'transcript.json').exists())
            self.assertFalse((output/'tutor_packet.json').exists())
            checkpoint=course.read(output/'checkpoint.json')
            self.assertNotIn('recognition',checkpoint['stages'])
            audio_hash=course.file_hash(output/'audio.wav')
            args.resume=True
            model.transcribe=lambda *a,**kw:(iter([segment(float('nan'),.4,'Invalid')]),NS(language='ru'))
            with self.assertRaisesRegex(ValueError,'finite'):processing_job.run_job(args,self.ffmpeg)
            self.assertFalse((output/'transcript.json').exists())
            model.transcribe=lambda *a,**kw:(iter([segment(-.1,.4,'Exact term'),segment(.6,1.2,'Original explanation'),segment(2,3,'Outside')]),NS(language='ru'))
            processing_job.run_job(args,self.ffmpeg)
        packet=course.read(output/'tutor_packet.json')
        self.assertEqual([r['text'] for r in packet['speech']],['Exact term','Original explanation'])
        self.assertEqual(packet['speech'][0]['start'],0)
        self.assertEqual(packet['speech'][1]['end'],1)
        self.assertIn('asr_timestamp_clipped_to_coverage',packet['speech'][0]['quality_warnings'])
        self.assertEqual(packet['recognition']['discarded_zero_or_outside_segments'],1)
        self.assertEqual(course.file_hash(output/'audio.wav'),audio_hash)

    def test_failed_frame_stage_removes_only_its_staging(self):
        import processing_job
        output=self.root/'job';source=self.media/'flash.mp4'
        args=NS(video=source,output=output,start=0,duration=1,frame_every=10,prepare_only=True,resume=False)
        def failed(video,folder,*a,**kw):
            (folder/'partial.jpg').write_bytes(b'partial')
            raise RuntimeError('frame fixture failed')
        with patch.object(processing_job,'extract_frames',side_effect=failed),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'frame fixture failed'):processing_job.run_job(args,self.ffmpeg)
        self.assertFalse((output/'.frames-staging').exists())
        self.assertTrue((output/'audio.wav').exists())
        self.assertNotIn('frames',course.read(output/'checkpoint.json')['stages'])

    def test_static_scan_decodes_identical_images_once(self):
        from PIL import Image
        source=self.media/'static.mp4';calls=[];original=Image.open
        def opened(path,*args,**kw):
            if Path(path).parent.name=='frames':calls.append(str(path))
            return original(path,*args,**kw)
        with patch.object(Image,'open',opened):
            folder=screens.scan(source,self.root/'scan',0,2,.25,course.file_hash(source))
        rows=course.read(folder/'screen.json')['frames']
        # One comparison decode plus one overview decode of the initial candidate.
        self.assertLessEqual(len(calls),2)
        self.assertEqual(len(rows),7)
        self.assertTrue(all((folder/r['file']).is_file() for r in rows))

if __name__=='__main__':unittest.main()
