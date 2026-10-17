"""Numerical compatibility and bounded FFT work, including block boundaries."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import numpy as np
from faster_whisper.feature_extractor import FeatureExtractor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from bounded_features import BoundedFeatureExtractor


class BoundedFeatures(unittest.TestCase):
    def compare(self,audio,**kwargs):
        original=FeatureExtractor(**kwargs)
        expected=original(audio)
        actual=BoundedFeatureExtractor(FeatureExtractor(**kwargs))(audio)
        self.assertEqual(actual.shape,expected.shape)
        np.testing.assert_allclose(actual,expected,rtol=1e-6,atol=1e-6)

    def test_signal_and_short_tail_across_block_boundary(self):
        samples=16000*61+73
        rng=np.random.default_rng(4);audio=rng.normal(0,.05,samples).astype('float32')
        audio[30*16000-300:30*16000+300]=.9
        self.compare(audio)

    def test_silence_and_tiny_nonempty_inputs(self):
        for size in (1,159,160,400,16000):
            with self.subTest(size=size):self.compare(np.zeros(size,dtype='float32'))

    def test_global_normalization_does_not_amplify_quiet_blocks(self):
        audio=np.sin(np.arange(61*16000,dtype=np.float32)*.15)*.5
        audio[:30*16000]*=1e-6
        self.compare(audio)

    def test_large_model_feature_width_and_float64_input(self):
        self.compare(np.random.default_rng(5).normal(0,.1,16000*31),feature_size=128)

    def test_fft_intermediates_are_bounded(self):
        base=FeatureExtractor();sizes=[];original=base.stft
        def track(block,*args,**kwargs):
            sizes.append(len(block));return original(block,*args,**kwargs)
        with patch.object(base,'stft',track):
            BoundedFeatureExtractor(base)(np.ones(95*16000,dtype='float32'))
        self.assertEqual(len(sizes),4)
        self.assertLessEqual(max(sizes),30*16000+400)

    def test_padding_and_chunk_length_contract(self):
        audio=np.random.default_rng(6).normal(0,.05,16000*31).astype('float32')
        for padding in (0,160,320):
            expected=FeatureExtractor()(audio,padding=padding,chunk_length=15)
            wrapper=BoundedFeatureExtractor(FeatureExtractor())
            actual=wrapper(audio,padding=padding,chunk_length=15)
            np.testing.assert_allclose(actual,expected,rtol=1e-6,atol=1e-6)
            self.assertEqual(wrapper.n_samples,15*16000)
            self.assertEqual(wrapper.nb_max_frames,1500)
