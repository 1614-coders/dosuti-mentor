"""Bounded epistemic metadata; source integrity and recorded review never prove truth."""
from copy import deepcopy
from itertools import islice
import re


_MARKERS=('requires_ocr','no_text_layer','unmapped_glyphs','images not interpreted',
          'missing/merged: review','headers unverified','[review:')
_WARNING=re.compile('|'.join(re.escape(marker) for marker in _MARKERS))
_PAGE=re.compile(r'\[PDF page (\d+)(?:;|\])')


def extraction_context(lines,start,end):
    """Only explicit extraction markers, not keyword-based judgement of factual meaning."""
    pages=[];warnings=[];page=None;pages_truncated=False
    # C-level filtering avoids a Python loop/regex dispatch for every ordinary line.
    # Warnings still cover the whole source, including text after the selected range.
    for line in filter(_WARNING.search,lines):
        for marker in _MARKERS:
            if marker in line and marker not in warnings:warnings.append(marker)
        if len(warnings)==len(_MARKERS):break
    # Only preceding/selected page markers can locate the requested excerpt.
    for number,line in enumerate(islice(lines,end),1):
        match=_PAGE.match(line) if line.startswith('[PDF page ') else None
        if match:page=int(match[1])
        if start<=number and page is not None and page not in pages:
            if len(pages)<200:pages.append(page)
            else:pages_truncated=True
    return dict(pages=pages,pages_truncated=pages_truncated,warnings=warnings,
        scope='explicit_markers_in_whole_source_not_semantic_validation',
        page_binding='extracted_text_marker_not_independent_attestation')


def source_assurance(context):
    review=context.get('review') or {};claims=[]
    start,end=context.get('start_line',1),context.get('end_line',0)
    for claim in review.get('claims',[]):
        if claim['start_line']>end or claim['end_line']<start:continue
        row=deepcopy(claim);status=row.pop('status')
        row.update(recorded_status=status,fact_status=status if status in ('contradictory','insufficient') else 'unverified',
                   anchor=dict(source_id=context.get('source_id'),research_id=context.get('research_id'),
                       source_sha256=(context.get('capture') or {}).get('sha256'),
                       start_line=claim['start_line'],end_line=claim['end_line']))
        for ref in row['evidence']:
            ref['research_id']=context.get('research_id')
            ref['extraction_quality']=deepcopy(context.get('citation_quality',{}).get(ref['source_id'],{}))
        claims.append(row)
    warnings=list(context.get('extraction_quality',{}).get('warnings',[]))
    for claim in claims:
        for ref in claim['evidence']:warnings.extend(ref['extraction_quality'].get('warnings',[]))
    media=context.get('context',{})
    warnings.extend(media.get('warnings',[]))
    if media.get('speech_gaps'):warnings.append('intervals_without_transcript_not_confirmed_silence')
    if any(r.get('quality_warnings') for r in media.get('speech',[])):warnings.append('individual_asr_segments_require_review')
    warnings=list(dict.fromkeys(warnings))
    states={r['fact_status'] for r in claims}
    status='contradictory' if 'contradictory' in states else 'insufficient' if 'insufficient' in states or warnings else 'unverified'
    return dict(status=status,fact_verified=False,independent_check=False,
        review_basis='assistant_recorded_assessment' if review else 'no_structured_fact_review',
        review_binding=context.get('evidence_sha256',context.get('review_sha256')),
        capture={k:deepcopy(context.get('capture',{}).get(k)) for k in ('id','url','origin','sha256','snapshot') if k in context.get('capture',{})},
        integrity_is_not_truth=True,claims=claims,warnings=warnings[:40],warning_count=len(warnings),warnings_truncated=len(warnings)>40,
        provenance={k:deepcopy(context[k]) for k in ('kind','lesson','source_id','research_id','start_line','end_line','start','end','sha256','origin') if k in context},
        locators=deepcopy(context.get('extraction_quality',{})),
        limitation='References/quotes bind to stored text; neither agreement nor acceptance independently validates a claim.')


def summarize(assurances):
    states={r['status'] for r in assurances}
    return dict(status='contradictory' if 'contradictory' in states else 'insufficient' if 'insufficient' in states else 'unverified',
        fact_verified=False,independent_check=False,cross_source_consistency='not_automatically_checked',
        source_count=len(assurances),recorded_claim_count=sum(len(r['claims']) for r in assurances),
        needs_review=any(r['status'] in ('contradictory','insufficient') for r in assurances),
        limitation='No automatic semantic cross-source verification; supported is a recorded assessment, not proof.')
