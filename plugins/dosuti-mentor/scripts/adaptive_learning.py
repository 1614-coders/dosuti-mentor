"""Explainable review suggestions; journal evidence is not automatic pedagogical proof."""
from datetime import datetime,timezone,timedelta
import re
import unicodedata
from copy import deepcopy

MIN_RECHECK_SECONDS=86400
INITIAL_REVIEW_SECONDS=86400
LATER_REVIEW_SECONDS=7*86400
ERROR_TAGS={'concept','application','syntax','reasoning','testing','attention','unknown'}
SUCCESS={'solved_independently','checked_later'}


def timestamp(value):
    if not isinstance(value,str):raise ValueError('Timestamp requires timezone-aware ISO8601')
    try:result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ValueError('Invalid ISO8601 timestamp')
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError('Timestamp must include timezone')
    try:return result.astimezone(timezone.utc)
    except (OverflowError,ValueError):raise ValueError('Timestamp outside supported UTC range')


def fresh_question(value):
    # Ignore terminal prose punctuation; this is not semantic novelty verification.
    value=unicodedata.normalize('NFKC',value).strip()
    return re.sub(r'\s+',' ',value).casefold().rstrip(' .!?…؟！？」')


def validate_recheck(event,earlier,strict=False):
    value=event['value'];references=value['recheck_of']
    for criterion in value['criteria']:
        baseline=next((e for e in reversed(earlier) if e['unit']==event['unit'] and e['unit_sha256']==event['unit_sha256']
                       and e['type']=='assessment' and criterion in e['value']['criteria']),None)
        if (baseline is None or baseline['revision']!=references[str(criterion)] or baseline['value']['stage'] not in SUCCESS):
            raise ValueError('Later check must reference the latest independent evidence for each criterion')
        if (timestamp(event['at'])-timestamp(baseline['at'])).total_seconds()<MIN_RECHECK_SECONDS:
            raise ValueError('Later check requires at least 24 elapsed hours')
        normalize=fresh_question if strict else lambda text:re.sub(r'\s+',' ',text.strip()).casefold()
        if normalize(value['question'])==normalize(baseline['value']['question']):
            raise ValueError('Later check requires a fresh question, not the same wording')


def validate_event_time(event):
    if event['type']=='review_decision' and event['value']['choice']=='defer':
        delay=(timestamp(event['value']['until'])-timestamp(event['at'])).total_seconds()
        if not 0<delay<=90*86400:raise ValueError('Defer deadline must be after event time and within 90 days')


def recommendations(unit,data,now):
    if unit['stale']:return dict(valid=True,blocked=True,reason='review_sources',criteria=[],evidence_events={},next_actions=[],later_verified_criteria=[],policy=policy())
    assessments={};decisions={};explained=False
    for event in data['events']:
        if event['unit']!=unit['id'] or event['unit_sha256']!=unit['content_sha256']:continue
        kind=event['type']
        if kind=='assessment':
            explained|=event['value']['stage']=='explained'
            for index in event['value']['criteria']:assessments[index]=event
        elif kind=='review_decision':
            for index in event['value']['criteria']:decisions[index]=event
    at=timestamp(now);rows=[];evidence={}
    for index,title in enumerate(unit['criteria'],1):
        latest=assessments.get(index)
        if latest:evidence[str(latest['revision'])]=deepcopy(latest)
        stage=latest['value']['stage'] if latest else 'not_assessed';due=None;future=False;time_valid=True
        if latest:
            try:
                moment=timestamp(latest['at']);future=moment>at
                # Dates too close to datetime limits cannot support the review schedule.
                moment+timedelta(seconds=LATER_REVIEW_SECONDS)
                time_valid=not future
            except (ValueError,OverflowError):time_valid=False
        if stage=='needs_review':action,priority='review_error',0
        elif stage=='practice_with_help':action,priority='fresh_independent_task',1
        elif stage in SUCCESS:
            try:
                due=timestamp(latest['at'])+timedelta(seconds=LATER_REVIEW_SECONDS if stage=='checked_later' else INITIAL_REVIEW_SECONDS)
                future=timestamp(latest['at'])>at
                action,priority=('recheck_due',2) if at>=due else ('continue_or_wait',5)
            except (ValueError,OverflowError):action,priority='review_evidence_time',3
        else:action,priority=('check_understanding' if explained else 'initial_check'),4
        decision=decisions.get(index)
        suppressed=None
        if decision and (latest is None or decision['revision']>latest['revision']):
            choice=decision['value']['choice']
            if choice=='skip':suppressed='user_skipped'
            elif choice=='defer' and at<timestamp(decision['value']['until']):suppressed='user_deferred'
        row=dict(criterion=index,title=title,state=stage,action=action,priority=priority,
                 evidence_revision=latest['revision'] if latest else None,evidence_at=latest['at'] if latest else None,
                 error_tags=latest['value'].get('error_tags',[]) if latest else [],
                 due_at=due.isoformat() if due else None,future_timestamp=future,time_valid=time_valid,suppressed=suppressed,
                 decision=decision if decision and (latest is None or decision['revision']>latest['revision']) else None)
        if not time_valid:row.update(action='review_evidence_time',priority=3)
        rows.append(row)
    actions=sorted([r for r in rows if not r['suppressed'] and r['action']!='continue_or_wait'],key=lambda r:(r['priority'],r['criterion']))
    return dict(valid=True,blocked=False,criteria=rows,evidence_events=evidence,next_actions=actions,
                later_verified_criteria=[r['criterion'] for r in rows if r['state']=='checked_later' and r['time_valid']],policy=policy())


