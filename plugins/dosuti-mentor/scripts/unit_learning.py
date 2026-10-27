"""Evidence-bound attempts and per-session continuation for video/text learning units."""
import json
from pathlib import Path
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock
from read_snapshot import cached, reuse_reads

LEGACY_SCHEMA='unit_learning_v1'
SCHEMA='unit_learning_v2'
LESSON_KINDS={'new_topic','continuation','review','practice','gap_work'}
STAGES={'explained','practice_with_help','solved_independently','needs_review','checked_later'}


class LessonSelectionError(ValueError):
    """Invalid lesson selection does not mean the journal is corrupt."""


def text(value,name,nullable=False):
    if nullable and value is None:return
    if not isinstance(value,str) or not value.strip() or len(value)>4000:raise ValueError('Invalid '+name)


def validate_lesson_input(value):
    if not isinstance(value,dict) or set(value)!={'kind','topic','goal','plan'}:
        raise ValueError('Lesson context requires kind, topic, goal and plan')
    if not isinstance(value['kind'],str) or value['kind'] not in LESSON_KINDS:
        raise ValueError('Unknown lesson kind')
    text(value['topic'],'lesson topic');text(value['goal'],'lesson goal')
    plan=value['plan']
    if not isinstance(plan,list) or not 1<=len(plan)<=20:raise ValueError('Lesson plan requires 1..20 steps')
    for step in plan:text(step,'lesson plan step')
    if len(set(plan))!=len(plan):raise ValueError('Duplicate lesson plan step')


def validate_lesson_reference(journal,identity,unit,checksum):
    event=next((e for e in journal['events'] if e['type']=='lesson_context' and e['value']['id']==identity),None)
    if event is None or event['unit']!=unit or event['unit_sha256']!=checksum:
        raise ValueError('Lesson context missing or belongs to another unit version')


def checkpoint_event(event):
    if event['type']=='checkpoint':return event
    return dict(event,type='checkpoint',projection_of='lesson_state',
                value=dict(event['value']['checkpoint'],lesson_id=event['value']['lesson_id']))


def apply_lesson_state(states,event):
    """Replay lifecycle in the existing journal; False means an idempotent retry."""
    kind=event['type'];value=event['value']
    if kind=='lesson_context':
        identity=value['id']
        if identity in states or event['session'] is None or value['basis']['unit_learning_revision']!=event['revision']-1:
            raise ValueError('Duplicate or invalid lesson context event')
        states[identity]=dict(context=event,status='active',revision=event['revision'],requests={},checkpoint=None)
        return True
    if kind not in ('checkpoint','lesson_state') or 'lesson_id' not in value:return True
    state=states.get(value['lesson_id'])
    if state is None or (state['context']['unit'],state['context']['unit_sha256'])!=(event['unit'],event['unit_sha256']):
        raise ValueError('Unknown lesson context or unit version')
    action=value.get('action');requests=state['requests'];request=requests.get(value.get('request_id'))
    if kind=='lesson_state' and event['session'] is None:raise ValueError('Lesson state requires a chat session')
    if action=='request_switch' and request:
        if request['target_topic']!=value['target_topic']:raise ValueError('Switch request ID already has another target')
        return False
    if action in ('confirm_switch','cancel_switch'):
        decision='confirmed' if action=='confirm_switch' else 'cancelled'
        if request is None:raise ValueError('Unknown switch request')
        if request['decision']!='pending':
            if request['decision']==decision:return False
            raise ValueError('Switch request already resolved differently')
    if state['status']=='completed':raise ValueError('Completed lesson cannot be changed; start a new lesson')
    if kind=='checkpoint':state['checkpoint']=event
    elif action=='request_switch':
        if state['status']!='active' or any(r['decision']=='pending' for r in requests.values()):
            raise ValueError('Switch requires an active lesson without another pending request')
        requests[value['request_id']]=dict(id=value['request_id'],target_topic=value['target_topic'],
            decision='pending',revision=event['revision'])
    elif action in ('confirm_switch','cancel_switch'):
        request.update(decision=decision,revision=event['revision'])
        state['status']='paused' if action=='confirm_switch' else 'active'
    elif action=='resume':
        if state['status']!='paused':raise ValueError('Only paused lessons can resume')
        state['status']='active'
    elif action=='pause':
        if state['status']!='active':raise ValueError('Only active lessons can pause')
        state['status']='paused'
    elif action=='complete':state['status']='completed'
    if 'checkpoint' in value:state['checkpoint']=checkpoint_event(event)
    state['revision']=event['revision']
    return True


