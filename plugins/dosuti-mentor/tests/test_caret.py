"""Candidate compaction must retain novel code and reversible first occurrences."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from PIL import Image, ImageDraw, ImageFont
import imageio_ffmpeg

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import screen_evidence as screens
from source_integrity import file_hash


class CaretCompaction(unittest.TestCase):
    def image(self,bar=False,text='count == 0',size=24):
        image=Image.new('RGB',(640,360),'#202020')
        d=ImageDraw.Draw(image)
        d.text((40,60),text,font=ImageFont.load_default(size=size),fill='white')
        if bar:d.rectangle((400,60,402,80),fill='white')
        return image

    def feed(self,images):
        detector=screens.RepeatedCaret()
        return [detector.observe(before,after,screens.local_difference(before,after))
                for before,after in zip(images,images[1:])]

    def test_blink_keeps_first_states_then_compacts(self):
        observed=self.feed([self.image(i%2==1) for i in range(8)])
        self.assertEqual(observed[:2],[False,False])
        self.assertTrue(all(observed[2:]))

    def test_permanent_thin_symbol_not_suppressed(self):
        self.assertEqual(self.feed([self.image(),self.image(True),self.image(True)]),[False,False])

    def test_new_operator_during_blink_not_suppressed(self):
        for old,new in [('==','!='),('<','<='),('+','-'),('!ready','ready'),('count','total')]:
            for size in (14,24,32):
                frames=[self.image(i%2==1,old,size) for i in range(6)]
                frames.append(self.image(False,new,size))
                self.assertFalse(self.feed(frames)[-1],(old,new,size))

    def test_blinking_exclamation_not_treated_as_solid_caret(self):
        base=self.image();marked=base.copy();d=ImageDraw.Draw(marked)
        d.rectangle((400,60,402,71),fill='white');d.rectangle((400,78,402,80),fill='white')
        self.assertFalse(any(self.feed([base,marked]*4)))

    def test_moving_caret_not_suppressed(self):
        frames=[]
        for x in range(400,430,4):
            im=self.image();ImageDraw.Draw(im).rectangle((x,60,x+2,80),fill='white');frames.append(im)
        self.assertFalse(any(self.feed(frames)))

    def test_compressed_video_preserves_operator_and_anchors(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for i in range(28):
                self.image(i%2==1,'count == 0' if i<12 else 'count != 0').save(root/f'{i:03d}.png')
            video=root/'blink.mp4'
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error',
                '-framerate','2','-i',str(root/'%03d.png'),'-c:v','libx264','-crf','23',
                '-pix_fmt','yuv420p',str(video)],check=True,capture_output=True)
            folder=screens.scan(video,root/'scans',0,14,.5,file_hash(video))
            receipt=json.loads((folder/'screen.json').read_text());rows=receipt['frames']
            self.assertEqual(len(rows),28)
            self.assertTrue(all((folder/r['file']).is_file() for r in rows))
            self.assertTrue(next(r for r in rows if r['approx_seconds']==6)['review_candidate'])
            self.assertTrue(next(r for r in rows if r['approx_seconds']==10)['review_candidate'])
            self.assertLess(sum(r['review_candidate'] for r in rows),20)
            self.assertTrue(any(r['candidate_reason']=='repeated_caret_like_change' for r in rows))

if __name__=='__main__':unittest.main()