def policy():return dict(min_recheck_hours=24,first_review_days=1,later_review_days=7,
                        verification='configurable_in_code_heuristic_not_scientifically_validated_for_this_learner',
                        limitations=['Suggestions do not change grades or prove long-term retention','Question novelty and correctness assessed by assistant; wording check alone is insufficient','No background timer or notifications'])


def confirmed_scope_advice(row):
    if row['action'] not in ('initial_check', 'check_understanding'): return row
    return dict(row, action='check_unverified_scope',
        reason='Diagnostic confirms observed criteria/difficulty only; check uncovered unit scope instead of default reteaching')


def roadmap_recommendations(intent,units,journal,now):
    """Explain existing route targets using the same criterion recommendation policy."""
    result=dict(status='no_accepted_plan',steps=[],recommendations=[],review_reasons=[],
                uncovered_diagnostic_gaps=[],verification='assistant_mapped_targets_not_semantic_proof')
    progress=intent.get('learning_progress',{})
    if not intent.get('valid') or not progress.get('valid'):
        result.update(status='incomplete',review_reasons=['Read and repair incomplete learning evidence']);return result
    diagnosis=progress['diagnostic']
    cards={row['competence']:row for row in diagnosis.get('competence_map',[])}
    result['proposal_guidance']=dict(goal=deepcopy(intent.get('goal')),diagnostic_revision=diagnosis['revision'],
        recommendations=deepcopy(diagnosis.get('recommendations',[])),acceptance_required=True)
    plan=intent['plan']
    if plan is None:return result
    result['adaptation_basis']=dict(base_plan_revision=plan['accepted_revision'],
        diagnostic_revision=progress['diagnostic']['revision'],unit_learning_revision=journal['revision'])
    if plan['needs_review']:
        result.update(status='needs_review',recommendations=[dict(action='review_route',reason='Accepted goal or route sources need review')]);return result
    bases=plan.get('step_bases',[{} for _ in plan['steps']])
    for basis in bases:
        for target in basis.get('targets',[]):
            unit=units.get(target['unit_id'])
            if (unit is None or unit['stale'] or unit['content_sha256']!=target['unit_sha256']
                    or any(index>len(unit['criteria']) for index in target['criteria'])):
                result.update(status='needs_review',recommendations=[dict(action='review_route',reason='Learning target changed between reads')])
                return result
    result['status']='ready' if any(basis.get('targets') for basis in bases) else 'legacy_unmapped'
    assessments={};ranked=[]
    for index,(title,basis) in enumerate(zip(plan['steps'],bases),1):
        step=dict(index=index,title=title,targets=[],blocked=index in progress['blocked_steps'],
                  completed=index in progress['completed_steps'],
                  criteria_completed=index in progress['criterion_completed_steps'])
        for target_index,target in enumerate(basis.get('targets',[])):
            name=target['unit_id']
            alignment=plan['basis_status']['target_alignments'][index-1][target_index]
            reviewed=bool(alignment.get('evidence_valid') and alignment.get('review_verdict')=='supported')
            result['review_reasons'].append('Competence alignment remains unverified: '+target['competence'])
            if name not in assessments:assessments[name]=recommendations(units[name],journal,now)
            assessment=assessments[name]
            rows=[row for row in assessment['criteria'] if row['criterion'] in target['criteria']]
            demonstrated=bool(rows) and all(row['state'] in SUCCESS and row['time_valid'] for row in rows)
            outcome=('alignment_unverified' if not reviewed else 'criteria_demonstrated' if demonstrated else 'needs_practice' if any(row['state'] in
                     ('needs_review','practice_with_help') for row in rows) else 'unverified')
            evidence=plan.get('basis_status',{}).get('diagnostic_evidence',{})
            step['targets'].append(dict(competence=target['competence'],rationale=target['rationale'],
                unit=name,unit_sha256=target['unit_sha256'],criteria=rows,outcome=outcome,alignment=alignment,
                competence_status='unverified',diagnostic_status=cards.get(target['competence'],{}).get('status'),
                diagnostic_confirmed_levels=cards.get(target['competence'],{}).get('confirmed_levels',[]),
                alignment_review=deepcopy(target.get('alignment_review')),
                diagnostic_evidence=[dict(revision=rev,evidence=evidence[str(rev)]['evidence'],
                    result=evidence[str(rev)].get('assessment',{}).get('result'))
                    for rev in target['diagnostic_event_revisions'] if str(rev) in evidence]))
            if step['blocked'] or not reviewed:continue
            for row in rows:
                if row['suppressed'] or row['action']=='continue_or_wait':continue
                if cards.get(target['competence'],{}).get('status')=='confirmed':row=confirmed_scope_advice(row)
                ranked.append(dict(step=index,step_title=title,competence=target['competence'],
                    competence_status='unverified',completion_scope='unit_criteria_only',
                    rationale=target['rationale'],unit=name,unit_sha256=target['unit_sha256'],**row))
        result['steps'].append(step)
    gaps=progress['diagnostic']['identified_gaps']
    # A cited model judgement cannot remove a diagnostic gap.
    result['uncovered_diagnostic_gaps']=list(gaps)
    if result['uncovered_diagnostic_gaps']:
        result['review_reasons'].append('Mapped criterion results do not independently verify diagnostic gaps')
    if any(not step['targets'] for step in result['steps']):
        result['review_reasons'].append('Some route steps have no explicit learning targets')
    ranked.sort(key=lambda row:(row['priority'],row['step'],row['unit'],row['criterion']))
    result['recommendations']=ranked
    if result['review_reasons']:
        result['recommendations'].append(dict(action='review_route',reason='; '.join(result['review_reasons'])))
    return result