def lesson_state(state):
    request=next(reversed(state['requests'].values()),None)
    return dict(status=state['status'],revision=state['revision'],switch_request=request,
                confirmation_required=bool(request and request['decision']=='pending' and state['status']=='active'))


def checkpoint_events(data):
    states=data.get('_lesson_states',{})
    events=[e for e in data['events'] if e['type']=='checkpoint' and 'lesson_id' not in e['value']]
    events.extend(s['checkpoint'] for s in states.values() if s['checkpoint'] and s['status']!='completed')
    return sorted(events,key=lambda e:e['revision'])


def lesson_continuations(units,data):
    """One continuation per unfinished lesson; legacy points stay per chat."""
    rows=[];latest={}
    for event in checkpoint_events(data):
        if 'lesson_id' not in event['value']:latest[(event['unit'],event['session'])]=event
    for event in latest.values():
        unit=units.get(event['unit']);valid=bool(unit and not unit['stale'] and unit['content_sha256']==event['unit_sha256'])
        rows.append(dict(kind='unit',unit=event['unit'],session=event['session'],revision=event['revision'],
            checkpoint=event['value'],valid=valid,reason=None if valid else 'unit_or_sources_changed'))
    for identity,state in data.get('_lesson_states',{}).items():
        if state['status']=='completed':continue
        context=state['context'];event=state['checkpoint'] or context;unit=units.get(context['unit'])
        valid=bool(unit and not unit['stale'] and unit['content_sha256']==context['unit_sha256'])
        rows.append(dict(kind='unit',unit=context['unit'],session=event['session'],revision=state['revision'],
            lesson_id=identity,status=state['status'],lesson_state=lesson_state(state),context=context['value'],
            checkpoint=state['checkpoint']['value'] if state['checkpoint'] else None,
            valid=valid,reason=None if valid else 'unit_or_sources_changed'))
    return sorted(rows,key=lambda r:r['revision'])


