"""Timestamped video samples, transient screen content and source preservation."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from PIL import Image
import imageio_ffmpeg

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import course
import screen_evidence as screens


class ScreenEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_temp=tempfile.TemporaryDirectory()
        cls.media=Path(cls.media_temp.name)
        cls.ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        for name,filter in [('flash',"drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill:enable='gte(t,0.75)*lt(t,1.25)'"),
                            ('brief',"drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill:enable='gte(t,0.1)*lt(t,0.2)'"),
                            ('static','null')]:
            subprocess.run([cls.ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi',
                '-i','color=c=black:s=160x90:r=10:d=6','-vf',filter,'-c:v','libx264',
                '-pix_fmt','yuv420p',str(cls.media/(name+'.mp4'))],check=True,capture_output=True)

    @classmethod
    def tearDownClass(cls):cls.media_temp.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.video=self.root/'video.mp4';self.video.write_bytes((self.media/'flash.mp4').read_bytes())
        self.checksum=course.file_hash(self.video)
        self.args=NS(course=self.root/'course',lesson='one',profile='default',title='Screen',
                     video=str(self.video),order=1,start=0,duration=2,sample_every=.25)
        with contextlib.redirect_stdout(io.StringIO()):course.init(self.args);course.add_lesson(self.args)
        self.lesson=self.args.course/'lessons/one'

    def tearDown(self):self.temp.cleanup()

    def scan(self,**params):
        return screens.scan(self.video,self.root/'scans',params.get('start',0),params.get('duration',2),
                            params.get('sample_every',.25),self.checksum)

    def red(self,path):
        with Image.open(path) as image:r,g,b=image.convert('RGB').getpixel((80,45))
        return r>180 and g<30 and b<30

    def register(self):
        with contextlib.redirect_stdout(io.StringIO()):course.scan_screen(self.args)
        return course.read(self.lesson/'lesson.json')['screen_scans'][0]

    def context(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):course.context(NS(**vars(self.args),time=0,window=2))
        return json.loads(output.getvalue())

    def test_dense_scan_captures_brief_state_missed_by_periodic_frames(self):
        coarse=self.root/'coarse'
        rows=screens.extract_frames(self.video,coarse,0,3,2,self.ffmpeg)
        self.assertFalse(any(self.red(coarse/f['file']) for f in rows))
        dense=self.scan();receipt=course.read(dense/'screen.json')
        red=[f for f in receipt['frames'] if self.red(dense/f['file'])]
        self.assertTrue(red)
        self.assertTrue(any(f['review_candidate'] for f in red))
        self.assertTrue(all(.75<=f['approx_seconds']<1.25 for f in red))

    def test_timestamps_come_from_decoder_and_match_sampled_colors(self):
        folder=self.root/'offset'
        rows=screens.extract_frames(self.video,folder,.5,1.5,.25,self.ffmpeg)
        for row in rows:
            self.assertEqual(row['approx_seconds'],.5+row['decoded_offset'])
            self.assertEqual(self.red(folder/row['file']),.75<=row['source_seconds']<1.25)
            self.assertEqual(row['timestamp_accuracy'],'decoded_video_timestamp')

    def test_short_clip_with_large_interval_still_has_initial_frame(self):
        rows=screens.extract_frames(self.video,self.root/'short',0,.2,10,self.ffmpeg)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['approx_seconds'],0)

    def test_samples_do_not_prove_that_every_short_change_was_seen(self):
        self.video.write_bytes((self.media/'brief.mp4').read_bytes());self.checksum=course.file_hash(self.video)
        folder=self.scan(sample_every=.5)
        receipt=course.read(folder/'screen.json')
        self.assertFalse(any(self.red(folder/f['file']) for f in receipt['frames']))
        self.assertIn('Changes between samples may be missed.',receipt['limitations'])
        self.assertTrue(all(f['verification']=='not_inspected' for f in receipt['frames']))

    def test_static_video_keeps_samples_but_reduces_candidate_list(self):
        self.video.write_bytes((self.media/'static.mp4').read_bytes());self.checksum=course.file_hash(self.video)
        folder=self.scan();r=course.read(folder/'screen.json')
        self.assertGreater(len(r['frames']),1)
        self.assertEqual(sum(f['review_candidate'] for f in r['frames']),1)
        self.assertTrue(all((folder/f['file']).is_file() for f in r['frames']))

    def test_repeat_scan_reuses_verified_files_without_ffmpeg(self):
        first=self.scan()
        with patch.object(screens,'preparation_run',side_effect=AssertionError('FFmpeg called')):
            self.assertEqual(self.scan(),first)

    def test_parallel_scans_publish_once(self):
        real=screens.extract_frames
        with patch.object(screens,'extract_frames',wraps=real) as extract,ThreadPoolExecutor(2) as pool:
            folders=list(pool.map(lambda _:self.scan(),range(2)))
        self.assertEqual(folders[0],folders[1]);self.assertEqual(extract.call_count,1)

    def test_failed_extraction_leaves_no_published_scan(self):
        with patch.object(screens,'preparation_run',side_effect=subprocess.CalledProcessError(1,['ffmpeg'])):
            with self.assertRaises(subprocess.CalledProcessError):self.scan()
        self.assertEqual(list((self.root/'scans').glob('scan-*')),[])
        self.assertEqual(list((self.root/'scans').glob('.pending-*')),[])
        self.assertTrue((self.scan()/'screen.json').is_file())

    def test_changed_source_during_scan_is_not_published(self):
        real=screens.extract_frames
        def mutate(*a,**kw):
            rows=real(*a,**kw);self.video.write_bytes((self.media/'static.mp4').read_bytes());return rows
        with patch.object(screens,'extract_frames',mutate):
            with self.assertRaisesRegex(ValueError,'changed during'):self.scan()
        self.assertEqual(list((self.root/'scans').glob('scan-*')),[])

    def test_corrupt_saved_frame_is_rejected_on_repeat(self):
        folder=self.scan();r=course.read(folder/'screen.json')
        (folder/r['frames'][0]['file']).write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError,'integrity changed'):self.scan()

    def test_screen_only_context_does_not_claim_speech_coverage(self):
        self.register();result=self.context()
        self.assertTrue(result['frames']);self.assertEqual(result['speech'],[])
        self.assertFalse(result['coverage']['complete'])
        self.assertIn('screen_scan_candidates_do_not_verify_meaning',result['warnings'])

    def test_context_detects_changed_selected_frame(self):
        entry=self.register();folder=Path(entry['folder']);r=course.read(folder/'screen.json')
        (folder/r['frames'][0]['file']).write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError,'integrity changed'):self.context()

    def test_rebuild_replaces_corrupt_scan_and_preserves_old_files(self):
        entry=self.register();folder=Path(entry['folder']);r=course.read(folder/'screen.json')
        broken=folder/r['frames'][0]['file'];broken.write_bytes(b'broken')
        self.args.rebuild=True;new=self.register()
        data=course.read(self.lesson/'lesson.json')
        self.assertNotEqual(new['folder'],str(folder))
        self.assertEqual(len(data['screen_scans']),1)
        self.assertEqual(data['retired_screen_scans'][0]['folder'],str(folder))
        self.assertEqual(broken.read_bytes(),b'broken')
        self.assertTrue(self.context()['frames'])
        self.args.rebuild=False
        self.assertEqual(self.register()['folder'],new['folder'])

    def test_context_detects_changed_scan_receipt(self):
        entry=self.register();(Path(entry['folder'])/'screen.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'metadata integrity'):self.context()

    def test_repeat_does_not_bless_modified_receipt(self):
        folder=self.scan();path=folder/'screen.json';r=course.read(path)
        r['frames'][0]['approx_seconds']=1.9;course.save(path,r)
        with self.assertRaisesRegex(ValueError,'metadata integrity'):self.scan()

    def test_invalid_timestamps_are_rejected_even_with_updated_metadata_hash(self):
        entry=self.register();folder=Path(entry['folder']);path=folder/'screen.json'
        r=course.read(path);r['frames'][0]['approx_seconds']=math.inf;course.save(path,r)
        course.update_lesson(self.lesson,lambda data:data['screen_scans'][0].update(receipt_sha256=course.file_hash(path)))
        with self.assertRaisesRegex(ValueError,'timestamp'):self.context()

    def test_context_detects_replaced_original_video(self):
        self.register();self.video.write_bytes((self.media/'static.mp4').read_bytes())
        with self.assertRaisesRegex(ValueError,'changed since registration'):self.context()

    def test_moving_course_preserves_screen_evidence_and_ids(self):
        entry=self.register();before=self.context()
        moved=self.root/'moved';self.args.course.rename(moved);self.args.course=moved;self.lesson=moved/'lessons/one'
        after=self.context()
        self.assertEqual([f['id'] for f in before['frames']],[f['id'] for f in after['frames']])
        self.assertTrue(all(Path(f['absolute_path']).is_file() for f in after['frames']))
        self.assertEqual(course.read(self.lesson/'lesson.json')['screen_scans'][0]['receipt_sha256'],entry['receipt_sha256'])

    def test_invalid_or_excessive_scan_arguments_are_rejected(self):
        for kw in [dict(start=-1),dict(duration=0),dict(sample_every=0),dict(start=math.nan),
                   dict(duration=math.inf),dict(duration=301),dict(duration=200,sample_every=.1)]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.scan(**kw)

    def test_requested_interval_after_video_end_has_no_successful_scan(self):
        with self.assertRaisesRegex(ValueError,'No video frames'):self.scan(start=10)


if __name__=='__main__':unittest.main()
