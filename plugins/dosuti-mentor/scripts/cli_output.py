"""Opt-in response projections; journals, validation and grading remain authoritative."""
import json

COMMANDS=('settings','diagnostic','learn-unit','context','resume','intent','study-next','progress')
MUTATIONS=('update_file','event_file','checkpoint_file','review_file','lesson_context_file',
           'lesson_state_file','practice_file')


def omit(value,*keys):
    return {k:v for k,v in value.items() if k not in keys}


def preferences(value):
    return dict(omit(value,'layers'),preference_revisions={k:v['revision'] for k,v in value.get('layers',{}).items()})


def progress(value):
    return omit(value,'unit_history','lesson_history')


def intent(value,embedded=False):
    result=omit(value,'history','learning_progress')
    if not embedded and 'learning_progress' in value:result['learning_progress']=progress(value['learning_progress'])
    return result


def validate_options(args,parser):
    revisions=getattr(args,'event_revision',None);before=getattr(args,'history_before',None)
    if revisions is not None and (len(revisions)>100 or any(n<1 for n in revisions)):
        parser.error('Request 1..100 positive event revisions')
    if before is not None and (before<1 or not 1<=args.history_limit<=100):
        parser.error('--history-before requires a positive revision and --history-limit 1..100')
    if (revisions or before is not None) and any(getattr(args,k,None) for k in MUTATIONS):
        parser.error('History and evidence requests are read-only; save the event separately')


def details(args,state):
    revisions=getattr(args,'event_revision',None);before=getattr(args,'history_before',None)
    if not revisions and before is None:return state
    if args.command=='diagnostic':
        from diagnostic_learning import load,path_for
        journal,_=load(path_for(args));revision=state.get('revision');events=journal['events']
    else:
        from unit_learning import load
        from learning_tools import learner
        journal=load(learner(args)/'unit-learning.json');revision=state['learning'].get('revision')
        events=[e for e in journal['events'] if e['unit']==args.unit_id]
    if journal['revision']!=revision:raise ValueError('Learning data changed while reading details; retry')
    result=dict(state)
    if revisions:
        index={e['revision']:e for e in events}
        if any(n not in index for n in revisions):raise ValueError('Requested event is unavailable for this unit or journal')
        result['detail_events']=[index[n] for n in dict.fromkeys(revisions)]
    if before is not None:
        available=[e for e in events if e['revision']<before];page=available[-args.history_limit:]
        more=len(available)>len(page)
        result['history_page']=dict(revision=revision,before_revision=before,
            next_before_revision=page[0]['revision'] if more else None,has_more=more)
        if args.command=='diagnostic':result['history']=page
        else:result['learning']=dict(state['learning'],history=page)
    return result


def compact(command,state,history=False):
    result=dict(state)
    if command=='settings':result=preferences(state)
    elif command=='diagnostic':
        result=omit(state,'attempts')
        if not history:result.pop('history',None)
        result['attempt_count']=len(state.get('attempts',[]))
        current=state.get('current')
        if current is not None:
            result['current']=dict(omit(current,'answers','skips'),
                answer_count=len(current['answers']),skip_count=len(current.get('skips',[])))
    else:
        if 'preferences' in result:result['preferences']=preferences(result['preferences'])
        if 'learning_intent' in result:result['learning_intent']=intent(result['learning_intent'],embedded=True)
        if 'learning_progress' in result:result['learning_progress']=progress(result['learning_progress'])
        if command=='intent':
            result=intent(state)
            if history and 'history' in state:result['history']=state['history']
        if command=='learn-unit':
            learning=state['learning'];result['learning']=omit(learning,'lesson_contexts','criterion_evidence')
            if not history:result['learning'].pop('history',None)
            result['learning']['lesson_choices']=[dict(id=row['event']['value']['id'],
                topic=row['event']['value']['topic'],kind=row['event']['value']['kind'],
                status=row['status'],valid=row['valid'],revision=row['event']['revision'],
                unit_sha256=row['event']['unit_sha256']) for row in learning.get('lesson_contexts',[])]
            needed={row['evidence_revision'] for row in learning.get('adaptive',{}).get('criteria',[])
                    if row.get('evidence_revision') is not None}
            evidence=learning.get('criterion_evidence',[])+state.get('learning_progress',{}).get('unit_history',[])+state.get('detail_events',[])
            index={e['revision']:e for e in evidence if e.get('unit')==state['unit']['id']
                   and e.get('unit_sha256')==state['unit']['content_sha256']}
            result['evidence']=[index[n] for n in sorted(needed & index.keys())]
            result['details_required']=dict(event_revisions=sorted(needed-index.keys()))
    # Remove only duplicated history from nested intent. Keep a single progress view.
    if command in ('context','intent','study-next') and 'learning_progress' not in result and 'learning_intent' in state:
        result['learning_progress']=progress(state['learning_intent']['learning_progress'])
    result['output_view']='compact_v1'
    return result


def emit(args,state):
    result=details(args,state)
    if getattr(args,'compact',False):
        result=compact(args.command,result,history=bool(getattr(args,'history_limit',0)))
        print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    else:print(json.dumps(result,ensure_ascii=False,indent=2))
