"""Bind study units to an accepted research capture and its actual review."""
from copy import deepcopy
from read_snapshot import reuse_reads


def validate(source,stored=False):
    import course
    fields={'kind','role','notes','research_id','source_id','start_line','end_line'}
    if stored:fields|={'sha256','evidence_sha256'}
    if set(source)!=fields:raise ValueError('Invalid research source fields')
    for key in ('research_id','source_id'):course.slug(source[key])
    if any(type(source[k]) is not int for k in ('start_line','end_line')) or not 1<=source['start_line']<=source['end_line']:
        raise ValueError('Research source requires 1 <= start_line <= end_line')
    if stored:
        from source_integrity import digest
        digest(source['sha256']);digest(source['evidence_sha256'])


@reuse_reads
def evidence(root,source,writing=False):
    from topic_research import load_research,snapshot,bind_claims
    from study_units import digest
    folder,_,state=load_research(root,source['research_id'])
    capture=state['sources'].get(source['source_id']);review=state['reviews'].get(source['source_id'])
    if capture is None or capture['status']!='captured':raise ValueError('Research capture missing or unavailable')
    if not review or review['verdict']!='accepted' or review['version_fit']!='compatible':
        raise ValueError('Research source requires an accepted compatible review')
    citation_quality={}
    if 'claims' in review:bind_claims(folder,state,review,stored=True,quality=citation_quality)
    content=snapshot(folder,capture);lines=content.splitlines()
    if source['end_line']>len(lines):raise ValueError('Research source lines exceed content')
    excerpt='\n'.join(lines[source['start_line']-1:source['end_line']])
    if len(excerpt)>100000:raise ValueError('Select at most 100000 research source characters')
    # Semantic identity excludes event timestamps and unrelated journal additions.
    signature=digest(dict(request=state['request'],capture=capture,review=review))
    value=dict(source,sha256=capture['sha256'],evidence_sha256=signature)
    if not writing and any(value[k]!=source[k] for k in ('sha256','evidence_sha256')):
        raise ValueError('Research source or review changed; review unit before learning')
    context={k:deepcopy(source[k]) for k in ('kind','role','notes','research_id','source_id','start_line','end_line')}
    from material_facts import extraction_context
    selected_review=deepcopy(review)
    if 'claims' in selected_review:
        selected_review['claims']=[r for r in selected_review['claims']
            if r['start_line']<=source['end_line'] and r['end_line']>=source['start_line']]
    context.update(citation_quality={r['source_id']:citation_quality[r['source_id']]
                       for claim in selected_review.get('claims',[]) for r in claim['evidence']},
                   extraction_quality=extraction_context(lines,source['start_line'],source['end_line']),text=excerpt,sha256=capture['sha256'],evidence_sha256=signature,
                   request=deepcopy(state['request']),capture=deepcopy(capture),review=selected_review,
                   verification='assistant_recorded_research_review_not_automatic_proof')
    return value,context
