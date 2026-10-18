"""Load verified mono PCM without a second full-size byte buffer."""
import wave


def read_pcm(path):
    import numpy as np
    with wave.open(str(path), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError('Expected mono 16-bit PCM at 16 kHz')
        frames = wav.getnframes()
        if not frames:
            raise ValueError('Expected nonempty PCM')
        audio = np.empty(frames, dtype=np.float32)
        for start in range(0, frames, 30 * 16000):
            count = min(30 * 16000, frames - start)
            raw = wav.readframes(count)
            if len(raw) != count * 2:
                raise ValueError('Truncated PCM; preserve source and rebuild audio')
            audio[start:start + count] = np.frombuffer(raw, dtype='<i2')
        audio /= 32768.0
        return audio
