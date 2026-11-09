"""Read-only summaries of existing learner bindings; no registry, seeding or discovery."""
from pathlib import Path
from types import SimpleNamespace as NS
from collections import Counter


def summary(item,home):
    from adaptive_learning import study_next
    from learning_tools import preferences
    args=NS(course=Path(item['course_path']),profile=item['profile'],learner_home=home,
            profiles_dir=Path(item['binding']['profiles_dir']) if item['binding'].get('profiles_dir') else None,
            session=None,limit=1)
    result=dict(item,goal=None,goal_source=None,current_topic=None,learning_state='incomplete',
                summary_valid=False,study_next_required=True,continuation_count=0,continuation_choice_required=False)
    if not item['available']:
        result['learning_state']='unavailable';return result
    try:
        state=study_next(args)
        if not state['valid']:
            result['reason']=state.get('reason') or state.get('learning_progress',{}).get('reason','Incomplete learning data')
            return result
        course=state['returning']['course']
        if course['id']!=item['course_id']:
            result['reason']='Course identity changed during reading; retry before selecting'
            return result
        result['title']=course['title'][:300]
        goal=state['learning_intent'].get('goal')
        if goal:
            result.update(goal=goal['description'],goal_source='learning_intent')
        else:
            answer=preferences(args)['onboarding_answers'].get('outcome',{})
            if answer.get('status')=='answered' and answer.get('text'):
                result.update(goal=answer['text'],goal_source='onboarding_answer')
        returning=state['returning'];progress=state['learning_progress'];point=returning['stopped_at']
        status=progress['diagnostic']['status']
        learning_state=('in_progress' if returning['continuation_count'] else
            'diagnostic_'+status if status in ('accepted','paused') else
            'recorded_results' if progress['studied_units'] or progress['studied_lessons'] else 'no_recorded_progress')
        if state['blocked_count'] or any(not r['valid'] for r in progress['continuations']):learning_state='needs_review'
        result.update(summary_valid=True,reason=None,current_topic=point['topic'] if point else None,
            learning_state=learning_state,continuation_count=returning['continuation_count'],
            diagnostic_status=status,
            continuation_choice_required=returning['continuation_choice_required'],as_of=state['as_of'],
            unit_learning_revision=state['journal_revision'],diagnostic_revision=progress['diagnostic']['revision'])
    except (ValueError,OSError,RuntimeError) as error:result['reason']=str(error)
    return result


def list_courses(home=None,project=None,profile=None,*,limit=10,offset=0):
    from learning_start import known_courses,CHOICES
    from bootstrap import default_home
    from managed_storage import safe,validate
    from course import slug
    if type(limit) is not int or not 1<=limit<=20:raise ValueError('Course list limit must be 1..20')
    if type(offset) is not int or offset<0:raise ValueError('Course list offset must be nonnegative')
    result=dict(valid=True,start_action='list_courses',profile=profile,needs_profile=profile is None,
        courses=[],total=0,offset=offset,next_offset=None,choices=list(CHOICES),errors=[],read_only=True,
        limitations=['known_bindings_only_no_cross_project_scanning','summaries_may_change_before_explicit_selection',
                     'per_course_snapshots_not_a_global_transaction'])
    if profile is None:
        result.update(valid=False,reason='Select the learner identity from the current connection; do not infer another profile')
        return result
    slug(profile)
    home=safe(home if home is not None else default_home())
    if home.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Course data must stay outside the plugin')
    if home.exists():
        try:home=validate(home)
        except (ValueError,OSError,RuntimeError) as error:
            result['errors'].append(dict(location=str(home),reason=str(error)));home=None
    else:home=None
    if project is not None:
        project=Path(project).expanduser().resolve()
        if not project.is_dir():
            result['errors'].append(dict(location=str(project),reason='Project directory unavailable'));project=None
    entries=known_courses(home,project,profile,include_unavailable=True,errors=result['errors'])
    entries.sort(key=lambda r:(not r['available'],(r['title'] or '').casefold(),r['course_path']))
    aliases=Counter(r['course_path'] for r in entries)
    rows=[dict(summary(item,home),requires_binding_choice=aliases[item['course_path']]>1)
          for item in entries[offset:offset+limit]]
    result.update(valid=not result['errors'] and all(r['summary_valid'] for r in rows),courses=rows,
        total=len(entries),next_offset=offset+limit if offset+limit<len(entries) else None,
        learner_home=str(home) if home else None)
    return result
