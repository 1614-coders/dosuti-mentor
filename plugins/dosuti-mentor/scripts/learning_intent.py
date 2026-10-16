"""Versioned learning goals, proposed/accepted plans and independent session tasks."""
from copy import deepcopy
import json
from pathlib import Path
from atomic_files import atomic_json
from storage_lock import file_lock

SCHEMA = 'learning_intent_v1'
CAREER_SCHEMA = 'learning_intent_v2'
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
    if not isinstance(value, dict) or not value or set(value) - {'goal','proposal','accept_proposal','session'}:
        raise ValueError('Intent update requires goal, proposal, accept_proposal or session')
    if 'goal' in value and value['goal'] is not None:
        goal = value['goal']
        if not isinstance(goal, dict) or 'description' not in goal or set(goal)-{'description','purpose'}:
            raise ValueError('Goal requires description and optional purpose')
        text(goal['description'], 'Goal description')
        if goal.get('purpose') is not None: text(goal['purpose'], 'Goal purpose')
    if 'proposal' in value and value['proposal'] is not None:
        plan=value['proposal']
        if not isinstance(plan,dict) or not {'summary','steps'}<=set(plan) or set(plan)-{'summary','steps','career'}:
            raise ValueError('Plan proposal requires summary and steps')
        text(plan['summary'],'Plan summary')
        if not isinstance(plan['steps'],list) or not 1<=len(plan['steps'])<=50:
            raise ValueError('Plan requires 1..50 steps')
        for step in plan['steps']: text(step,'Plan step',1000)
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


def apply(state, event):
    value=patch_value(event['changes'],event['session']); rev=event['revision']
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
    if (set(journal)!={'schema','revision','events'} or journal['schema'] not in (SCHEMA,CAREER_SCHEMA)
            or type(journal['revision']) is not int or journal['revision']<0
            or not isinstance(journal['events'],list) or len(journal['events'])!=journal['revision']):
        raise ValueError('Invalid learning intent journal')
    state=dict(goal=None,goal_revision=None,plan=None,proposal=None,sessions={})
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
        result=dict(value,needs_review=state['goal'] is None or value['goal_revision']!=state['goal_revision'])
        if 'career' in value:
            from career_route import status
            result['career_status']=status(args,value['career'])
            result['needs_review']=result['needs_review'] or result['career_status']['needs_review']
        return result
    plan_view=plan(state['plan']);proposal_view=plan(state['proposal'])
    task=deepcopy(state['sessions'].get(session)) if session is not None else None
    if task is not None:
        task['needs_review']=(task['goal_revision']!=state['goal_revision'] or
                              task['plan_revision']!=current_plan_revision(state) or bool(plan_view and task['plan_revision']==plan_view['accepted_revision'] and plan_view['needs_review']))
    return dict(valid=True,revision=journal['revision'],goal=state['goal'],goal_revision=state['goal_revision'],
                plan=plan_view,proposal=proposal_view,session=session,session_task=task,
                session_count=len(state['sessions']),event_count=len(journal['events']),
                history=journal['events'][-limit:] if limit else [])


def commit(args,changes,sources_locked=False):
    """One atomic intent event; caller may hold the shared source lock."""
    import course
    from profile_storage import checked
    from contextlib import ExitStack
    changes=patch_value(changes,getattr(args,'session',None))
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
            if proposal and 'career' in proposal:
                from career_route import status
                review=status(args,proposal['career'])
                if not review['valid']:raise ValueError('Career route needs review: '+'; '.join(review['reasons']))
            event=dict(revision=expected+1,at=course.now(),reason=args.reason,session=getattr(args,'session',None),changes=changes)
            apply(state,event)
            schema=CAREER_SCHEMA if journal['schema']==CAREER_SCHEMA or (proposal and 'career' in proposal) else SCHEMA
            journal=dict(schema=schema,revision=expected+1,events=journal['events']+[event])
            from document_store import write
            write(path,journal,'learning_intent',writer=atomic_json,limit=MAX_BYTES)


def intent(args):
    from learning_tools import read_json
    if getattr(args,'update_file',None):commit(args,read_json(args.update_file))
    return view(args)
