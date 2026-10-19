"""Proposed career routes bound to intent, dated evidence, and explicit study units."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from topic_research import fields,text
from study_units import strings


def validate(metadata):
    import course
    from source_integrity import digest
    fields(metadata,'sample_id sample_sha256 learner_context steps')
    if metadata['sample_id'] is None:
        if metadata['sample_sha256'] is not None:raise ValueError('Sample hash requires sample ID')
    else:course.slug(metadata['sample_id']);digest(metadata['sample_sha256'])
    context=metadata['learner_context'];fields(context,'self_report available_time timeframe unknowns')
    for key in ('self_report','available_time','timeframe'):text(context[key],key,True)
    strings(context['unknowns'],'unknowns')
    steps=metadata['steps']
    if not isinstance(steps,list) or not 1<=len(steps)<=50:raise ValueError('Route requires1..50 steps')
    seen=set()
    for step in steps:
        fields(step,'id title rationale kind requirements prerequisites units')
        course.slug(step['id']);text(step['title'],'step title');text(step['rationale'],'step rationale')
        if step['id'] in seen:raise ValueError('Duplicate route step')
        if step['kind'] not in ('foundation','market','project','review'):raise ValueError('Unknown route step kind')
        for key in ('requirements','prerequisites'):
            strings(step[key],key)
            for identity in step[key]:course.slug(identity)
            if len(set(step[key]))!=len(step[key]):raise ValueError('Duplicate step references')
        if any(identity not in seen for identity in step['prerequisites']):raise ValueError('Prerequisite must precede dependent step')
        seen.add(step['id'])
        if step['kind']=='market':
            if metadata['sample_id'] is None or not step['requirements']:raise ValueError('Market step requires sample and requirement evidence')
        elif step['requirements']:raise ValueError('Only market steps claim market requirements')
        if not isinstance(step['units'],list) or len(step['units'])>20:raise ValueError('Step supports up to20 units')
        units=set()
        for unit in step['units']:
            fields(unit,'id sha256');course.slug(unit['id']);digest(unit['sha256'])
            if unit['id'] in units:raise ValueError('Duplicate step unit')
            units.add(unit['id'])


def status(args,metadata,with_progress=False):
    import career_evidence,study_units,course
    validate(metadata);root=course.root(args);reasons=[];sample=None
    if metadata['sample_id'] is not None:
        try:
            sample=career_evidence.career(SimpleNamespace(course=root,sample_id=metadata['sample_id']))
            if not sample.get('valid') or not sample['sample'] or sample['sample']['content_sha256']!=metadata['sample_sha256']:reasons.append('Career sample changed or needs review')
        except (ValueError,OSError) as error:reasons.append('Career evidence: '+str(error))
    if any(step['kind']=='market' for step in metadata['steps']) and (sample or {}).get('freshness',{}).get('needs_review'):
        reasons.append('Career observations require a freshness review before market-based planning')
    requirements={r['id']:r for r in (sample or {}).get('requirements',[])}
    for step in metadata['steps']:
        for identity in step['requirements']:
            evidence=requirements.get(identity);counts=evidence.get('current_counts') if evidence else None
            if not counts or not (counts['required'] or counts['preferred']) or evidence['classification_conflicts']:
                reasons.append('No unambiguous current sample support: '+identity)
    names=sorted({unit['id'] for step in metadata['steps'] for unit in step['units']});units={}
    if names:
        try:
            registry=study_units.load(root)
            for name in names:
                if name not in registry['units']:reasons.append('Missing study unit: '+name)
            units=study_units.checked_units(root,registry,[name for name in names if name in registry['units']],include_contexts=False)
            for step in metadata['steps']:
                for reference in step['units']:
                    row=units.get(reference['id'])
                    if row and (row['stale'] or row['content_sha256']!=reference['sha256']):reasons.append('Study unit changed or needs review: '+row['id'])
        except (ValueError,OSError) as error:reasons.append('Study units: '+str(error))
    result=dict(valid=not reasons,needs_review=bool(reasons),reasons=list(dict.fromkeys(reasons)),sample_as_of=(sample or {}).get('sample',{}).get('as_of') if (sample or {}).get('sample') else None,
                freshness=(sample or {}).get('freshness'),learner_context=deepcopy(metadata['learner_context']),steps=deepcopy(metadata['steps']))
    if with_progress:
        import unit_learning
        from learning_tools import learner
        try:
            journal=unit_learning.load(learner(args)/'unit-learning.json',compact=True)
            result['progress']={}
            for name in names:
                if name not in units:continue
                view=unit_learning.view(SimpleNamespace(session=getattr(args,'session',None),history_limit=0),units[name],journal)
                from adaptive_learning import recommendations
                adaptation=recommendations(units[name],journal,course.now())
                result['progress'][name]=dict(criteria=view['criteria'],all_criteria_demonstrated=view['all_criteria_demonstrated'],stale=units[name]['stale'],evidence_time_valid=all(row['time_valid'] for row in adaptation['criteria']),later_verified_criteria=adaptation['later_verified_criteria'])
            result['progress_valid']=True
        except (ValueError,OSError) as error:result.update(progress_valid=False,progress={},progress_reason=str(error))
    return result


def compile_route(args,card):
    import course,study_units,career_evidence
    fields(card,'summary sample_id learner_context steps'+(' step_bases' if isinstance(card,dict) and 'step_bases' in card else ''));text(card['summary'],'route summary')
    draft=deepcopy(card);root=course.root(args)
    names=[]
    if not isinstance(draft['steps'],list):raise ValueError('Route steps require a list')
    for step in draft['steps']:
        fields(step,'id title rationale kind requirements prerequisites units')
        strings(step['units'],'units')
        for name in step['units']:course.slug(name);names.append(name)
    registry=study_units.load(root)
    if any(name not in registry['units'] for name in names):raise ValueError('Unknown study unit')
    for step in draft['steps']:
        step['units']=[dict(id=name,sha256=registry['units'][name]['content_sha256']) for name in step['units']]
    checksum=None
    if draft['sample_id'] is not None:
        sample=career_evidence.career(SimpleNamespace(course=root,sample_id=draft['sample_id']))
        if not sample.get('valid') or not sample['sample']:raise ValueError('Sample missing or needs review')
        checksum=sample['sample']['content_sha256']
    metadata=dict(sample_id=draft['sample_id'],sample_sha256=checksum,learner_context=draft['learner_context'],steps=draft['steps'])
    validate(metadata);review=status(args,metadata)
    if not review['valid']:raise ValueError('; '.join(review['reasons']))
    proposal=dict(summary=draft['summary'],steps=[step['title'] for step in draft['steps']],career=metadata)
    if 'step_bases' in draft:
        from learning_intent import validate_bases
        proposal['step_bases']=draft['step_bases'];validate_bases(proposal)
    return proposal


def route(args):
    import learning_intent,course
    from learning_tools import read_json
    from profile_storage import checked
    from storage_lock import file_lock
    incoming=getattr(args,'route_file',None)
    if incoming:
        # Same source lock as sample/unit writes, then the ordinary intent/profile lock.
        root=course.root(args);course.read(root/'course.json')
        if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Career routes must stay outside plugin')
        lock=root/'.study-units.lock';checked(lock)
        with file_lock(lock):
            checked(lock);proposal=compile_route(args,read_json(incoming))
            learning_intent.commit(args,dict(proposal=proposal),sources_locked=True)
    intent=learning_intent.view(args)
    output=dict(intent=intent,verification='assistant_proposed_route_not_employment_or_readiness_prediction')
    for name in ('proposal','plan'):
        if intent.get(name) and 'career' in intent[name]:
            output[name]=status(args,intent[name]['career'],with_progress=True)
            if intent[name]['needs_review'] and not output[name]['needs_review']:
                output[name].update(valid=False,needs_review=True)
                output[name]['reasons'].append('Learning goal or step bases need review')
    return output
