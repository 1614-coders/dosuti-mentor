"""On-request presentation of checked evidence; no storage, grading or route changes."""
from copy import deepcopy

SECTIONS=('confirmed','partial','gaps','unverified','unavailable','legacy')
REASONS={
    'review_error':'В сохранённом ответе обнаружена ошибка по этому критерию; сначала разберём её причину.',
    'fresh_independent_task':'Результат получен с помощью; нужна новая задача для самостоятельной проверки.',
    'initial_check':'По этому критерию ещё нет проверенного ответа; можно начать с подходящей задачи.',
    'check_understanding':'Тему объясняли, но объяснение не доказывает понимание; нужен ответ ученика.',
    'check_unverified_scope':'Диагностика проверила только часть темы; проверим оставшиеся критерии без повторного изучения подтверждённого.',
    'recheck_due':'По сохранённой дате подошёл срок повторной проверки; это предложение, а не утрата знаний.',
    'continue_or_wait':'Критерий подтверждён на сохранённом задании; можно выбрать следующую тему без изменения Roadmap.',
    'review_evidence_time':'Даты свидетельств требуют проверки; уверенное подтверждение пока недоступно.',
}


def incomplete(reason):
    return dict(valid=False,reason=reason,sections={},next_choices=[],acceptance_required=True)


def excerpts(value,fields):
    result={};shortened=[]
    for name in fields:
        text=value.get(name)
        result[name+'_excerpt']=text[:240] if isinstance(text,str) else text
        if isinstance(text,str) and len(text)>240:shortened.append(name)
    result['truncated_fields']=shortened
    return result


def build(course_data,profile,progress,units,states,next_choices,roadmap,limit,offset):
    """Reuse authoritative criterion rows and diagnostic cards from one snapshot."""
    if not progress['valid']:return incomplete(progress.get('reason','Incomplete learning data'))
    groups={name:[] for name in SECTIONS}
    for name,unit in units.items():
        if unit['stale']:
            groups['unavailable'].append(dict(source='unit',unit=name,title=unit['title'],
                reason='unit_or_sources_changed',details=unit['reasons'],evidence=[]))
            continue
        state=states[name]
        for row in state['criteria']:
            stage=row['state'];section=('unavailable' if not row['time_valid'] else
                'confirmed' if stage in ('solved_independently','checked_later') else
                'partial' if stage=='practice_with_help' else 'gaps' if stage=='needs_review' else 'unverified')
            event=state['evidence'].get(str(row['evidence_revision']))
            evidence=[]
            if event:
                value=event['value']
                evidence=[dict(source='unit_assessment',revision=event['revision'],at=event['at'],
                    session=event['session'],unit_sha256=event['unit_sha256'],stage=value['stage'],
                    hints=value['hints'],task_id=value.get('task_id'),error_tags=value.get('error_tags',[]),
                    **excerpts(value,('question','answer','details')))]
            reason=REASONS[row['action']]
            if row['suppressed']:reason='Проверка пропущена или отложена по выбору ученика; не запускаем её автоматически.'
            groups[section].append(dict(source='unit',unit=name,title=unit['title'],criterion=row['criterion'],
                skill=row['title'],state=stage,scope='unit_criterion_only',evidence=evidence,
                fact_assurance=deepcopy(unit['fact_assurance']),uncertainties=unit['uncertainties'][:5],
                uncertainty_count=len(unit['uncertainties']),
                recommendation=dict(action=row['action'],reason=reason,suppressed=row['suppressed'],
                                    due_at=row['due_at'],requires_consent=True)))
    diagnosis=progress['diagnostic']
    for card in diagnosis.get('competence_map',[]):
        section={'confirmed':'confirmed','partial':'partial','gap':'gaps','insufficient':'unverified'}[card['status']]
        evidence=[]
        for item in card['evidence'][:20]:
            evidence.append(dict(source='diagnostic_answer',**{key:item.get(key) for key in
                ('answer_revision','prompt_revision','difficulty','result','assistance','legacy_evidence','critical_errors')},
                **excerpts(item,('explanation',))))
        groups[section].append(dict(source='diagnostic',competence=card['competence'],status=card['status'],
            scope=card['scope'],confirmed_levels=card['confirmed_levels'],has_conflicts=card['has_conflicts'],
            criteria=deepcopy(card['criteria'][:5]),criterion_count=len(card['criteria']),
            gaps=card['gaps'][:5],gap_count=len(card['gaps']),evidence=evidence,evidence_count=len(card['evidence']),
            details_required=len(card['criteria'])>5 or len(card['gaps'])>5 or len(card['evidence'])>20,
            limitations=card['limitations'],reason_codes=card['reason_codes']))
    for row in progress['studied_lessons']:
        groups['legacy'].append(dict(source='legacy_lesson',lesson=row['lesson'],topic=row['topic'],
            stage=row['stage'],invalidated=row['invalidated'],verification='legacy_recorded_claim',
            reason='Старая запись занятия не содержит полноценной оценки по критериям и не подтверждает навык.'))
    sections={}
    for name,rows in groups.items():
        end=offset+limit
        sections[name]=dict(items=rows[offset:end],total=len(rows),offset=offset,
            next_offset=end if end<len(rows) else None)
    return dict(valid=True,course={key:course_data.get(key) for key in ('id','title')},profile=profile,
        revisions=dict(unit_learning=progress.get('unit_event_count',0),diagnostic=diagnosis['revision']),
        sections=sections,next_choices=deepcopy(next_choices),acceptance_required=True,
        diagnostic_recommendations=deepcopy(diagnosis.get('recommendations',[])),
        diagnostic_limitations=diagnosis.get('limitations',[]),
        roadmap_status=roadmap['status'],
        limitations=['assistant_recorded_evidence_not_automatic_grading',
            'unit_criteria_do_not_verify_diagnostic_competence_alignment',
            'unit_rows_describe_latest_criterion_evidence_not_entire_history',
            'diagnostic_map_describes_current_attempt_not_all_historical_attempts',
            'excerpts_are_not_full_answers_request_original_events_when_needed',
            'no_automatic_progress_or_roadmap_changes'])
