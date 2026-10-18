"""Summarize checked context, never promote annotations or clean ASR to proof."""


def summarize(context):
    sources = context['sources']
    audio = []
    for source in sources:
        warnings = list(dict.fromkeys(source['audio_quality'].get('warnings', []) +
                                      source['asr_quality'].get('warnings', [])))
        if warnings:
            audio.append(dict(fragment_id=source['fragment_id'], coverage=source['coverage'],
                              warnings=warnings))
    stages = context['code_stages']
    current = [row for row in stages if not row['stale'] and not row['overlapping_stage_ids']]
    differences = [row['id'] for row in current if row['match'] == 'differs']
    partial = [row['id'] for row in current if row['match'] == 'partial']
    stale = [row['id'] for row in stages if row['stale']]
    ambiguous = [row['id'] for row in stages if row['overlapping_stage_ids']]
    uncertainties = context['visual_notes']['uncertainties']
    notes = [dict(index=index, provenance=row.get('provenance'), range=row.get('range'),
                  stale=bool(row.get('stale')))
             for index, row in enumerate(uncertainties[:20])]
    return dict(schema='material_quality_v1', scope=context['requested_window'],
        meaning_verified=False, coverage_complete=context['coverage']['complete'],
        gaps=context['gaps'], video_integrity=context['video_integrity'],
        audio=dict(status='requires_review' if audio else ('unverified' if sources else 'unavailable'),
                   flagged_fragment_count=len(audio), fragments=audio[:20],
                   diagnostics_scope='whole_contributing_fragments', truncated=len(audio)>20),
        screen=dict(status='requires_review' if uncertainties else 'unverified',
                    readability='not_automatically_assessed', uncertainty_count=len(uncertainties),
                    uncertainty_refs=notes, truncated=len(uncertainties)>20),
        code=dict(status='requires_review' if differences or partial or stale or ambiguous else 'unverified',
                  comparison_scope='recorded_snapshot_and_screen_interval',
                  annotation_count=len(stages), difference_count=len(differences),
                  difference_ids=differences[:20], stale_count=len(stale), stale_ids=stale[:20],
                  partial_count=len(partial), partial_ids=partial[:20],
                  ambiguous_count=len(ambiguous), ambiguous_ids=ambiguous[:20],
                  evidence_kind='assistant_annotations_not_automatic_proof',
                  truncated=any(len(rows)>20 for rows in (differences,partial,stale,ambiguous))),
        warnings=list(context['warnings']))
