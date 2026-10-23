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
    current=[e for e in data['events'] if e['unit']==unit['id'] and e['unit_sha256']==unit['content_sha256']]
    if unit['stale']:return dict(valid=True,blocked=True,reason='review_sources',criteria=[],evidence_events={},next_actions=[],later_verified_criteria=[],policy=policy())
    at=timestamp(now);rows=[];evidence={}
    for index,title in enumerate(unit['criteria'],1):
        latest=next((e for e in reversed(current) if e['type']=='assessment' and index in e['value']['criteria']),None)
        explained=any(e['type']=='assessment' and e['value']['stage']=='explained' for e in current)
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
        decision=next((e for e in reversed(current) if e['type']=='review_decision' and index in e['value']['criteria']),None)
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


def study_next(args):
    from read_snapshot import snapshot, SnapshotChanged
    for _ in range(3):
        try:
            with snapshot():
                return _study_next(args)
        except SnapshotChanged:
            pass
    return dict(valid=False,reason='Learning data changed during study-next; no consistent read after 3 attempts',
                recommendations=[],recovery='Retry study-next after the other chat finishes saving')


def _study_next(args):
    import course,study_units,unit_learning
    from learning_tools import learner
    from learning_intent import view as intent
    root=course.root(args);course.read(root/'course.json');folder=learner(args)
    limit=getattr(args,'limit',5)
    if type(limit) is not int or not 1<=limit<=20:raise ValueError('limit must be 1..20')
    session=getattr(args,'session',None)
    if session is not None:course.slug(session)
    try:journal=unit_learning.load(folder/'unit-learning.json',compact=True,history_limit=100)
    except (ValueError,OSError) as error:return dict(valid=False,reason=str(error),recommendations=[],recovery='Preserve and repair learner journal before using recommendations')
    registry=study_units.load(root,include_history=False);units=study_units.checked_units(root,registry,include_contexts=False);now=course.now()
    suggestions=[];blocked=[];continuations=[];by_unit={}
    for event in journal['events']:by_unit.setdefault(event['unit'],[]).append(event)
    for unit in units.values():
        selected_journal=dict(journal,events=by_unit.get(unit['id'],[]))
        result=recommendations(unit,selected_journal,now)
        if result['blocked']:blocked.append(dict(unit=unit['id'],reason='review_sources',details=unit['reasons']));continue
        view=unit_learning.view(args,unit,selected_journal)
        if view['checkpoint']['valid']:continuations.append(dict(unit=unit['id'],checkpoint=view['checkpoint']))
        for action in result['next_actions']:
            suggestions.append(dict(unit=unit['id'],unit_sha256=unit['content_sha256'],prerequisites=unit['prerequisites'],**action))
    suggestions.sort(key=lambda r:(r['priority'],r['unit'],r['criterion']))
    intent_view=intent(args)
    roadmap=roadmap_recommendations(intent_view,units,journal,now)
    confirmed_units={target['unit'] for step in roadmap['steps'] for target in step['targets']
                     if target.get('diagnostic_status')=='confirmed'}
    suggestions=[confirmed_scope_advice(row) if row['unit'] in confirmed_units else row for row in suggestions]
    valid=bool(intent_view.get('valid') and intent_view['learning_progress']['valid'])
    if not valid:suggestions=[]
    roadmap['recommendation_count']=len(roadmap['recommendations'])
    roadmap['truncated']=len(roadmap['recommendations'])>limit
    roadmap['recommendations']=roadmap['recommendations'][:limit]
    return dict(valid=valid,profile=getattr(args,'profile','default'),session=session,journal_revision=journal['revision'],as_of=now,learning_intent=intent_view,learning_progress=intent_view['learning_progress'],
                recommendations=suggestions[:limit],recommendation_count=len(suggestions),truncated=len(suggestions)>limit,
                blocked=blocked[:limit],blocked_count=len(blocked),continuations=continuations[:limit],continuation_count=len(continuations),
                roadmap=roadmap,policy=policy(),verification='read_only_suggestions_not_automatic_curriculum_or_grading')
