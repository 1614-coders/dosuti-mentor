"""PCM sample identity, bounded reads, and truncated input refusal."""
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pcm_audio


class PCMAudio(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'audio.wav'
    def tearDown(self):self.temp.cleanup()
    def write(self,samples,channels=1,rate=16000,width=2):
        with wave.open(str(self.path),'wb') as wav:
            wav.setparams((channels,width,rate,0,'NONE','not compressed'));wav.writeframes(samples)
    def test_every_int16_value_is_identical(self):
        samples=np.arange(-32768,32768,dtype=np.int32).astype('<i2');self.write(samples.tobytes())
        expected=samples.astype(np.float32);expected/=32768.0
        actual=pcm_audio.read_pcm(self.path)
        np.testing.assert_array_equal(actual,expected);self.assertEqual(actual.dtype,np.float32)
    def test_bounded_reads_and_short_tail(self):
        samples=np.random.default_rng(8).integers(-32768,32768,16000*61+13,dtype=np.int16)
        self.write(samples.astype('<i2').tobytes());calls=[];original=wave.Wave_read.readframes
        def read(wav,count):calls.append(count);return original(wav,count)
        with patch.object(wave.Wave_read,'readframes',read):actual=pcm_audio.read_pcm(self.path)
        self.assertEqual(calls,[480000,480000,16013])
        np.testing.assert_array_equal(actual,samples.astype(np.float32)/32768.0)
    def test_truncated_pcm_is_rejected(self):
        self.write(b'\0\0'*16000);raw=self.path.read_bytes();self.path.write_bytes(raw[:-1])
        with self.assertRaisesRegex(ValueError,'Truncated'):pcm_audio.read_pcm(self.path)
    def test_empty_and_wrong_format_are_rejected(self):
        for raw,kw in [(b'',{}),(b'\0'*100,dict(channels=2)),(b'\0'*100,dict(rate=8000)),(b'\0'*100,dict(width=1))]:
            with self.subTest(kw=kw):
                self.write(raw,**kw)
                with self.assertRaises(ValueError):pcm_audio.read_pcm(self.path)
