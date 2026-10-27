"""Replay task/help provenance in unit_learning; assessments remain authoritative."""
LEVELS=('hint','concrete','explanation','solution')


def validate(value):
    from course import slug
    from unit_learning import text
    if not isinstance(value,dict):raise ValueError('Invalid practice event')
    action=value.get('action')
    if action=='task':
        if not {'action','id','question','criteria','rationale'}<=set(value) or set(value)-{'action','id','question','criteria','rationale','lesson_id'}:
            raise ValueError('Invalid practice task fields')
        slug(value['id']);text(value['question'],'practice question');text(value['rationale'],'task rationale')
        indices=value['criteria']
        if not isinstance(indices,list) or not indices or len(indices)>50 or any(type(i) is not int or not 1<=i<=50 for i in indices) or len(set(indices))!=len(indices):
            raise ValueError('Practice task requires unique criterion indices')
        if 'lesson_id' in value:slug(value['lesson_id'])
    elif action=='help':
        if set(value)!={'action','task_id','level','content','request'}:raise ValueError('Invalid practice help fields')
        slug(value['task_id'])
        if not isinstance(value['level'],str) or value['level'] not in LEVELS:raise ValueError('Invalid practice help level')
        text(value['content'],'help content');text(value['request'],'help request',True)
        if value['level']=='solution' and value['request'] is None:raise ValueError('Full solution requires an explicit request')
    else:raise ValueError('Unknown practice action')


def replay(tasks,event,lessons,help_index=None):
    """Enforce recorded provenance, not semantic novelty or automatic grading."""
    from adaptive_learning import fresh_question
    value=event['value'];prefix=(event['unit'],event['unit_sha256']);kind=event['type']
    if kind=='practice' and value['action']=='task':
        key=prefix+(value['id'],)
        if key in tasks:raise ValueError('Duplicate practice task ID')
        if 'lesson_id' in value:
            lesson=lessons.get(value['lesson_id'])
            if lesson is None or (lesson['context']['unit'],lesson['context']['unit_sha256'])!=prefix or lesson['status']=='completed':
                raise ValueError('Practice requires an unfinished matching lesson')
        tasks[key]=dict(value,revision=event['revision'],help=[],solution_shown=False,last_attempt=None)
        return
    if kind=='practice' or (kind=='assessment' and 'task_id' in value):
        task=tasks.get(prefix+(value['task_id'],))
        if task is None:raise ValueError('Unknown practice task')
        if 'lesson_id' in task and lessons[task['lesson_id']]['status']=='completed':
            raise ValueError('Completed lesson cannot receive practice results')
        if kind=='practice':
            previous=task['help'][-1]['level'] if task['help'] else None
            if previous=='solution' and value['level']!='solution':raise ValueError('A shown solution cannot return to hint-only help')
            expected=LEVELS[min(LEVELS.index(previous)+1,2)] if previous and previous!='solution' else 'hint'
            if value['level']!='solution' and value['level']!=expected:raise ValueError('Practice help must progress gradually')
            task['help'].append(dict(level=value['level'],content=value['content'],request=value['request'],revision=event['revision']))
            task['solution_shown']|=value['level']=='solution'
            if help_index is not None:
                key=prefix+(fresh_question(task['question']),)
                help_index[key]=max(help_index.get(key,0),len(task['help']))
            return
        if fresh_question(value['question'])!=fresh_question(task['question']) or not set(value['criteria'])<=set(task['criteria']):
            raise ValueError('Assessment does not match its practice task')
        if value['stage']=='explained':raise ValueError('A practice task requires an actual attempt')
    if kind!='assessment' or value['stage']=='explained':return
    # Also prevent bypass by omitting task_id or relabelling an already shown task.
    question=fresh_question(value['question'])
    help_count=(help_index.get(prefix+(question,),0) if help_index is not None else
        max((len(t['help']) for key,t in tasks.items() if key[:2]==prefix and t['help'] and fresh_question(t['question'])==question),default=0))
    if help_count and (value['stage'] in ('solved_independently','checked_later') or value['hints']<help_count):
        raise ValueError('Recorded help requires an assisted attempt; use a fresh task for independent evidence')
    if 'task_id' in value:
        tasks[prefix+(value['task_id'],)]['last_attempt']=dict(revision=event['revision'],**value)


def validate_replay(tasks,event,lessons):
    """Trial only the task replay can mutate; leave the shared projection intact."""
    from copy import deepcopy
    trial=dict(tasks)
    value=event['value']
    if event['type']=='practice' or (event['type']=='assessment' and 'task_id' in value):
        key=(event['unit'],event['unit_sha256'],value.get('task_id'))
        if key in trial:trial[key]=deepcopy(trial[key])
    replay(trial,event,lessons)


def view(tasks,unit,lesson_id=None):
    rows=[]
    for key,task in tasks.items():
        if key[:2]!=(unit['id'],unit['content_sha256']) or (lesson_id is not None and task.get('lesson_id')!=lesson_id):continue
        attempt=task['last_attempt']
        action=('fresh_independent_task' if task['help'] else 'try_independently')
        if attempt and attempt['stage']=='needs_review':
            action='try_with_help' if task['help'] and task['help'][-1]['revision']>attempt['revision'] else 'explain_error_then_retry'
            if task['solution_shown']:action='fresh_independent_task'
        elif attempt and attempt['stage'] in ('solved_independently','checked_later'):action='choose_next_or_pause'
        elif task['help'] and attempt is None and not task['solution_shown']:action='try_with_help'
        rows.append(dict(task,next_action=action,valid=not unit['stale']))
    return sorted(rows,key=lambda r:r['revision'])
