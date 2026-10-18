"""Conservative ASR diagnostics; absence of warnings never verifies the words."""
import math
import re
from collections import Counter


def diagnose(records):
    texts = [' '.join(re.findall(r'\w+', r.get('text', '').casefold())) for r in records]
    counts = Counter(t for t in texts if t)
    dominant = counts.most_common(1)[0][1] if counts else 0
    runs, first = [], 0
    for index in range(1, len(records) + 1):
        if (index < len(records) and texts[index] and texts[index] == texts[first]
                and 0 <= records[index]['start'] - records[index - 1]['end'] <= 5):
            continue
        if texts[first] and index - first >= 8 and records[index - 1]['end'] - records[first]['start'] >= 20:
            runs.append(dict(start=records[first]['start'], end=records[index - 1]['end'],
                             segments=index - first))
        first = index

    def metric(row, key, predicate):
        value = row.get(key)
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and predicate(value))

    suspicious = sum(metric(r, 'compression_ratio', lambda v: v > 2.4) or
                     (metric(r, 'avg_logprob', lambda v: v < -1.0) and
                      metric(r, 'no_speech_prob', lambda v: v > .6)) for r in records)
    warnings = []
    if not any(texts):
        warnings.append('no_speech_recognized_requires_review')
    if runs or (dominant >= 20 and dominant / len(records) >= .5):
        warnings.append('asr_mass_repetition_requires_review')
    if suspicious >= 3 and suspicious / len(records) >= .25:
        warnings.append('asr_suspicious_metrics_requires_review')
    return dict(schema_version=1, status='requires_review' if warnings else 'unverified',
                warnings=warnings, segments=len(records), dominant_text_segments=dominant,
                suspicious_metric_segments=suspicious, repeated_runs=runs[:20],
                repeated_run_count=len(runs), meaning_verified=False)
