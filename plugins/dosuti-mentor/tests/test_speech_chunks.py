"""Sample and metadata compatibility, including upstream fallback modes."""
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import speech_chunks


class SpeechChunks(unittest.TestCase):
    def setUp(self):
        self.backend=importlib.import_module('faster_whisper.transcribe')
        from faster_whisper.vad import collect_chunks
        self.original=collect_chunks
        self.saved=self.backend.collect_chunks;self.backend.collect_chunks=collect_chunks
        speech_chunks.install();self.collect=self.backend.collect_chunks
    def tearDown(self):self.backend.collect_chunks=self.saved
    def compare(self,audio,chunks,**kwargs):
        expected,metadata=self.original(audio,chunks,**kwargs)
        actual,new_metadata=self.collect(audio,chunks,**kwargs)
        self.assertEqual(metadata,new_metadata);self.assertEqual(len(actual),len(expected))
        for a,b in zip(actual,expected):
            np.testing.assert_array_equal(a,b);self.assertEqual(a.dtype,b.dtype)
    def test_many_chunks_and_edges(self):
        audio=np.random.default_rng(10).normal(0,.1,32000).astype(np.float32)
        chunks=[dict(start=i,end=i+80) for i in range(0,32000,160)]
        self.compare(audio,chunks,sampling_rate=8000)
        self.compare(audio,[dict(start=0,end=len(audio))])
    def test_empty_speech_and_zero_length_chunk(self):
        self.compare(np.ones(10,dtype=np.float32),[])
        self.compare(np.ones(10,dtype=np.float32),[dict(start=3,end=3)])
    def test_finite_duration_and_other_dtypes_use_upstream(self):
        chunks=[dict(start=0,end=16000),dict(start=24000,end=48000)]
        self.compare(np.ones(48000,dtype=np.float32),chunks,max_duration=1)
        for dtype in (np.int16,np.float64):self.compare(np.arange(48000,dtype=dtype),chunks)
    def test_one_concatenation_instead_of_growing_prefix(self):
        audio=np.ones(32000,dtype=np.float32);chunks=[dict(start=i,end=i+10) for i in range(0,32000,20)]
        with patch.object(np,'concatenate',wraps=np.concatenate) as concatenate:
            self.collect(audio,chunks);self.assertEqual(concatenate.call_count,1)
    def test_short_recording_keeps_original_collection(self):
        audio=np.ones(32000,dtype=np.float32)
        for count in (2,31,32):
            chunks=[dict(start=i*10,end=i*10+5) for i in range(count)]
            with self.subTest(count=count),patch.object(np,'concatenate',wraps=np.concatenate) as concatenate:
                self.collect(audio,chunks)
                self.assertEqual(concatenate.call_count,count if count<32 else 1)
    def test_input_and_metadata_not_mutated(self):
        audio=np.arange(100,dtype=np.float32);before=audio.copy();chunks=[dict(start=1,end=10),dict(start=20,end=40)]
        result,metadata=self.collect(audio,chunks);result[0][0]=-1
        metadata[0]['segments'].append(dict(start=50,end=60))
        np.testing.assert_array_equal(audio,before);self.assertEqual(len(chunks),2)
    def test_repeated_install_does_not_wrap_again(self):
        speech_chunks.install();self.assertIs(self.backend.collect_chunks,self.collect)
