"""Evidence-bound attempts and per-session continuation for video/text learning units."""
import json
from pathlib import Path
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock
from read_snapshot import cached

LEGACY_SCHEMA='unit_learning_v1'
SCHEMA='unit_learning_v2'
STAGES={'explained','practice_with_help','solved_independently','needs_review','checked_later'}


def text(value,name,nullable=False):
    if nullable and value is None:return
    if not isinstance(value,str) or not value.strip() or len(value)>4000:raise ValueError('Invalid '+name)


def validate(value,kind):
    if kind=='assessment':
        required={'stage','details','question','answer','hints','criteria'}
        optional={'error_tags'}|({'recheck_of'} if isinstance(value,dict) and value.get('stage')=='checked_later' else set())
        if not isinstance(value,dict) or not required<=set(value) or set(value)-required-optional:
            raise ValueError('Assessment requires stage/details/question/answer/hints/criteria')
        if not isinstance(value['stage'],str) or value['stage'] not in STAGES:raise ValueError('Unknown learning stage')
        text(value['details'],'assessment details')
        text(value['question'],'question',True);text(value['answer'],'answer',True)
        if type(value['hints']) is not int or not 0<=value['hints']<=100:raise ValueError('hints must be 0..100')
        indices=value['criteria']
        if not isinstance(indices,list) or len(indices)>50 or any(type(i) is not int or not 1<=i<=50 for i in indices) or len(set(indices))!=len(indices):
            raise ValueError('criteria requires unique 1-based indices')
        if value['stage']=='explained':
            if indices or value['answer'] is not None or value['hints']:raise ValueError('Explanation cannot claim assessed criteria or learner answer')
        elif value['question'] is None or value['answer'] is None or not indices:
            raise ValueError('Practice requires actual question, answer and assessed criteria')
        if value['stage'] in ('solved_independently','checked_later') and value['hints']:
            raise ValueError('Independent solution cannot include hints')
        if value['stage']=='practice_with_help' and not value['hints']:
            raise ValueError('Assisted practice requires a recorded hint')
        from adaptive_learning import ERROR_TAGS
        tags=value.get('error_tags',[])
        if not isinstance(tags,list) or len(tags)>5 or any(not isinstance(t,str) or t not in ERROR_TAGS for t in tags) or len(set(tags))!=len(tags):raise ValueError('Invalid error tags')
        if tags and value['stage'] not in ('needs_review','practice_with_help'):raise ValueError('Error tags require observed error or assisted practice')
        if value['stage']=='checked_later':
            references=value.get('recheck_of')
            if not isinstance(references,dict) or set(references)!={str(i) for i in indices} or any(type(r) is not int or r<1 for r in references.values()):raise ValueError('Later check requires criterion -> baseline revision mapping')
    elif kind=='review_decision':
        from adaptive_learning import timestamp
        if not isinstance(value,dict) or set(value)!={'choice','criteria','until','reason'}:raise ValueError('Invalid review decision fields')
        if value['choice'] not in ('skip','defer','resume'):raise ValueError('Unknown review choice')
        indices=value['criteria']
        if not isinstance(indices,list) or not 1<=len(indices)<=50 or any(type(i) is not int or not 1<=i<=50 for i in indices) or len(set(indices))!=len(indices):raise ValueError('Review decision needs unique criteria')
        text(value['reason'],'decision reason')
        if value['choice']=='defer':timestamp(value['until'])
        elif value['until'] is not None:raise ValueError('Only defer has a deadline')
    elif kind=='checkpoint':
        if not isinstance(value,dict) or set(value)!={'recap','next_step','pending_question'}:raise ValueError('Invalid unit checkpoint fields')
        text(value['recap'],'recap');text(value['next_step'],'next_step');text(value['pending_question'],'pending_question',True)
    else:raise ValueError('Unknown unit learning event type')


