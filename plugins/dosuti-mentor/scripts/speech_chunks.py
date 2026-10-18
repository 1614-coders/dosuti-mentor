"""Avoid repeatedly copying the growing VAD audio prefix during normal ASR."""
import importlib


def install():
    backend = importlib.import_module('faster_whisper.transcribe')
    original = backend.collect_chunks
    if getattr(original, '_dosuti_linear', False):
        return

    def collect(audio, chunks, sampling_rate=16000, max_duration=float('inf')):
        import numpy as np
        # Keep the upstream contract for batched/future modes we do not optimise.
        if (max_duration != float('inf') or not isinstance(audio, np.ndarray) or audio.dtype != np.float32 or
                audio.ndim != 1 or not isinstance(chunks, list) or len(chunks) < 32):
            return original(audio, chunks, sampling_rate, max_duration)
        selected = np.concatenate([audio[c['start']:c['end']] for c in chunks])
        duration = sum(c['end'] - c['start'] for c in chunks) / sampling_rate
        return [selected], [dict(offset=0.0, duration=duration, segments=list(chunks))]

    collect._dosuti_linear = True
    backend.collect_chunks = collect
