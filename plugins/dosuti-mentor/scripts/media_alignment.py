"""Temporal evidence links only: proximity never confirms words or screen meaning."""
from bisect import bisect_left
from asr_quality import record_warnings


def align(speech, frames):
    # Keep IDs/provenance stable while restoring chronological presentation.
    speech.sort(key=lambda r:(r['start'],r['end']))
    frames.sort(key=lambda r:r['approx_seconds'])
    times=[r['approx_seconds'] for r in frames]
    for row in speech:
        warnings=record_warnings(row)
        row['quality_warnings']=warnings
        row['verification']='asr_unverified'  # Preserve the existing verification contract.
        left=bisect_left(times,row['start']);right=bisect_left(times,row['end'])
        if left and (left==len(times) or times[left]>row['start']):
            # Keep all different samples tied at the nearest preceding timestamp.
            left=bisect_left(times,times[left-1])
        row['frame_ids']=[r['id'] for r in frames[left:min(right,left+20)]]
        row['frame_alignment']=dict(kind='temporal_samples_only',meaning_verified=False,
            truncated=right-left>20,preceding_sample_may_be_stale=left<bisect_left(times,row['start']))