def validate(value,kind):
    if kind=='practice':
        from practice_support import validate as validate_practice
        validate_practice(value)
    elif kind=='lesson_context':
        from learning_intent import patch_value
        import course
        if not isinstance(value,dict) or set(value)!={'id','kind','topic','goal','plan','basis'}:
            raise ValueError('Invalid saved lesson context')
        course.slug(value['id'])
        validate_lesson_input({k:value[k] for k in ('kind','topic','goal','plan')})
        basis=value['basis']
        if not isinstance(basis,dict) or set(basis)!={'intent_revision','goal_revision','learning_goal','plan_revision','unit_learning_revision','diagnostic_revision','criteria'}:
            raise ValueError('Invalid lesson basis')
        for name in ('intent_revision','unit_learning_revision','diagnostic_revision'):
            if type(basis[name]) is not int or basis[name]<0:raise ValueError('Invalid lesson basis revision')
        for name in ('goal_revision','plan_revision'):
            if basis[name] is not None and (type(basis[name]) is not int or basis[name]<1):
                raise ValueError('Invalid lesson goal/plan revision')
        patch_value(dict(goal=basis['learning_goal']),None)
        if not isinstance(basis['criteria'],dict) or any(k not in {str(i) for i in range(1,51)}
                or not isinstance(v,str) or v not in STAGES|{'not_assessed'} for k,v in basis['criteria'].items()):
            raise ValueError('Invalid lesson criterion snapshot')
    elif kind=='lesson_state':
        import course
        if not isinstance(value,dict) or not {'lesson_id','action'}<=set(value):raise ValueError('Invalid lesson state fields')
        fields={'pause':{'checkpoint'},'resume':set(),'complete':{'checkpoint'},
                'request_switch':{'request_id','target_topic'},'confirm_switch':{'request_id','checkpoint'},
                'cancel_switch':{'request_id'}}
        action=value['action']
        if not isinstance(action,str) or action not in fields or set(value)!={'lesson_id','action'}|fields[action]:
            raise ValueError('Invalid lesson state action fields')
        course.slug(value['lesson_id'])
        if 'request_id' in value:course.slug(value['request_id'])
        if 'target_topic' in value:text(value['target_topic'],'switch target topic')
        if 'checkpoint' in value:
            validate(value['checkpoint'],'checkpoint')
            if value['checkpoint'].get('lesson_id',value['lesson_id'])!=value['lesson_id']:raise ValueError('Conflicting lesson IDs')
    elif kind=='assessment':
        required={'stage','details','question','answer','hints','criteria'}
        optional={'error_tags','task_id'}|({'recheck_of'} if isinstance(value,dict) and value.get('stage')=='checked_later' else set())
        if not isinstance(value,dict) or not required<=set(value) or set(value)-required-optional:
            raise ValueError('Assessment requires stage/details/question/answer/hints/criteria')
        if not isinstance(value['stage'],str) or value['stage'] not in STAGES:raise ValueError('Unknown learning stage')
        text(value['details'],'assessment details')
        if 'task_id' in value:
            from course import slug
            slug(value['task_id'])
        text(value['question'],'question',True);text(value['answer'],'answer',True)
        if type(value['hints']) is not int or not 0<=value['hints']<=100:raise ValueError('hints must be 0..100')
        indices=value['criteria']
        if not isinstance(indices,list) or len(indices)>50 or any(type(i) is not int or not 1<=i<=50 for i in indices) or len(set(indices))!=len(indices):
            raise ValueError('criteria requires unique 1-based indices')
        if value['stage']=='explained':
            if 'task_id' in value:raise ValueError('Explanation cannot reference a practice task; record an actual attempt separately')
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
        if (not isinstance(value,dict) or not {'recap','next_step','pending_question'}<=set(value)
                or set(value)-{'recap','next_step','pending_question','lesson_id'}):raise ValueError('Invalid unit checkpoint fields')
        if 'lesson_id' in value:
            from course import slug
            slug(value['lesson_id'])
        text(value['recap'],'recap');text(value['next_step'],'next_step');text(value['pending_question'],'pending_question',True)
    else:raise ValueError('Unknown unit learning event type')


