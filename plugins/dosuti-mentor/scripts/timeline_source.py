"""A unit depends on one adjacent project transition and optional checked screen stages."""
from copy import deepcopy

FIELDS={'kind','role','notes','timeline_id','track_id','before','after','stages'}


def validate(source,stored=False):
    import course
    if set(source)!=FIELDS|({'evidence_sha256'} if stored else set()):raise ValueError('Invalid timeline source fields')
    for key in ('timeline_id','track_id','before','after'):course.slug(source[key])
    if source['before']==source['after']:raise ValueError('Transition needs distinct endpoints')
    if not isinstance(source['stages'],dict) or set(source['stages'])!={'before','after'}:raise ValueError('Stage bindings require before and after, nullable IDs')
    for identity in source['stages'].values():
        if identity is not None:course.slug(identity)
    if stored:
        from source_integrity import digest
        digest(source['evidence_sha256'])


def stage_context(root,step,code,identity):
    import course
    from code_stages import checked
    from study_units import digest
    directory=root/'lessons'/step['lesson'];data=course.read(directory/'lesson.json')
    rows=[r for r in checked(directory,data) if r.get('id')==identity]
    if len(rows)!=1 or rows[0]['stale']:raise ValueError('Bound code stage missing or stale')
    row=rows[0]
    if row['code_sha256']!=code['content_sha256'] or row['file'] not in code['inventory']:
        raise ValueError('Stage does not belong to this transition endpoint snapshot')
    original=data['code_stages'][identity]
    signature=digest(dict(annotation={k:v for k,v in original.items() if k!='updated'},overlaps=sorted(row['overlapping_stage_ids'])))
    # Paths/derived historical flag are not part of the semantic identity.
    result={k:deepcopy(row[k]) for k in ('id','title','file','screen_file','start','end','match','notes','frames','usable_as_screen_match','overlapping_stage_ids')}
    result.update(lesson=step['lesson'],verification='assistant_annotation_not_automatic_screen_proof')
    return signature,result


def evidence(root,source,writing=False):
    from project_timeline import load,selected,changes
    from study_units import digest
    registry=load(root);record=registry['timelines'].get(source['timeline_id'])
    if record is None:raise ValueError('Timeline source missing')
    track=next((t for t in record['tracks'] if t['id']==source['track_id']),None)
    if track is None:raise ValueError('Timeline track missing')
    index=next((i for i,s in enumerate(track['steps']) if s['id']==source['before']),None)
    if index is None or index+1>=len(track['steps']) or track['steps'][index+1]['id']!=source['after']:
        raise ValueError('Timeline source must bind an adjacent ordered transition')
    steps=track['steps'][index:index+2];codes=[];endpoints={};stage_hashes={}
    for label,step in zip(('before','after'),steps):
        code,historical=selected(root,step)
        if code['content_sha256']!=step['code_sha256']:raise ValueError('Timeline endpoint snapshot changed; review series')
        codes.append(code);item=dict(step=deepcopy(step),historical=historical,lecture_match='unverified',stage=None)
        if source['stages'][label] is not None:
            stage_hashes[label],item['stage']=stage_context(root,step,code,source['stages'][label])
            item['lecture_match']=item['stage']['match']
        endpoints[label]=item
    comparison=(dict(status='gap',reason='Explicit gap; no continuous evolution inferred') if steps[1]['gap_before']
                else dict(status='observed',**changes(*codes)))
    signature=digest(dict(timeline_id=record['id'],title=record['title'],uncertainties=record['uncertainties'],
                          track=dict(id=track['id'],title=track['title']),steps=steps,stage_hashes=stage_hashes))
    value=dict(source,evidence_sha256=signature)
    if not writing and source['evidence_sha256']!=signature:raise ValueError('Timeline transition or stage annotation changed')
    context={k:deepcopy(source[k]) for k in FIELDS}
    context.update(evidence_sha256=signature,title=record['title'],uncertainties=deepcopy(record['uncertainties']),
                   endpoints=endpoints,comparison=comparison,
                   continuous_video_evolution_verified=False,
                   verification='snapshot_transition_and_manual_stage_annotations_not_automatic_causal_proof')
    return value,context