def incomplete_return(reason):
    return dict(valid=False,status='incomplete',reason=reason,stopped_at=None,actions=[])


def returning_guidance(course_data,profile,progress,units,states,suggestions):
    """Small read-only projection of this course/profile's already checked snapshot."""
    if not progress['valid']:return incomplete_return(progress.get('reason','Incomplete learning data'))
    points=[]
    for row in progress['continuations']:
        if not row['valid']:continue
        context=row.get('context',{});checkpoint=row.get('checkpoint') or row.get('state') or {}
        unit=units.get(row.get('unit'),{})
        points.append(dict(kind=row['kind'],unit=row.get('unit'),lesson_id=row.get('lesson_id'),
            lesson=row.get('state',{}).get('lesson'),session=row.get('session'),status=row.get('status'),
            topic=context.get('topic') or unit.get('title') or checkpoint.get('topic'),
            goal=context.get('goal'),recap=checkpoint.get('recap'),next_step=checkpoint.get('next_step'),
            pending_question=checkpoint.get('pending_question')))
    confirmed=[];unverified=[];tasks=[]
    unfinished={r.get('lesson_id') for r in points if r.get('lesson_id')}
    for name,state in states.items():
        for row in state['criteria']:
            reference=dict(unit=name,criterion=row['criterion'],evidence_revision=row['evidence_revision'])
            if row['state'] in SUCCESS and row['time_valid']:confirmed.append(reference)
            elif row['state'] in ('not_assessed','explained'):unverified.append(reference)
        for task in state['practice']:
            if not task['valid'] or (task.get('lesson_id') and task['lesson_id'] not in unfinished):continue
            if task['last_attempt'] and task['last_attempt']['stage'] in SUCCESS:continue
            tasks.append(dict(unit=name,task_id=task['id'],lesson_id=task.get('lesson_id'),criteria=task['criteria'],
                help_count=len(task['help']),solution_shown=task['solution_shown'],next_action=task['next_action']))
    diagnosis=progress['diagnostic']
    knowledge=dict(confirmed_criteria=confirmed,unverified_criteria=unverified,gaps=progress['unit_gaps'],
        diagnostic={key:diagnosis.get(key,[]) for key in ('confirmed_knowledge','identified_gaps','unverified_areas')})
    actions=[]
    def offer(action,reason,**target):
        if len(actions)<3:actions.append(dict(action=action,reason=reason,requires_consent=True,**target))
    if points:
        offer('continue_lesson' if len(points)==1 else 'choose_continuation',
              'Saved unfinished lesson' if len(points)==1 else 'Several saved continuations; learner must choose')
    elif diagnosis['status'] in ('accepted','paused'):
        offer('continue_diagnostic','An agreed diagnostic is unfinished; read its saved question before continuing')
    if any(not row['valid'] for row in progress['continuations']):
        offer('review_sources','Some saved continuations cannot be verified; do not resume them automatically')
    error=next((r for r in suggestions if r['action']=='review_error'),None)
    if error:offer('review_error','Recorded criterion error',unit=error['unit'],criterion=error['criterion'],
                   evidence_revision=error['evidence_revision'])
    elif diagnosis.get('identified_gaps'):
        offer('review_error','Diagnostic identified a gap; no automatic unit alignment',competence=diagnosis['identified_gaps'][0])
    eligible={(r['unit'],r['criterion']) for r in suggestions}
    task=next((t for t in tasks if any((t['unit'],i) in eligible for i in t['criteria'])),None)
    if task:
        fresh=task['solution_shown'] or task['next_action']=='fresh_independent_task'
        offer('fresh_independent_task' if fresh else 'practice',
              'Shown assistance requires a fresh independent check' if fresh else 'An unfinished task is saved',
              unit=task['unit'],task_id=task['task_id'],lesson_id=task['lesson_id'])
    elif suggestions:
        row=suggestions[0]
        offer('fresh_independent_task' if row['action']=='fresh_independent_task' else 'practice',
              row['action'],unit=row['unit'],criterion=row['criterion'],evidence_revision=row['evidence_revision'])
    if len(actions)<2:offer('choose_topic','Choose a topic within this course; the accepted Roadmap remains unchanged')
    if len(actions)<2:offer('add_material','Optionally use a learner-provided material after explicit agreement')
    recorded=points or progress['studied_units'] or progress['studied_lessons'] or diagnosis['status'] in ('accepted','paused','completed')
    result=dict(valid=True,status='ready' if recorded else 'no_progress',
        course={key:course_data.get(key) for key in ('id','title')},profile=profile,
        stopped_at=points[0] if len(points)==1 else None,continuation_choice_required=len(points)>1,
        continuations=points,unfinished_tasks=tasks,knowledge=knowledge,actions=actions,
        limitations=progress['limitations'],truncated_fields=[])
    # Full originals/evidence remain in study-next/learn-unit; never silently drop counts.
    for key,count in (('continuations','continuation_count'),('unfinished_tasks','unfinished_task_count')):
        result[count]=len(result[key])
        if len(result[key])>5:result['truncated_fields'].append(key);result[key]=result[key][:5]
    for key in ('confirmed_criteria','unverified_criteria','gaps'):
        knowledge[key+'_count']=len(knowledge[key])
        if len(knowledge[key])>5:result['truncated_fields'].append('knowledge.'+key);knowledge[key]=knowledge[key][:5]
    return result


