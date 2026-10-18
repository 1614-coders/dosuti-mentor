"""Bound Silero ONNX batches while carrying its recurrent states unchanged."""


class BoundedVADSession:
    def __init__(self, session):
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session, name)

    def run(self, output_names, inputs, **kwargs):
        import numpy as np
        audio = inputs['input']
        if len(audio) <= 512:
            return self.session.run(output_names, inputs, **kwargs)
        if output_names is not None or set(inputs) != {'input', 'h', 'c'}:
            raise ValueError('Unsupported VAD session contract; review the installed backend')
        h, c = inputs['h'], inputs['c']
        outputs = []
        for start in range(0, len(audio), 512):
            output, h, c = self.session.run(None, dict(input=audio[start:start + 512], h=h, c=c), **kwargs)
            outputs.append(output)
        return [np.concatenate(outputs, axis=0), h, c]


def install():
    from speech_chunks import install as install_linear_chunks
    install_linear_chunks()
    from faster_whisper.vad import get_vad_model
    model = get_vad_model()
    if not isinstance(model.session, BoundedVADSession):
        model.session = BoundedVADSession(model.session)
