"""Versioned learning goals, proposed/accepted plans and independent session tasks."""
from copy import deepcopy
import json
from pathlib import Path
from atomic_files import atomic_json
from storage_lock import file_lock

SCHEMA = 'learning_intent_v1'
CAREER_SCHEMA = 'learning_intent_v2'
CONTEXT_SCHEMA = 'learning_intent_v3'
BASES_SCHEMA = 'learning_intent_v4'
TARGETS_SCHEMA = 'learning_intent_v5'
MAX_BYTES = 64*1024*1024


def current_plan_revision(state):
    plan=state['plan']
    return (plan['accepted_revision'] if plan and state['goal'] is not None
            and plan['goal_revision']==state['goal_revision'] else None)


def text(value, label, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(label + ' requires nonempty text within length limit')
    return value


def patch_value(value, session):
    import course
    if not isinstance(value, dict) or not value or set(value) - {'goal','proposal','accept_proposal','session','learning_context'}:
        raise ValueError('Intent update requires goal, proposal, accept_proposal, session or learning_context')
    if 'learning_context' in value:
        context=value['learning_context']
        if (not isinstance(context,dict) or not context
                or set(context)-{'context','constraints','self_assessment'}):
            raise ValueError('Learning context requires context, constraints or self_assessment')
        for name,answer in context.items():
            if answer is not None:text(answer,'User-reported '+name)
    if 'goal' in value and value['goal'] is not None:
        goal = value['goal']
        if not isinstance(goal, dict) or 'description' not in goal or set(goal)-{'description','purpose'}:
            raise ValueError('Goal requires description and optional purpose')
        text(goal['description'], 'Goal description')
        if goal.get('purpose') is not None: text(goal['purpose'], 'Goal purpose')
    if 'proposal' in value and value['proposal'] is not None:
        plan=value['proposal']
        if not isinstance(plan,dict) or not {'summary','steps'}<=set(plan) or set(plan)-{'summary','steps','career','step_bases','adaptation_basis'}:
            raise ValueError('Plan proposal requires summary and steps')
        text(plan['summary'],'Plan summary')
        if not isinstance(plan['steps'],list) or not 1<=len(plan['steps'])<=50:
            raise ValueError('Plan requires 1..50 steps')
        for step in plan['steps']: text(step,'Plan step',1000)
        if 'step_bases' in plan:validate_bases(plan)
        if 'adaptation_basis' in plan:
            basis=plan['adaptation_basis']
            if not isinstance(basis,dict) or set(basis)!={'base_plan_revision','diagnostic_revision','unit_learning_revision'}:
                raise ValueError('Invalid adaptation basis')
            for key,revision in basis.items():
                if key=='base_plan_revision' and revision is None:continue
                if type(revision) is not int or revision<(1 if key=='base_plan_revision' else 0):
                    raise ValueError('Invalid adaptation revision')
        if 'career' in plan:
            from career_route import validate
            validate(plan['career'])
            if plan['steps']!=[step['title'] for step in plan['career']['steps']]:raise ValueError('Route labels differ from plan steps')
    if 'accept_proposal' in value:
        if value['accept_proposal'] is not True or 'goal' in value or 'proposal' in value:
            raise ValueError('Accept an existing proposal separately from goal/proposal changes')
    if 'session' in value:
        if session is None: raise ValueError('Session task requires --session ID')
        course.slug(session)
        if value['session'] is not None: text(value['session'],'Session task')
    elif session is not None:
        course.slug(session)
    return value


def validate_bases(plan):
    import course
    bases=plan['step_bases']
    if not isinstance(bases,list) or len(bases)!=len(plan['steps']):
        raise ValueError('Step bases must match plan steps')
    for index,basis in enumerate(bases,1):
        if (not isinstance(basis,dict) or not basis
                or set(basis)-{'prerequisites','diagnostic_event_revisions','unit_ids','targets'}):
            raise ValueError('Step basis requires prerequisite, diagnostic answer or unit references')
        for name,values in basis.items():
            if name=='targets':continue
            if not isinstance(values,list) or not 1<=len(values)<=50:
                raise ValueError('Step basis references require 1..50 values')
            for value in values:
                if name=='unit_ids':course.slug(value)
                elif type(value) is not int or value<1 or (name=='prerequisites' and value>=index):
                    raise ValueError('Prerequisite must precede step; diagnostic revision must be positive')
            if len(set(values))!=len(values):raise ValueError('Duplicate step basis references')
        if 'targets' in basis:validate_targets(basis['targets'],basis)


def has_targets(plan):
    return (isinstance(plan,dict) and isinstance(plan.get('step_bases',[]),list)
            and any(isinstance(basis,dict) and 'targets' in basis for basis in plan.get('step_bases',[])))


def validate_targets(targets,basis):
    from source_integrity import digest
    if not isinstance(targets,list) or not 1<=len(targets)<=50:raise ValueError('Step requires 1..50 targets')
    for target in targets:
        if not isinstance(target,dict) or set(target)!={'competence','rationale','unit_id','unit_sha256','criteria','diagnostic_event_revisions'}:
            raise ValueError('Target requires competence/rationale/unit/version/criteria/diagnostic references')
        text(target['competence'],'Target competence',1000);text(target['rationale'],'Target rationale')
        if target['unit_id'] not in basis.get('unit_ids',[]):raise ValueError('Target unit must belong to step units')
        digest(target['unit_sha256'])
        for name in ('criteria','diagnostic_event_revisions'):
            values=target[name]
            if (not isinstance(values,list) or len(values)>50 or (name=='criteria' and not values)
                    or any(type(value) is not int or value<1 for value in values) or len(set(values))!=len(values)):
                raise ValueError('Invalid target '+name)
        if any(rev not in basis.get('diagnostic_event_revisions',[]) for rev in target['diagnostic_event_revisions']):
            raise ValueError('Target diagnostic references must belong to step evidence')


def basis_status(args,plan):
    """Check cited facts only; do not infer mastery or change the route."""
    import course,study_units,diagnostic_learning
    bases=plan['step_bases'];reasons=[];answers={};units={}
    names={name for basis in bases for name in basis.get('unit_ids',[])}
    if names:
        try:
            root=course.root(args);registry=study_units.load(root,include_history=False)
            for name in sorted(names-set(registry['units'])):reasons.append('Missing study unit: '+name)
            units=study_units.checked_units(root,registry,sorted(names & set(registry['units'])),include_contexts=False)
            for name in sorted(names & set(units)):
                if units[name]['stale']:reasons.append('Study unit needs review: '+name)
        except (ValueError,OSError) as error:reasons.append('Study units: '+str(error))
    revisions={rev for basis in bases for rev in basis.get('diagnostic_event_revisions',[])}
    if revisions:
        try:
            _,attempts=diagnostic_learning.load(diagnostic_learning.path_for(args))
            answers={str(answer['revision']):answer for attempt in attempts for answer in attempt['answers']
                     if answer['revision'] in revisions}
            for rev in sorted(revisions):
                if str(rev) not in answers:reasons.append('Missing diagnostic answer event: '+str(rev))
        except (ValueError,OSError) as error:reasons.append('Diagnostic evidence: '+str(error))
    for basis in bases:
        for target in basis.get('targets',[]):
            unit=units.get(target['unit_id'])
            if unit and (target['unit_sha256']!=unit['content_sha256'] or any(i>len(unit['criteria']) for i in target['criteria'])):
                reasons.append('Target unit version or criteria changed: '+target['unit_id'])
            for revision in target['diagnostic_event_revisions']:
                answer=answers.get(str(revision))
                if answer and answer['competence']!=target['competence']:
                    reasons.append('Diagnostic competence differs from learning target: '+target['competence'])
    return dict(valid=not reasons,needs_review=bool(reasons),reasons=reasons,diagnostic_evidence=answers)


def adaptation_snapshot(args,state):
    import diagnostic_learning,unit_learning
    from learning_tools import learner
    journal,_=diagnostic_learning.load(diagnostic_learning.path_for(args))
    learning=unit_learning.load(learner(args)/'unit-learning.json',compact=True)
    return dict(base_plan_revision=state['plan']['accepted_revision'] if state['plan'] else None,
                diagnostic_revision=journal['revision'],unit_learning_revision=learning['revision'])


def adaptation_status(args,proposal,state):
    """Recheck proposal evidence, never invalidate an accepted plan after learning."""
    expected=proposal['adaptation_basis'];reasons=[]
    try:
        actual=adaptation_snapshot(args,state)
        if expected!=actual:reasons.append('Plan or learning evidence changed; reassess adaptation proposal')
    except (ValueError,OSError) as error:reasons.append(str(error))
    return dict(valid=not reasons,needs_review=bool(reasons),reasons=reasons)


def learning_progress(args, intent):
    """Read existing journals into one learner-facing route summary; never writes."""
    import course,diagnostic_learning,study_units,unit_learning
    diagnostic=diagnostic_learning.view(args)
    summary=diagnostic.get('summary') if diagnostic.get('valid') else None
    result=dict(valid=True, route_state=(intent['plan'] or {}).get('route_state','none'),
                diagnostic=dict(status=diagnostic.get('status'),revision=diagnostic.get('revision'),
                                confirmed_knowledge=(summary or {}).get('confirmed_knowledge',[]),
                                partially_confirmed_knowledge=(summary or {}).get('partially_confirmed_knowledge',[]),
                                identified_gaps=(summary or {}).get('identified_gaps',[]),
                                unverified_areas=(summary or {}).get('unverified_areas',[]),
                                recommended_start=(summary or {}).get('recommended_start')),
                studied_units=[], demonstrated_units=[], unit_gaps=[],
                studied_lessons=[],confirmed_lesson_knowledge=[],completed_lesson_tasks=[],
                completed_unit_tasks=[],lesson_history=[],unit_history=[],
                continuations=[],continuation_choice_required=False,
                completed_steps=[],blocked_steps=[],current_step=None,next_step=None,stopped_at=None,
                limitations=['recorded_evidence_not_automatic_grading'])
    if not diagnostic.get('valid'):
        result.update(valid=False, reason='diagnostic: '+diagnostic['reason'])
    plan=intent['plan']
    from learning_tools import learner,cursor
    try:
        legacy=course.read_progress(args)
        lessons={}
        from spoken_homework import resolve
        for row in legacy['progress']:
            directory=course.root(args)/'lessons'/course.slug(row['lesson'])
            if (directory/'lesson.json').exists():
                data=course.read(directory/'lesson.json')
                lessons[row['lesson']]=dict(data,homework=resolve(directory,data))
        rows=course.visible_progress(legacy['progress'],lessons)
        result['studied_lessons']=rows
        # Old lesson records do not contain criterion-bound independent answers.
        result['confirmed_lesson_knowledge']=[dict(row,verification='legacy_recorded_claim') for row in rows if row['stage'] in
            ('understood','theory_checked','practice_checked','homework_passed') and not row['invalidated']]
        result['completed_lesson_tasks']=[dict(row,verification='legacy_recorded_claim') for row in rows if row['stage'] in
            ('practice_checked','homework_passed') and not row['invalidated']]
        result.update(lesson_history=legacy['events'],lesson_event_count=legacy['event_count'],
                      lesson_history_truncated=legacy['event_count']>len(legacy['events']))
        result['limitations'].append('legacy_lesson_checks_are_recorded_claims_without_unit_criteria')
    except (ValueError,OSError) as error:
        result.update(valid=False,reason='lesson learning: '+str(error))
    # cursor.json is independent of SQLite and must survive its read failures.
    try:
        checkpoint=cursor(args)
        if checkpoint.get('reason')=='invalid_checkpoint':
            raise ValueError('invalid_checkpoint; preserve and repair cursor.json')
        if checkpoint.get('state') is not None:
            result['continuations'].append(dict(kind='lesson',session=None,scope='shared_profile',**checkpoint))
    except (ValueError,OSError) as error:
        reason='legacy cursor: '+str(error)
        result.update(valid=False,reason='; '.join(filter(None,[result.get('reason'),reason])))
    try:
        root=course.root(args);registry=study_units.load(root,include_history=False)
        units=study_units.checked_units(root,registry,include_contexts=False)
        journal=unit_learning.load(learner(args)/'unit-learning.json',compact=True,history_limit=100)
        result.update(unit_history=journal['_history'],unit_event_count=journal['revision'],
                      unit_history_truncated=journal['revision']>100)
        states={}
        for name,unit in units.items():
            view=unit_learning.view(args,unit,journal)
            from adaptive_learning import recommendations
            adaptive=recommendations(unit,journal,course.now())
            current=not unit['stale'] and view['all_criteria_demonstrated'] and all(row['time_valid'] for row in adaptive['criteria'])
            states[name]=dict(studied=bool(view['attempt_count'] or view['explanation_count']),demonstrated=current,
                              criteria=view['criteria'],checkpoint=view['checkpoint'],adaptive=adaptive['criteria'])
            if states[name]['studied']:result['studied_units'].append(name)
            if current:result['demonstrated_units'].append(name)
            for criterion,state in view['criteria'].items():
                if state in ('needs_review','practice_with_help'):
                    result['unit_gaps'].append(dict(unit=name,criterion=int(criterion),state=state))
        for event in journal['_completed_tasks']:
            unit=units.get(event['unit'])
            result['completed_unit_tasks'].append(dict(unit=event['unit'],revision=event['revision'],
                criteria=event['value']['criteria'],stage=event['value']['stage'],
                current=bool(unit and not unit['stale'] and unit['content_sha256']==event['unit_sha256'])))
        result['completed_unit_task_count']=journal['_completed_task_count']
        latest={}
        for event in journal['events']:
            if event['type']=='checkpoint':latest[(event['unit'],event['session'])]=event
        for (name,session),event in latest.items():
            unit=units.get(name)
            valid=bool(unit and not unit['stale'] and unit['content_sha256']==event['unit_sha256'])
            result['continuations'].append(dict(kind='unit',unit=name,session=session,
                revision=event['revision'],checkpoint=event['value'],valid=valid,
                reason=None if valid else 'unit_or_sources_changed'))
    except (ValueError,OSError) as error:
        reason='unit learning: '+str(error)
        result.update(valid=False,reason='; '.join(filter(None,[result.get('reason'),reason])))
        states={}
    available=[row for row in result['continuations'] if row['valid']]
    result['continuation_choice_required']=len(available)>1
    if result['valid'] and len(available)==1:result['stopped_at']=available[0]
    if not result['valid'] or plan is None or plan['needs_review']:return result
    answers={answer['revision']:answer for attempt in diagnostic.get('attempts',[])
             for answer in attempt['answers']}
    steps=[]
    for index,title in enumerate(plan['steps'],1):
        basis=(plan.get('step_bases') or [{} for _ in plan['steps']])[index-1]
        prerequisites=basis.get('prerequisites',[]);unit_ids=basis.get('unit_ids',[])
        if 'career' in plan:
            career=plan['career']['steps'][index-1]
            unit_ids=list(dict.fromkeys(unit_ids+[unit['id'] for unit in career['units']]))
            prerequisites=list(dict.fromkeys(prerequisites+[
                i for i,step in enumerate(plan['career']['steps'],1) if step['id'] in career['prerequisites']]))
        findings=[answers[revision]['evidence'] for revision in basis.get('diagnostic_event_revisions',[]) if revision in answers]
        guidance=('address_identified_gap' if 'unknown' in findings else
                  'practice_partially_confirmed' if 'needs_practice' in findings else
                  'confirm_transfer' if 'confirmed' in findings else 'no_diagnostic_evidence')
        completed=bool(unit_ids) and all(states.get(name,{}).get('demonstrated') for name in unit_ids)
        targets=basis.get('targets',[])
        if targets:
            completed=all(all(any(row['criterion']==index and row['state'] in ('solved_independently','checked_later')
                and row['time_valid'] for row in states.get(target['unit_id'],{}).get('adaptive',[]))
                for index in target['criteria']) for target in targets)
        steps.append(dict(index=index,title=title,unit_ids=unit_ids,prerequisites=prerequisites,
                          diagnostic_guidance=guidance,completed=completed))
    result['completed_steps']=[step['index'] for step in steps if step['completed']]
    for step in steps:
        if any(not steps[number-1]['completed'] or number in result['blocked_steps'] for number in step['prerequisites']):
            result['blocked_steps'].append(step['index'])
    for step in steps:
        if step['completed'] or step['index'] in result['blocked_steps']:continue
        result['current_step']=step
        result['next_step']=step
        break
    return result


def apply(state, event):
    value=patch_value(event['changes'],event['session']); rev=event['revision']
    if 'learning_context' in value:
        for name,answer in value['learning_context'].items():
            if answer is None:state['learning_context'].pop(name,None)
            else:state['learning_context'][name]=answer
    if 'goal' in value:
        goal=value['goal']
        if goal is not None: goal=dict(description=goal['description'],purpose=goal.get('purpose'))
        if goal != state['goal']:
            state.update(goal=deepcopy(goal),goal_revision=rev)
    if 'proposal' in value:
        proposal=value['proposal']
        if proposal is not None:
            if state['goal'] is None: raise ValueError('Define a goal before proposing a plan')
            proposal=dict(deepcopy(proposal),revision=rev,goal_revision=state['goal_revision'])
        state['proposal']=proposal
    if value.get('accept_proposal'):
        proposal=state['proposal']
        if proposal is None or state['goal'] is None or proposal['goal_revision']!=state['goal_revision']:
            raise ValueError('No current proposal to accept; review the goal and propose a plan')
        state['plan']=dict(deepcopy(proposal),accepted_revision=rev)
        state['proposal']=None
    if 'session' in value:
        name=event['session']
        if name not in state['sessions'] and len(state['sessions'])>=200:
            raise ValueError('Session limit reached; preserve/export learning history')
        state['sessions'][name]=dict(task=value['session'],revision=rev,
                                     goal_revision=state['goal_revision'],
                                     plan_revision=current_plan_revision(state))


def load(path):
    from learning_tools import read_json
    from profile_storage import checked
    checked(path)
    from document_store import read
    journal=read(path,dict(schema=SCHEMA,revision=0,events=[]),'learning_intent')
    if (set(journal)!={'schema','revision','events'} or journal['schema'] not in (SCHEMA,CAREER_SCHEMA,CONTEXT_SCHEMA,BASES_SCHEMA,TARGETS_SCHEMA)
            or type(journal['revision']) is not int or journal['revision']<0
            or not isinstance(journal['events'],list) or len(journal['events'])!=journal['revision']):
        raise ValueError('Invalid learning intent journal')
    state=dict(goal=None,goal_revision=None,plan=None,proposal=None,sessions={},learning_context={})
    for index,event in enumerate(journal['events'],1):
        if (not isinstance(event,dict) or set(event)!={'revision','at','reason','session','changes'}
                or type(event['revision']) is not int or event['revision']!=index):
            raise ValueError('Invalid learning intent history')
        text(event['at'],'Event timestamp',100); text(event['reason'],'Reason')
        if event['session'] is not None:
            import course
            course.slug(event['session'])
        if journal['schema']==SCHEMA and isinstance(event['changes'],dict) and isinstance(event['changes'].get('proposal'),dict) and 'career' in event['changes']['proposal']:
            raise ValueError('Career metadata requires learning_intent_v2')
        if journal['schema'] not in (CONTEXT_SCHEMA,BASES_SCHEMA,TARGETS_SCHEMA) and isinstance(event['changes'],dict) and 'learning_context' in event['changes']:
            raise ValueError('Learning context requires learning_intent_v3')
        proposal=event['changes'].get('proposal') if isinstance(event['changes'],dict) else None
        if journal['schema'] not in (BASES_SCHEMA,TARGETS_SCHEMA) and isinstance(proposal,dict) and 'step_bases' in proposal:
            raise ValueError('Step bases require learning_intent_v4')
        if journal['schema']!=TARGETS_SCHEMA and isinstance(proposal,dict) and (has_targets(proposal) or 'adaptation_basis' in proposal):
            raise ValueError('Learning targets and adaptation require learning_intent_v5')
        apply(state,event)
    return journal,state


def path_for(args):
    from learning_tools import learner
    path=learner(args)/'learning-intent.json'
    if path.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Learning intent must stay outside the plugin')
    return path


def view(args):
    import course
    path=path_for(args)
    session=getattr(args,'session',None)
    if session is not None: course.slug(session)
    limit=getattr(args,'history_limit',0)
    if type(limit) is not int or not 0<=limit<=100:
        raise ValueError('History limit must be 0..100')
    try: journal,state=load(path)
    except (ValueError,OSError) as error:
        return dict(valid=False,revision=None,reason=str(error),
                    recovery='Preserve and repair or archive learning-intent.json before writing')
    def plan(value):
        if value is None:return None
        route_state='accepted' if 'accepted_revision' in value else ('proposed' if 'step_bases' in value or 'career' in value else 'preliminary')
        result=dict(value,route_state=route_state,needs_review=state['goal'] is None or value['goal_revision']!=state['goal_revision'])
        if 'career' in value:
            from career_route import status
            result['career_status']=status(args,value['career'])
            result['needs_review']=result['needs_review'] or result['career_status']['needs_review']
        if 'step_bases' in value:
            result['basis_status']=basis_status(args,value)
            result['needs_review']=result['needs_review'] or result['basis_status']['needs_review']
        if 'accepted_revision' not in value and 'adaptation_basis' in value:
            result['adaptation_status']=adaptation_status(args,value,state)
            result['needs_review']=result['needs_review'] or result['adaptation_status']['needs_review']
        return result
    plan_view=plan(state['plan']);proposal_view=plan(state['proposal'])
    task=deepcopy(state['sessions'].get(session)) if session is not None else None
    if task is not None:
        task['needs_review']=(task['goal_revision']!=state['goal_revision'] or
                              task['plan_revision']!=current_plan_revision(state) or bool(plan_view and task['plan_revision']==plan_view['accepted_revision'] and plan_view['needs_review']))
    result=dict(valid=True,revision=journal['revision'],goal=state['goal'],goal_revision=state['goal_revision'],
                learning_context=state['learning_context'],
                plan=plan_view,proposal=proposal_view,session=session,session_task=task,
                session_count=len(state['sessions']),event_count=len(journal['events']),
                history=journal['events'][-limit:] if limit else [])
    result['learning_progress']=learning_progress(args,result)
    return result


def commit(args,changes,sources_locked=False):
    """One atomic intent event; caller may hold the shared source lock."""
    import course
    from profile_storage import checked
    from contextlib import ExitStack
    changes=deepcopy(patch_value(changes,getattr(args,'session',None)))
    text(getattr(args,'reason',None),'Reason')
    expected=getattr(args,'expected_revision',None)
    if type(expected) is not int or expected<0:raise ValueError('Intent update requires --expected-revision')
    view(args)  # Validate read options before any write.
    path=path_for(args);source_lock=course.root(args)/'.study-units.lock';checked(source_lock)
    with ExitStack() as stack:
        if not sources_locked:stack.enter_context(file_lock(source_lock))
        checked(source_lock)
        for directory in (path.parent.parent,path.parent):
            directory.mkdir(exist_ok=True);checked(directory,True)
        lock=path.with_suffix('.lock');checked(lock)
        with file_lock(lock):
            checked(lock);journal,state=load(path)
            if journal['revision']!=expected:raise ValueError('Learning intent changed in another chat; read intent again')
            proposal=changes.get('proposal')
            if changes.get('accept_proposal'):proposal=state['proposal']
            if proposal and has_targets(proposal) and state['plan'] and 'adaptation_basis' not in proposal:
                raise ValueError('Targeted route replacement requires adaptation_basis')
            if proposal and (has_targets(proposal) or 'adaptation_basis' in proposal):
                from learning_tools import learner
                for name in ('diagnostic-learning.lock','unit-learning.lock'):
                    evidence_lock=learner(args)/name;checked(evidence_lock)
                    stack.enter_context(file_lock(evidence_lock));checked(evidence_lock)
                if 'adaptation_basis' not in proposal:
                    proposal['adaptation_basis']=adaptation_snapshot(args,state)
                review=adaptation_status(args,proposal,state)
                if not review['valid']:raise ValueError('Route adaptation needs review: '+'; '.join(review['reasons']))
            if proposal and 'career' in proposal:
                from career_route import status
                review=status(args,proposal['career'])
                if not review['valid']:raise ValueError('Career route needs review: '+'; '.join(review['reasons']))
            if proposal and 'step_bases' in proposal:
                review=basis_status(args,proposal)
                if not review['valid']:raise ValueError('Plan step bases need review: '+'; '.join(review['reasons']))
            event=dict(revision=expected+1,at=course.now(),reason=args.reason,session=getattr(args,'session',None),changes=changes)
            apply(state,event)
            schema=CAREER_SCHEMA if journal['schema']==CAREER_SCHEMA or (proposal and 'career' in proposal) else SCHEMA
            if journal['schema']==CONTEXT_SCHEMA or 'learning_context' in changes:schema=CONTEXT_SCHEMA
            if journal['schema']==BASES_SCHEMA or (proposal and 'step_bases' in proposal):schema=BASES_SCHEMA
            if journal['schema']==TARGETS_SCHEMA or (proposal and (has_targets(proposal) or 'adaptation_basis' in proposal)):schema=TARGETS_SCHEMA
            journal=dict(schema=schema,revision=expected+1,events=journal['events']+[event])
            from document_store import write
            write(path,journal,'learning_intent',writer=atomic_json,limit=MAX_BYTES)


def intent(args):
    from learning_tools import read_json
    if getattr(args,'update_file',None):commit(args,read_json(args.update_file))
    return view(args)