@cached
def load(path,unit_id=None,compact=False,history_limit=0):
    import course
    from learning_tools import read_json
    from source_integrity import digest
    checked(path)
    data=read_json(path,dict(schema=SCHEMA,revision=0,events=[]))
    from learning_segments import event_stream
    schema,revision,events=event_stream(path,data)
    if schema not in (LEGACY_SCHEMA,SCHEMA) or type(revision) is not int or revision<0:
        raise ValueError('Invalid unit learning journal; preserve before repair')
    from collections import deque
    if type(history_limit) is not int or not 0<=history_limit<=100:raise ValueError('history-limit must be 0..100')
    retained=[];latest={};baselines={};count=0;counts={};history=deque(maxlen=history_limit)
    successful=deque(maxlen=history_limit);successful_count=0
    for i,event in enumerate(events,1):
        if (not isinstance(event,dict) or set(event)!={'revision','at','unit','unit_sha256','session','type','value'}
                or type(event['revision']) is not int or event['revision']!=i):raise ValueError('Invalid unit learning history')
        course.slug(event['unit']);digest(event['unit_sha256']);text(event['at'],'event timestamp')
        if event['session'] is not None:course.slug(event['session'])
        if event['type']=='checkpoint' and event['session'] is None:raise ValueError('Checkpoint session missing')
        validate(event['value'],event['type'])
        if schema==LEGACY_SCHEMA and (event['type']=='review_decision' or (event['type']=='assessment' and set(event['value'])!={'stage','details','question','answer','hints','criteria'} ) or (event['type']=='assessment' and event['value']['stage']=='checked_later')):raise ValueError('New events require unit_learning_v2')
        from adaptive_learning import validate_recheck,validate_event_time
        if event['type']=='assessment' and event['value']['stage']=='checked_later':
            previous={e['revision']:e for criterion in event['value']['criteria'] if (e:=baselines.get((event['unit'],event['unit_sha256'],criterion))) is not None}
            validate_recheck(event,sorted(previous.values(),key=lambda e:e['revision']))
        validate_event_time(event)
        count=i
        if event['type']=='assessment':
            for criterion in event['value']['criteria']:baselines[(event['unit'],event['unit_sha256'],criterion)]=event
        if unit_id is not None and event['unit']!=unit_id:continue
        if compact:history.append(event)
        if compact and unit_id is None and event['type']=='assessment' and event['value']['stage'] in ('solved_independently','checked_later'):
            successful.append(event);successful_count+=1
        if unit_id is not None and compact:
            if event['type']=='assessment':
                key=event['unit_sha256'];entry=counts.setdefault(key,dict(attempts=0,explanations=0))
                entry['explanations' if event['value']['stage']=='explained' else 'attempts']+=1
        if not compact:retained.append(event);continue
        prefix=(event['unit'],event['unit_sha256'],event['type'])
        if event['type']=='checkpoint':latest[(event['unit'],'checkpoint',event['session'])]=event
        else:
            for criterion in event['value'].get('criteria',[]) or ['explained']:latest[prefix+(criterion,)]=event
    if count!=revision:raise ValueError('Invalid unit learning event count')
    if compact:retained=sorted({e['revision']:e for e in latest.values()}.values(),key=lambda e:e['revision'])
    result=dict(schema=schema,revision=revision,events=retained)
    if compact and unit_id is not None:result.update(_counts=counts,_history=list(history))
    elif compact and history_limit:
        result.update(_history=list(history),_completed_tasks=list(successful),_completed_task_count=successful_count)
    return result


def view(args,unit,data):
    session=getattr(args,'session',None)
    events=[e for e in data['events'] if e['unit']==unit['id']]
    assessments=[e for e in events if e['type']=='assessment']
    current=[e for e in assessments if e['unit_sha256']==unit['content_sha256'] and not unit['stale']]
    attempts=[e for e in assessments if e['value']['stage']!='explained']
    current_attempts=[e for e in current if e['value']['stage']!='explained']
    criteria={str(i):'not_assessed' for i in range(1,len(unit['criteria'])+1)}
    for event in current:
        for index in event['value']['criteria']:
            if str(index) in criteria:criteria[str(index)]=event['value']['stage']
    checkpoint=next((e for e in reversed(events) if e['type']=='checkpoint' and session is not None and e['session']==session),None)
    valid=bool(checkpoint and not unit['stale'] and checkpoint['unit_sha256']==unit['content_sha256'])
    limit=getattr(args,'history_limit',0)
    if '_counts' in data:
        attempt_count=sum(c['attempts'] for c in data['_counts'].values())
        explanation_count=sum(c['explanations'] for c in data['_counts'].values())
        current_count=data['_counts'].get(unit['content_sha256'],{}).get('attempts',0) if not unit['stale'] else 0
    else:attempt_count=len(attempts);explanation_count=len(assessments)-len(attempts);current_count=len(current_attempts)
    return dict(revision=data['revision'],criteria=criteria,attempt_count=attempt_count,explanation_count=explanation_count,
                current_attempt_count=current_count,outdated_attempt_count=attempt_count-current_count,
                all_criteria_demonstrated=all(v in ('solved_independently','checked_later') for v in criteria.values()),
                checkpoint=dict(event=checkpoint,valid=valid,reason=None if valid else ('no_checkpoint' if checkpoint is None else 'unit_or_sources_changed')),
                history=data.get('_history',events)[-limit:] if limit else [],
                verification='assistant_recorded_assessment_not_automatic_grading')