def study_next(args):
    from read_snapshot import snapshot, SnapshotChanged
    for _ in range(3):
        try:
            with snapshot():
                return _study_next(args)
        except SnapshotChanged:
            pass
    reason='Learning data changed during study-next; no consistent read after 3 attempts'
    result=dict(valid=False,reason=reason,returning=incomplete_return(reason),
                recommendations=[],recovery='Retry study-next after the other chat finishes saving')
    if getattr(args,'knowledge_map',False):
        from knowledge_map import incomplete
        result['knowledge_map']=incomplete(reason)
    return result


def _study_next(args):
    import course,study_units,unit_learning
    from learning_tools import learner
    from learning_intent import view as intent
    root=course.root(args);course_data=course.read(root/'course.json')
    if (not isinstance(course_data,dict) or type(course_data.get('schema_version')) is not int or course_data['schema_version']!=1
            or any(not isinstance(course_data.get(key),str) or not course_data[key].strip() for key in ('id','title'))):
        raise ValueError('Course metadata changed or is invalid; preserve data and retry after checking the course')
    folder=learner(args)
    limit=getattr(args,'limit',5)
    if type(limit) is not int or not 1<=limit<=20:raise ValueError('limit must be 1..20')
    knowledge_offset=getattr(args,'knowledge_offset',0)
    if type(knowledge_offset) is not int or knowledge_offset<0:raise ValueError('knowledge offset must be nonnegative')
    if knowledge_offset and not getattr(args,'knowledge_map',False):raise ValueError('knowledge offset requires --knowledge-map')
    session=getattr(args,'session',None)
    if session is not None:course.slug(session)
    try:
        journal=unit_learning.load(folder/'unit-learning.json',compact=True,history_limit=100)
        registry=study_units.load(root,include_history=False)
        units=study_units.checked_units(root,registry,include_contexts=False)
    except (ValueError,OSError) as error:
        result=dict(valid=False,reason=str(error),recommendations=[],returning=incomplete_return(str(error)),
                    recovery='Preserve and repair learning data before using recommendations')
        # Intake is independent of the evidence journal/registry. Keep its saved
        # questions and explicit choice visible, without trusting broken progress.
        from topic_queue import load as load_queue, view as queue_view
        try:
            queue_path,queue_state=load_queue(args,folder=folder)
            if queue_path.exists():result['topic_queue']=queue_view(args,queue_state)
        except (ValueError,OSError) as queue_error:
            # A second corrupt source must not hide the original journal error.
            result['topic_queue_error']=str(queue_error)
        if getattr(args,'knowledge_map',False):
            from knowledge_map import incomplete
            result['knowledge_map']=incomplete(str(error))
        return result
    now=course.now()
    suggestions=[];blocked=[];continuations=[];returning_states={};by_unit=unit_learning.journals_by_unit(journal)
    for unit in units.values():
        selected_journal=by_unit[unit['id']]
        result=recommendations(unit,selected_journal,now)
        if result['blocked']:blocked.append(dict(unit=unit['id'],reason='review_sources',details=unit['reasons']));continue
        view=unit_learning.view(args,unit,selected_journal)
        returning_states[unit['id']]=dict(criteria=result['criteria'],practice=view['practice'],evidence=result['evidence_events'])
        if view['checkpoint']['valid'] and 'lesson_id' not in view['checkpoint']['event']['value']:continuations.append(dict(unit=unit['id'],checkpoint=view['checkpoint']))
        for action in result['next_actions']:
            suggestions.append(dict(unit=unit['id'],unit_sha256=unit['content_sha256'],prerequisites=unit['prerequisites'],**action))
    continuations.extend(row for row in unit_learning.lesson_continuations(units,journal) if row.get('lesson_id') and row['valid'])
    suggestions.sort(key=lambda r:(r['priority'],r['unit'],r['criterion']))
    intent_view=intent(args)
    roadmap=roadmap_recommendations(intent_view,units,journal,now)
    confirmed_units={target['unit'] for step in roadmap['steps'] for target in step['targets']
                     if target.get('diagnostic_status')=='confirmed'}
    suggestions=[confirmed_scope_advice(row) if row['unit'] in confirmed_units else row for row in suggestions]
    valid=bool(intent_view.get('valid') and intent_view['learning_progress']['valid'])
    if not valid:suggestions=[]
    returning=returning_guidance(course_data,getattr(args,'profile','default'),
        intent_view['learning_progress'],units,returning_states,suggestions)
    roadmap['recommendation_count']=len(roadmap['recommendations'])
    roadmap['truncated']=len(roadmap['recommendations'])>limit
    roadmap['recommendations']=roadmap['recommendations'][:limit]
    result=dict(valid=valid,profile=getattr(args,'profile','default'),session=session,journal_revision=journal['revision'],as_of=now,learning_intent=intent_view,learning_progress=intent_view['learning_progress'],
                recommendations=suggestions[:limit],recommendation_count=len(suggestions),truncated=len(suggestions)>limit,
                blocked=blocked[:limit],blocked_count=len(blocked),continuations=continuations[:limit],continuation_count=len(continuations),
                roadmap=roadmap,returning=returning,policy=policy(),verification='read_only_suggestions_not_automatic_curriculum_or_grading')
    from topic_queue import load as load_queue, view as queue_view
    queue_path,queue_state=load_queue(args,folder=folder)
    if queue_path.exists():result['topic_queue']=queue_view(args,queue_state,units=units,journal=journal)
    if getattr(args,'knowledge_map',False):
        from knowledge_map import build
        result['knowledge_map']=build(course_data,getattr(args,'profile','default'),intent_view['learning_progress'],
            units,returning_states,returning['actions'],roadmap,limit,knowledge_offset)
    return result