@cached
def _load(path,unit_id=None,compact=False,history_limit=0):
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
    successful=deque(maxlen=history_limit);successful_count=0;lessons={};tasks={};help_index={};unit_counts={};unit_history={}
    for i,event in enumerate(events,1):
        if (not isinstance(event,dict) or set(event)!={'revision','at','unit','unit_sha256','session','type','value'}
                or type(event['revision']) is not int or event['revision']!=i):raise ValueError('Invalid unit learning history')
        course.slug(event['unit']);digest(event['unit_sha256']);text(event['at'],'event timestamp')
        if event['session'] is not None:course.slug(event['session'])
        if event['type'] in ('checkpoint','practice') and event['session'] is None:raise ValueError('Checkpoint/practice session missing')
        validate(event['value'],event['type'])
        if not apply_lesson_state(lessons,event):raise ValueError('Duplicate lifecycle event in journal')
        from practice_support import replay
        replay(tasks,event,lessons,help_index)
        if schema==LEGACY_SCHEMA and (event['type'] in ('practice','review_decision','lesson_context','lesson_state') or (event['type']=='checkpoint' and 'lesson_id' in event['value']) or (event['type']=='assessment' and set(event['value'])!={'stage','details','question','answer','hints','criteria'} ) or (event['type']=='assessment' and event['value']['stage']=='checked_later')):raise ValueError('New events require unit_learning_v2')
        from adaptive_learning import validate_recheck,validate_event_time
        if event['type']=='assessment' and event['value']['stage']=='checked_later':
            previous={e['revision']:e for criterion in event['value']['criteria'] if (e:=baselines.get((event['unit'],event['unit_sha256'],criterion))) is not None}
            validate_recheck(event,sorted(previous.values(),key=lambda e:e['revision']))
        validate_event_time(event)
        count=i
        if event['type']=='assessment':
            for criterion in event['value']['criteria']:baselines[(event['unit'],event['unit_sha256'],criterion)]=event
        if compact:
            unit_history.setdefault(event['unit'],deque(maxlen=100)).append(event)
            if event['type']=='assessment':
                entry=unit_counts.setdefault(event['unit'],{}).setdefault(event['unit_sha256'],dict(attempts=0,explanations=0))
                entry['explanations' if event['value']['stage']=='explained' else 'attempts']+=1
        if unit_id is not None and event['unit']!=unit_id:continue
        if compact:history.append(event)
        if compact and unit_id is None and event['type']=='assessment' and event['value']['stage'] in ('solved_independently','checked_later'):
            successful.append(event);successful_count+=1
        if unit_id is not None and compact:
            if event['type']=='assessment':
                key=event['unit_sha256'];entry=counts.setdefault(key,dict(attempts=0,explanations=0))
                entry['explanations' if event['value']['stage']=='explained' else 'attempts']+=1
        if not compact:retained.append(event);continue
        if event['type']=='practice':continue  # Full provenance is available in the replay projection.
        prefix=(event['unit'],event['unit_sha256'],event['type'])
        if event['type']=='checkpoint':latest[(event['unit'],'checkpoint',event['session'],event['value'].get('lesson_id'))]=event
        elif event['type']=='lesson_context':latest[(event['unit'],'lesson_context',event['value']['id'])]=event
        elif event['type']=='lesson_state':latest[(event['unit'],'lesson_state',event['value']['lesson_id'])]=event
        else:
            for criterion in event['value'].get('criteria',[]) or ['explained']:latest[prefix+(criterion,)]=event
    if count!=revision:raise ValueError('Invalid unit learning event count')
    if compact:retained=sorted({e['revision']:e for e in latest.values()}.values(),key=lambda e:e['revision'])
    result=dict(schema=schema,revision=revision,events=retained)
    if tasks:result['_practice_tasks']=tasks
    if lessons:result['_lesson_states']=lessons
    if compact and unit_id is not None:result.update(_counts=counts,_history=list(history))
    elif compact and history_limit:
        result.update(_history=list(history),_completed_tasks=list(successful),_completed_task_count=successful_count)
    if compact:result.update(_unit_counts=unit_counts,_unit_history=unit_history)
    return result


def load(path,unit_id=None,compact=False,history_limit=0):
    if type(history_limit) is not int or not 0<=history_limit<=100:raise ValueError('history-limit must be 0..100')
    if not compact:return _load(path,unit_id,compact,history_limit)
    # One validated replay supplies all compact projections in this operation.
    data=dict(_load(path,None,True,100))
    counts=data.pop('_unit_counts');histories=data.pop('_unit_history')
    if unit_id is not None:
        data['events']=[e for e in data['events'] if e['unit']==unit_id]
        data['_counts']=counts.get(unit_id,{})
        data['_history']=list(histories.get(unit_id,()))[-history_limit:] if history_limit else []
        data.pop('_completed_tasks',None);data.pop('_completed_task_count',None)
    elif history_limit:
        data['_history']=data['_history'][-history_limit:]
        data['_completed_tasks']=data['_completed_tasks'][-history_limit:]
    else:
        for key in ('_history','_completed_tasks','_completed_task_count'):data.pop(key,None)
    return data


def journals_by_unit(data):
    """Group one validated journal for read-only projections; never cache across reads."""
    from collections import defaultdict
    grouped=defaultdict(lambda:dict(data,events=[],_practice_tasks={},_lesson_states={}))
    for event in data['events']:grouped[event['unit']]['events'].append(event)
    for key,task in data.get('_practice_tasks',{}).items():
        grouped[key[0]]['_practice_tasks'][key]=task
    for identity,state in data.get('_lesson_states',{}).items():
        grouped[state['context']['unit']]['_lesson_states'][identity]=state
    return grouped


