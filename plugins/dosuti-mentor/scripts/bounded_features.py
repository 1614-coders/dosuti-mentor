"""Compute Whisper log-Mel features with bounded FFT intermediates, without ASR cuts."""


class BoundedFeatureExtractor:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def __call__(self, waveform, padding=160, chunk_length=None):
        import numpy as np
        base = self.original
        if chunk_length is not None:
            base.n_samples = chunk_length * base.sampling_rate
            base.nb_max_frames = base.n_samples // base.hop_length
        waveform = np.asarray(waveform, dtype=np.float32)
        if padding:
            waveform = np.pad(waveform, (0, padding))
        # Match the original whole-waveform reflection, including both endpoints.
        waveform = np.pad(waveform, (base.n_fft // 2, base.n_fft // 2), mode='reflect')
        frames = (len(waveform) - base.n_fft) // base.hop_length
        features = np.empty((base.mel_filters.shape[0], frames), dtype=np.float32)
        window = np.hanning(base.n_fft + 1)[:-1].astype('float32')
        block_frames = max(1, 30 * base.sampling_rate // base.hop_length)
        for start in range(0, frames, block_frames):
            end = min(frames, start + block_frames)
            block = waveform[start * base.hop_length:(end - 1) * base.hop_length + base.n_fft]
            spectrum = base.stft(block, base.n_fft, base.hop_length, window=window,
                                 center=False, return_complex=True).astype('complex64')
            magnitudes = np.abs(spectrum) ** 2
            features[:, start:end] = np.log10(np.clip(base.mel_filters @ magnitudes, 1e-10, None))
        # The normalisation maximum is global, never per block.
        np.maximum(features, features.max() - 8.0, out=features)
        features += 4.0
        features /= 4.0
        return features
