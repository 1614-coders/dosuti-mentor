"""Execution limits only: keep model, decoding and cache identities unchanged."""
import os


def policy(mode='standard'):
    if mode not in ('standard', 'economy'):
        raise ValueError('CPU mode must be standard or economy')
    return dict(mode=mode, asr_threads=2 if mode == 'economy' else 4,
                ffmpeg_threads=1 if mode == 'economy' else None)


def configure_environment(mode):
    limits = policy(mode)
    if mode == 'economy':
        # Set before NumPy/BLAS imports in the private lecture worker.
        for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                     'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            os.environ[name] = str(limits['asr_threads'])
    return limits
