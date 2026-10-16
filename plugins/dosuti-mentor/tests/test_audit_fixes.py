"""Product regressions from the independent 0.1.6 audit, with real local media."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import wave
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import imageio_ffmpeg

S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
import course, processing_job, screen_evidence as se
from media_input import probe_media, signal_stats


class AuditFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pool=tempfile.TemporaryDirectory();cls.media=Path(cls.pool.name);cls.ff=imageio_ffmpeg.get_ffmpeg_exe()
        def make(name,*args):
            subprocess.run([cls.ff,'-y','-hide_banner','-loglevel','error',*args,str(cls.media/name)],check=True,capture_output=True)
        video=['-f','lavfi','-i','color=c=black:s=320x180:r=10:d=12']
        make('delayed.mkv',*video,'-itsoffset','5','-f','lavfi','-i','sine=frequency=440:duration=2','-c:v','libx264','-c:a','pcm_s16le')
        make('tail.mkv',*video,'-f','lavfi','-i','sine=duration=3','-vf',"drawbox=color=red:t=fill:enable='gte(t,8)'",'-c:v','libx264','-c:a','pcm_s16le')
        make('anti.mkv',*video,'-f','lavfi','-i','aevalsrc=0.25*sin(2*PI*440*t)|-0.25*sin(2*PI*440*t):s=16000:d=12','-c:v','libx264','-c:a','pcm_s16le')
        make('silent.mp4',*video,'-c:v','libx264')
        for name,first in [('multiple.mkv','anullsrc=r=16000:cl=mono:d=12'),('ambiguous.mkv','sine=frequency=220:duration=12')]:
            make(name,*video,'-f','lavfi','-i',first,'-f','lavfi','-i','sine=frequency=440:duration=12','-map','0:v','-map','1:a','-map','2:a','-c:v','libx264','-c:a','pcm_s16le')
        make('full.mp4','-f','lavfi','-i','testsrc2=s=320x180:r=10:d=12','-f','lavfi','-i','sine=duration=12','-c:v','libx264','-c:a','aac','-movflags','+faststart')
        data=(cls.media/'full.mp4').read_bytes();(cls.media/'truncated.mp4').write_bytes(data[:len(data)//2])

    @classmethod
    def tearDownClass(cls):cls.pool.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.args=NS(course=self.root/'course',lesson='one',profile='default',title='Audit',video=str(self.media/'silent.mp4'),order=1)
        self.quiet(course.init,self.args);self.quiet(course.add_lesson,self.args);self.lesson=self.args.course/'lessons/one'

    def tearDown(self):self.temp.cleanup()

    def quiet(self,fn,args):
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):fn(args)
        return buf.getvalue()

    def job(self,name,**kw):
        args=NS(video=self.media/name,output=self.root/('job-'+str(len(list(self.root.glob('job-*'))))),
                start=0,duration=12,frame_every=1,resume=True,prepare_only=True,**kw)
        processing_job.run_job(args,self.ff)
        return args.output,course.read(args.output/'manifest.json')

    def test_delayed_audio_preserves_five_second_offset(self):
        folder,m=self.job('delayed.mkv')
        with wave.open(str(folder/'audio.wav'),'rb') as w:x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2')
        self.assertAlmostEqual(np.flatnonzero(abs(x)>100)[0]/16000,5,delta=.02)
        self.assertEqual(m['actual_duration'],12)

    def test_seek_before_and_inside_delayed_audio(self):
        for start,expected in [(3,2),(6,0)]:
            args=NS(video=self.media/'delayed.mkv',output=self.root/f'seek{start}',start=start,duration=4,frame_every=1,resume=True,prepare_only=True)
            processing_job.run_job(args,self.ff)
            with wave.open(str(args.output/'audio.wav'),'rb') as w:x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2')
            self.assertAlmostEqual(np.flatnonzero(abs(x)>100)[0]/16000,expected,delta=.02)

    def test_visual_tail_survives_audio_end(self):
        folder,m=self.job('tail.mkv');self.assertTrue(any(f['approx_seconds']>=8 for f in m['frames']))

    def test_antiphase_retains_signal_and_discloses_channel(self):
        folder,m=self.job('anti.mkv');self.assertGreater(signal_stats(folder/'audio.wav')['mono_rms'],100)
        self.assertIn('channel_cancellation_using_single_channel',m['audio_quality']['warnings'])
        self.assertIsNotNone(m['audio_quality']['selected_channel'])

    def test_nonempty_second_track_selected(self):
        folder,m=self.job('multiple.mkv');self.assertEqual(m['audio_quality']['selected_track'],2)
        self.assertGreater(signal_stats(folder/'audio.wav')['mono_rms'],100)

    def test_two_nonempty_tracks_require_choice(self):
        with self.assertRaisesRegex(ValueError,'Multiple nonempty'):self.job('ambiguous.mkv')

    def test_explicit_track_and_invalid_channel(self):
        folder,m=self.job('ambiguous.mkv',audio_track=2);self.assertEqual(m['audio_quality']['selected_track'],2)
        with self.assertRaisesRegex(ValueError,'channel'):self.job('anti.mkv',audio_channel=4)

    def test_resume_rejects_changed_track(self):
        folder,m=self.job('ambiguous.mkv',audio_track=2)
        args=NS(video=self.media/'ambiguous.mkv',output=folder,start=0,duration=12,frame_every=1,resume=True,prepare_only=True,audio_track=1)
        with self.assertRaisesRegex(ValueError,'arguments'):processing_job.run_job(args,self.ff)

    def test_no_audio_is_explicit_visual_only(self):
        folder,m=self.job('silent.mp4');self.assertEqual(m['audio_quality']['status'],'no_audio_track')
        self.assertEqual(len(m['frames']),12)

    def test_corrupt_video_fails_before_ready_manifest(self):
        with self.assertRaises((ValueError,subprocess.CalledProcessError)):self.job('truncated.mp4')
        self.assertFalse(list(self.root.glob('job-*/manifest.json')))

    def test_short_valid_video_clamps_requested_interval(self):
        args=NS(video=self.media/'silent.mp4',output=self.root/'short',start=10,duration=20,frame_every=1,resume=True,prepare_only=True)
        processing_job.run_job(args,self.ff);self.assertAlmostEqual(course.read(args.output/'manifest.json')['actual_duration'],2)

    def test_static_screen_present_in_narrow_context(self):
        self.quiet(course.scan_screen,NS(**vars(self.args),start=0,duration=6,sample_every=.5))
        value=json.loads(self.quiet(course.context,NS(**vars(self.args),time=2,window=2)))
        self.assertTrue(value['frames']);self.assertFalse(value['coverage']['complete'])

    def test_small_code_changes_survive_local_comparison(self):
        font=ImageFont.load_default(size=24)
        for old,new in [('==','!='),('<','<='),('+','-'),('!ready','ready'),('count','total')]:
            for size in [14,24,32]:
                before=Image.new('RGB',(1600,900),'#202020');after=before.copy()
                f=ImageFont.load_default(size=size)
                ImageDraw.Draw(before).text((80,100),'if ('+old+')',font=f,fill='white')
                ImageDraw.Draw(after).text((80,100),'if ('+new+')',font=f,fill='white')
                self.assertGreaterEqual(se.local_difference(before,after)['max_tile_pixels'],4,(old,new,size))
        self.assertEqual(se.local_difference(before,before)['max_tile_pixels'],0)

    def test_changed_and_missing_homework_cannot_pass(self):
        source=self.root/'task.txt';source.write_text('Original task')
        self.quiet(course.attach,NS(**vars(self.args),kind='homework',none=False,source=str(source)))
        hw=course.read(self.lesson/'lesson.json')['homework'];args=NS(**vars(self.args),topic='task',stage='homework_passed',details='audit',homework_revision=hw['revision'])
        self.quiet(course.progress,args)
        target=Path(hw['file']);target.chmod(0o644);target.write_text('Different task')
        for remove in [False,True]:
            if remove:target.unlink()
            with self.assertRaisesRegex(ValueError,'content changed'):course.progress(args)
            state=json.loads(self.quiet(course.status,self.args))['progress'][0]
            self.assertTrue(state['invalidated']);self.assertEqual(state['stage'],'needs_review')

    def test_late_scan_does_not_resurrect_retired_generation(self):
        args=NS(**vars(self.args),start=0,duration=6,sample_every=.5)
        self.quiet(course.scan_screen,args)
        ready=threading.Event();release=threading.Event();real=se.scan
        def gated(*a,**kw):
            folder=real(*a,**kw)
            if not kw.get('rebuild'):ready.set();self.assertTrue(release.wait(10))
            return folder
        with patch.object(se,'scan',gated),ThreadPoolExecutor(2) as pool:
            old=pool.submit(course.scan_screen,args);self.assertTrue(ready.wait(10))
            try:course.scan_screen(NS(**vars(args),rebuild=True))
            finally:release.set()
            old.result(timeout=10)
        state=course.read(self.lesson/'lesson.json');self.assertEqual(len(state['screen_scans']),1)
        active={s['folder'] for s in state['screen_scans']};retired={s['folder'] for s in state['retired_screen_scans']}
        self.assertFalse(active & retired)

if __name__=='__main__':unittest.main()