def topic_summary(unit,learning):
    """Read-only closing facts and optional choices, never a completion/mastery event."""
    rows=learning['adaptive']['criteria']
    demonstrated=[r['criterion'] for r in rows if r['state'] in ('solved_independently','checked_later') and r['time_valid']]
    pending=[int(i) for i in learning['criteria'] if int(i) not in demonstrated]
    if unit['stale']:outcome='review_sources'
    elif not learning['evidence_time_valid']:outcome='review_evidence_time'
    elif learning['assessment_and_time_verified']:outcome='all_criteria_demonstrated'
    elif not learning['current_attempt_count']:outcome='not_assessed'
    elif any(r['state'] in ('practice_with_help','needs_review') for r in rows):outcome='needs_practice'
    else:outcome='partially_demonstrated'
    return dict(outcome=outcome,demonstrated_criteria=demonstrated,unverified_criteria=pending,
                current_attempt_count=learning['current_attempt_count'],outdated_attempt_count=learning['outdated_attempt_count'],
                total_explanation_count=learning['explanation_count'],
                next_choices=['practice','repeat_topic','choose_next_topic','pause'],
                suggested_choice='repeat_topic' if unit['stale'] else ('choose_next_topic' if learning['assessment_and_time_verified'] else 'practice'),
                continuation=learning['checkpoint'],
                verification='recorded_evidence_not_mastery_or_automatic_completion')


def learn_unit(args):
    import course
    import study_units
    from learning_tools import learner,read_json,preferences
    from learning_intent import view as intent_view
    root=course.root(args);folder=learner(args)
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Unit learning must stay outside plugin')
    course.slug(args.unit_id)
    session=getattr(args,'session',None)
    if session is not None:course.slug(session)
    limit=getattr(args,'history_limit',0)
    if type(limit) is not int or not 0<=limit<=100:raise ValueError('history-limit must be 0..100')
    assessment=getattr(args,'event_file',None);checkpoint=getattr(args,'checkpoint_file',None);decision=getattr(args,'review_file',None)
    if sum(bool(v) for v in (assessment,checkpoint,decision))>1:raise ValueError('Choose assessment, checkpoint or review decision')
    path=folder/'unit-learning.json'
    prefs=preferences(args)
    def selected():
        data=study_units.load(root,include_history=False)
        if args.unit_id not in data['units']:raise ValueError('Unknown study unit')
        return study_units.checked_units(root,data,[args.unit_id])[args.unit_id]
    incoming=assessment or checkpoint or decision
    if incoming:
        kind='assessment' if assessment else ('checkpoint' if checkpoint else 'review_decision')
        value=read_json(incoming);validate(value,kind)
        if kind=='checkpoint' and session is None:raise ValueError('Unit checkpoint requires --session ID')
        expected=getattr(args,'expected_revision',None)
        if type(expected) is not int or expected<0:raise ValueError('Unit learning write requires --expected-revision')
        from source_integrity import digest
        digest(getattr(args,'expected_unit_sha256',None))
        root_lock=root/'.study-units.lock';checked(root_lock)
        with file_lock(root_lock):
            checked(root_lock)
            unit=selected()
            if unit['stale'] or unit['content_sha256']!=args.expected_unit_sha256:raise ValueError('Unit or source changed; review before recording')
            if kind in ('assessment','review_decision') and any(i>len(unit['criteria']) for i in value['criteria']):raise ValueError('Unknown criterion index')
            for directory in (folder.parent,folder):directory.mkdir(exist_ok=True);checked(directory,True)
            lock=folder/'unit-learning.lock';checked(lock)
            with file_lock(lock):
                checked(lock);journal=load(path,compact=True)
                if journal['revision']!=expected:raise ValueError('Unit learning changed in another chat; read before writing')
                event=dict(revision=expected+1,at=course.now(),unit=args.unit_id,unit_sha256=unit['content_sha256'],
                           session=session,type=kind,value=value)
                from adaptive_learning import validate_recheck,validate_event_time
                if kind=='assessment' and value['stage']=='checked_later':validate_recheck(event,journal['events'],strict=True)
                validate_event_time(event)
                from learning_segments import append
                append(path,event,expected)
    unit=selected()
    if unit['stale']:unit['source_contexts']=[]
    # Do not present study_units' legacy placeholder as learner-specific status.
    unit.pop('mastery',None)
    try:
        journal=load(path,unit_id=args.unit_id,compact=True,history_limit=limit);learning=view(args,unit,journal);learning['valid']=True
        from adaptive_learning import recommendations
        learning['adaptive']=recommendations(unit,journal,course.now())
        learning['evidence_time_valid']=all(r['time_valid'] for r in learning['adaptive']['criteria'])
        learning['assessment_and_time_verified']=learning['all_criteria_demonstrated'] and learning['evidence_time_valid']
        learning['topic_summary']=topic_summary(unit,learning)
    except (ValueError,OSError) as error:
        learning=dict(valid=False,revision=None,reason=str(error),recovery='Preserve and repair unit-learning.json before writing')
    intent=intent_view(args)
    return dict(schema=SCHEMA,unit=unit,profile=getattr(args,'profile','default'),session=session,
                preferences=prefs,learning_intent=intent,learning_progress=intent['learning_progress'],learning=learning)