def view(args,unit,data,*,lesson_id=None):
    from practice_support import view as practice_view
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
    checkpoint=next((e for e in reversed(checkpoint_events(data)) if e['unit']==unit['id'] and session is not None and e['session']==session),None)
    valid=bool(checkpoint and not unit['stale'] and checkpoint['unit_sha256']==unit['content_sha256'])
    contexts=[e for e in events if e['type']=='lesson_context']
    identity=lesson_id
    context=next((e for e in contexts if e['value']['id']==identity),None)
    if identity is not None and context is None:raise LessonSelectionError('Unknown lesson ID for this unit')
    def context_state(event):
        valid=not unit['stale'] and event['unit_sha256']==unit['content_sha256']
        result=dict(event=event,valid=valid,reason=None if valid else 'unit_or_sources_changed')
        if event['type']=='lesson_context':result['status']=data['_lesson_states'][event['value']['id']]['status']
        return result
    selected_state=data.get('_lesson_states',{}).get(identity)
    lesson_checkpoint=selected_state['checkpoint'] if selected_state else None
    limit=getattr(args,'history_limit',0)
    if '_counts' in data:
        attempt_count=sum(c['attempts'] for c in data['_counts'].values())
        explanation_count=sum(c['explanations'] for c in data['_counts'].values())
        current_count=data['_counts'].get(unit['content_sha256'],{}).get('attempts',0) if not unit['stale'] else 0
    else:attempt_count=len(attempts);explanation_count=len(assessments)-len(attempts);current_count=len(current_attempts)
    return dict(revision=data['revision'],lesson_state=lesson_state(selected_state) if selected_state else None,lesson_context=context_state(context) if context else None,
                lesson_contexts=[context_state(e) for e in contexts],
                lesson_checkpoint=context_state(lesson_checkpoint) if lesson_checkpoint else None,
                practice=practice_view(data.get('_practice_tasks',{}),unit,lesson_id),
                criteria=criteria,attempt_count=attempt_count,explanation_count=explanation_count,
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


@reuse_reads
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
    context_file=getattr(args,'lesson_context_file',None);lesson_id=getattr(args,'lesson_id',None)
    state_file=getattr(args,'lesson_state_file',None)
    practice_file=getattr(args,'practice_file',None)
    if state_file and lesson_id is None:raise ValueError('Lesson state requires --lesson-id')
    if lesson_id is not None:course.slug(lesson_id)
    if context_file and lesson_id is not None:raise ValueError('Create a new context or select an existing lesson ID')
    if sum(bool(v) for v in (assessment,checkpoint,decision,context_file,state_file,practice_file))>1:raise ValueError('Choose one unit learning event')
    path=folder/'unit-learning.json'
    prefs=preferences(args)
    def selected():
        data=study_units.load(root,include_history=False)
        if args.unit_id not in data['units']:raise ValueError('Unknown study unit')
        return study_units.checked_units(root,data,[args.unit_id])[args.unit_id]
    incoming=assessment or checkpoint or decision or context_file or state_file or practice_file
    if incoming:
        kind='assessment' if assessment else ('checkpoint' if checkpoint else 'review_decision' if decision else 'lesson_context' if context_file else 'lesson_state' if state_file else 'practice')
        value=read_json(incoming)
        if kind=='practice' and isinstance(value,dict) and value.get('action')=='task' and lesson_id is not None:
            if value.get('lesson_id',lesson_id)!=lesson_id:raise ValueError('Conflicting lesson IDs')
            value=dict(value,lesson_id=lesson_id)
        if kind=='lesson_context':
            validate_lesson_input(value)
            if session is None:raise ValueError('New lesson requires --session ID')
        else:
            if kind=='lesson_state':
                if 'lesson_id' in value and value['lesson_id']!=lesson_id:raise ValueError('Conflicting lesson IDs')
                value=dict(value,lesson_id=lesson_id)
            if kind=='checkpoint' and lesson_id is not None:
                if 'lesson_id' in value and value['lesson_id']!=lesson_id:raise ValueError('Conflicting lesson IDs')
                value=dict(value,lesson_id=lesson_id)
            validate(value,kind)
        if kind in ('checkpoint','lesson_state','practice') and session is None:raise ValueError('Checkpoint/lifecycle/practice requires --session ID')
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
            if kind=='practice' and value['action']=='task' and any(i>len(unit['criteria']) for i in value['criteria']):raise ValueError('Unknown criterion index')
            for directory in (folder.parent,folder):directory.mkdir(exist_ok=True);checked(directory,True)
            lock=folder/'unit-learning.lock';checked(lock)
            with file_lock(lock):
                checked(lock);journal=load(path,compact=True)
                if journal['revision']!=expected:raise ValueError('Unit learning changed in another chat; read before writing')
                if lesson_id is not None:
                    validate_lesson_reference(journal,lesson_id,args.unit_id,unit['content_sha256'])
                    if kind in ('assessment','review_decision','practice') and journal['_lesson_states'][lesson_id]['status']=='completed':
                        raise ValueError('Completed lesson cannot receive new results; start a new lesson')
                if kind=='checkpoint' and 'lesson_id' in value:
                    validate_lesson_reference(journal,value['lesson_id'],args.unit_id,unit['content_sha256'])
                if kind=='lesson_context':
                    from uuid import uuid4
                    current_intent=intent_view(args)
                    if not current_intent['valid']:raise ValueError('Repair learning intent before creating a lesson context')
                    lesson_id='lesson-'+uuid4().hex
                    if lesson_id in (args.unit_id,session) or any(e['type']=='lesson_context' and e['value']['id']==lesson_id for e in journal['events']):
                        raise ValueError('Lesson ID collision; retry creation')
                    basis=dict(intent_revision=current_intent['revision'],goal_revision=current_intent['goal_revision'],
                        learning_goal=current_intent['goal'],plan_revision=(current_intent['plan'] or {}).get('accepted_revision'),
                        unit_learning_revision=expected,diagnostic_revision=current_intent['learning_progress']['diagnostic']['revision'],
                        criteria=view(args,unit,journal)['criteria'])
                    value=dict(value,id=lesson_id,basis=basis);validate(value,kind)
                event=dict(revision=expected+1,at=course.now(),unit=args.unit_id,unit_sha256=unit['content_sha256'],
                           session=session,type=kind,value=value)
                from adaptive_learning import validate_recheck,validate_event_time
                if kind=='assessment' and value['stage']=='checked_later':validate_recheck(event,journal['events'],strict=True)
                validate_event_time(event)
                from learning_segments import append
                from copy import deepcopy
                from practice_support import validate_replay
                if lesson_id is not None and 'task_id' in value:
                    task=journal.get('_practice_tasks',{}).get((args.unit_id,unit['content_sha256'],value['task_id']))
                    if task and task.get('lesson_id')!=lesson_id:raise ValueError('Practice task belongs to another lesson')
                validate_replay(journal.get('_practice_tasks',{}),event,journal.get('_lesson_states',{}))
                trial=dict(journal.get('_lesson_states',{}))
                if lesson_id in trial:trial[lesson_id]=deepcopy(trial[lesson_id])
                if apply_lesson_state(trial,event):append(path,event,expected)
    unit=selected()
    if unit['stale']:unit['source_contexts']=[]
    # Do not present study_units' legacy placeholder as learner-specific status.
    unit.pop('mastery',None)
    try:
        journal=load(path,unit_id=args.unit_id,compact=True,history_limit=limit);learning=view(args,unit,journal,lesson_id=lesson_id);learning['valid']=True
        from adaptive_learning import recommendations
        learning['adaptive']=recommendations(unit,journal,course.now())
        learning['evidence_time_valid']=all(r['time_valid'] for r in learning['adaptive']['criteria'])
        learning['assessment_and_time_verified']=learning['all_criteria_demonstrated'] and learning['evidence_time_valid']
        learning['topic_summary']=topic_summary(unit,learning)
        if getattr(args,'compact',False):
            needed={row['evidence_revision'] for row in learning['adaptive']['criteria'] if row['evidence_revision'] is not None}
            learning['criterion_evidence']=[event for event in journal['events'] if event['revision'] in needed]
    except LessonSelectionError:raise
    except (ValueError,OSError) as error:
        learning=dict(valid=False,revision=None,reason=str(error),recovery='Preserve and repair unit-learning.json before writing')
    intent=intent_view(args)
    return dict(schema=SCHEMA,unit=unit,profile=getattr(args,'profile','default'),session=session,
                preferences=prefs,learning_intent=intent,learning_progress=intent['learning_progress'],learning=learning)
