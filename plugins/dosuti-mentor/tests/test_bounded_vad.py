"""Preserve Silero state and speech intervals with bounded ONNX batches."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from bounded_vad import BoundedVADSession,install


class FakeSession:
    def __init__(self):self.sizes=[]
    def run(self,names,inputs,**kwargs):
        self.sizes.append(len(inputs['input']))
        values=np.cumsum(inputs['input'][:,0])+inputs['h'][0]
        return [values[:,None],values[-1:],inputs['c']+len(values)]


class BoundedVADTests(unittest.TestCase):
    def test_state_and_output_order_across_multiple_batches(self):
        data=dict(input=np.ones((1201,576),dtype='float32'),h=np.zeros(1),c=np.zeros(1))
        original=FakeSession().run(None,data);session=FakeSession()
        actual=BoundedVADSession(session).run(None,data)
        for left,right in zip(original,actual):np.testing.assert_array_equal(left,right)
        self.assertEqual(session.sizes,[512,512,177])

    def test_small_batch_uses_original_contract(self):
        session=FakeSession();data=dict(input=np.ones((1,576)),h=np.zeros(1),c=np.zeros(1))
        BoundedVADSession(session).run(None,data);self.assertEqual(session.sizes,[1])

    def test_backend_failure_propagates(self):
        session=NS(run=lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('QA backend failure')))
        with self.assertRaisesRegex(RuntimeError,'QA backend failure'):
            BoundedVADSession(session).run(None,dict(input=np.ones((700,576)),h=np.zeros(1),c=np.zeros(1)))

    def test_unknown_large_batch_contract_rejected(self):
        with self.assertRaises(ValueError):BoundedVADSession(FakeSession()).run(['output'],dict(input=np.ones((700,576)),h=np.zeros(1),c=np.zeros(1)))

    def test_install_is_idempotent(self):
        import faster_whisper.vad as vad
        model=NS(session=FakeSession())
        with patch.object(vad,'get_vad_model',return_value=model):
            install();first=model.session;install();self.assertIs(first,model.session)

    def test_real_onnx_probabilities_and_intervals(self):
        import faster_whisper.vad as vad
        model=vad.get_vad_model();original=model.session
        audio=np.random.default_rng(7).normal(0,.1,512*1400).astype('float32')
        audio[16000:32000]=0
        expected=model(audio.copy());intervals=vad.get_speech_timestamps(audio.copy())
        with patch.object(model,'session',BoundedVADSession(original)):
            actual=model(audio.copy());current=vad.get_speech_timestamps(audio.copy())
        np.testing.assert_allclose(actual,expected,atol=1e-6,rtol=1e-6)
        self.assertEqual(intervals,current)
