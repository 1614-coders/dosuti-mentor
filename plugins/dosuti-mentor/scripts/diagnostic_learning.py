"""Voluntary diagnostic evidence; questions and evaluations come from the assistant."""
from copy import deepcopy
from pathlib import Path
from profile_storage import checked
from storage_lock import file_lock
from learning_intent import text, validate_read_options
from read_snapshot import cached

SCHEMA = 'diagnostic_learning_v1'
QUESTION_POLICY = 'fixed_15_v1'
QUESTION_LIMIT = 15
SELECTION_POLICY = 'adaptive_v1'
MAX_BYTES = 64 * 1024 * 1024
DIFFICULTIES = {'basic', 'applied', 'transfer'}
EVIDENCE = {'confirmed', 'needs_practice', 'unknown'}
FORMATS = {'single_choice', 'multiple_choice', 'open', 'practical',
           'code_analysis', 'bug_search', 'explanation'}


def rubric_texts(values, label, minimum=0):
    if not isinstance(values, list) or not minimum <= len(values) <= 20:
        raise ValueError('Invalid rubric ' + label)
    for item in values: text(item, label, 2000)
    if len(set(values)) != len(values): raise ValueError('Duplicate rubric ' + label)


def validate_rubric(value):
    if (not isinstance(value, dict) or not {'action', 'required', 'critical_errors'} <= set(value)
            or set(value) - {'action', 'required', 'alternatives', 'critical_errors'}):
        raise ValueError('Rubric requires action, required criteria and critical errors')
    text(value['action'], 'Observable learner action')
    for name in ('required', 'alternatives', 'critical_errors'):
        rubric_texts(value.get(name, []), name, 1 if name == 'required' else 0)


def validate_assessment(value, evidence, rubric=None):
    fields = {'result', 'met', 'unmet', 'unverified', 'critical_errors', 'assistance', 'verification'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Structured assessment requires result, criterion decisions, assistance and verification')
    result = value['result']
    if not isinstance(result, str) or result not in ('correct', 'partial', 'incorrect', 'insufficient'):
        raise ValueError('Unsupported assessment result')
    for name in ('met', 'unmet', 'unverified', 'critical_errors'):
        indices = value[name]
        if (not isinstance(indices, list) or len(indices) > 20
                or any(type(i) is not int or not 1 <= i <= 20 for i in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError('Invalid assessment criterion indices: ' + name)
    met, unmet, unverified, errors = (value[n] for n in ('met', 'unmet', 'unverified', 'critical_errors'))
    decisions = met + unmet + unverified
    if len(set(decisions)) != len(decisions): raise ValueError('Criterion decisions must not overlap')
    if rubric is not None and (set(decisions) != set(range(1, len(rubric['required']) + 1))
            or any(i > len(rubric['critical_errors']) for i in errors)):
        raise ValueError('Assessment must cover exactly the saved rubric criteria')
    if ((result == 'correct' and (not met or unmet or unverified or errors))
            or (result == 'partial' and (not met or not unmet or errors))
            or (result == 'incorrect' and (not unmet or (met and not errors)))
            or (result == 'insufficient' and not unverified)
            or (errors and result != 'incorrect')):
        raise ValueError('Result contradicts criterion decisions')
    assistance = value['assistance']
    if (not isinstance(assistance, dict) or set(assistance) != {'status', 'details'}
            or not isinstance(assistance['status'], str)
            or assistance['status'] not in ('independent', 'assisted', 'unknown')):
        raise ValueError('Assistance must distinguish independent, assisted and unknown')
    if assistance['details'] is not None or assistance['status'] == 'assisted':
        text(assistance['details'], 'Actual assistance or its uncertainty')
    expected = ('confirmed' if result == 'correct' and assistance['status'] == 'independent' else
                'needs_practice' if result in ('correct', 'partial') else 'unknown')
    if evidence != expected: raise ValueError('Evidence contradicts result or recorded assistance')
    verification = value['verification']
    if (not isinstance(verification, dict) or not {'method', 'details', 'limitations'} <= set(verification)
            or set(verification) - {'method', 'details', 'limitations', 'execution'}
            or not isinstance(verification['method'], str)
            or verification['method'] not in ('assistant_review', 'execution', 'mixed', 'unknown')):
        raise ValueError('Invalid assessment verification')
    text(verification['details'], 'Verification explanation')
    rubric_texts(verification['limitations'], 'verification limitations', 1)
    if verification['method'] == 'unknown' and (result != 'insufficient' or met or unmet):
        raise ValueError('Unknown verification cannot support a definite result')
    if verification['method'] in ('execution', 'mixed'):
        run = verification.get('execution')
        if not isinstance(run, dict) or set(run) != {'command', 'exit_code', 'output'}:
            raise ValueError('Execution claim requires command, exit code and actual output')
        text(run['command'], 'Executed command')
        if type(run['exit_code']) is not int or not isinstance(run['output'], str) or len(run['output']) > 16000:
            raise ValueError('Invalid execution record')
    elif 'execution' in verification:
        raise ValueError('Non-execution verification cannot claim an execution record')


def validate(value, *, new=False):
    """Replay legacy v1 fields unchanged; require adaptive fields for new writes."""
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('Diagnostic update requires action')
    action = value['action']
    fields = {
        'accept': {'competences', 'next_difficulty'}, 'decline': set(),
        'prompt': {'competence', 'prompt', 'difficulty'},
        'answer': {'prompt_revision', 'answer', 'evaluation', 'evidence', 'next_difficulty'},
        'skip': {'prompt_revision', 'next_difficulty', 'next_competence', 'selection_reason'},
        'pause': set(), 'resume': set(), 'complete': set(),
    }
    extensions = {'prompt': {'format'}, 'answer': {'next_competence', 'selection_reason'}}
    optional = {'accept': {'question_policy', 'selection_policy','competence_prerequisites','starting_reason'}, 'prompt': {'rubric'},
                'answer': {'assessment'}, 'skip': {'skip_reason'}}
    required = fields.get(action, set()) | {'action'}
    extra = extensions.get(action, set())
    allowed = required | extra | optional.get(action, set())
    if (action not in fields or not required <= set(value) or set(value) - allowed
            or (new and action in extensions and not extra <= set(value))
            or (action == 'answer' and bool(set(value) & extra) and not extra <= set(value))):
        raise ValueError('Invalid diagnostic action fields')
    if action == 'accept':
        if 'question_policy' in value and value['question_policy'] != QUESTION_POLICY:
            raise ValueError('Unsupported diagnostic question policy')
        from diagnostic_policy import POLICY,validate_graph
        if 'selection_policy' in value and (value['selection_policy'] not in (SELECTION_POLICY,POLICY)
                or value.get('question_policy') != QUESTION_POLICY):
            raise ValueError('Adaptive selection requires a supported policy and fixed_15_v1')
        names = value['competences']
        if not isinstance(names, list) or not 1 <= len(names) <= 50:
            raise ValueError('Diagnostic requires 1..50 concrete competences')
        for name in names: text(name, 'Competence', 1000)
        if len(set(names)) != len(names): raise ValueError('Competences must be unique')
        if value.get('selection_policy')==POLICY:
            if 'competence_prerequisites' not in value:raise ValueError('adaptive_v2 requires competence_prerequisites')
            validate_graph(names,value['competence_prerequisites'])
        elif 'competence_prerequisites' in value or 'starting_reason' in value:
            raise ValueError('Prerequisites and calibration reason require adaptive_v2')
        if 'starting_reason' in value:text(value['starting_reason'],'Starting calibration reason')
    if action == 'prompt':
        text(value['competence'], 'Competence', 1000)
        text(value['prompt'], 'Actual diagnostic prompt')
        if 'rubric' in value: validate_rubric(value['rubric'])
        if 'format' in value and (not isinstance(value['format'], str) or value['format'] not in FORMATS):
            raise ValueError('Unsupported diagnostic prompt format')
    if action == 'answer':
        if type(value['prompt_revision']) is not int or value['prompt_revision'] < 1:
            raise ValueError('Answer requires prompt revision')
        text(value['answer'], 'Actual learner answer')
        text(value['evaluation'], 'Assistant evaluation')
        if 'next_competence' in value:
            if value['next_competence'] is not None:
                text(value['next_competence'], 'Next competence', 1000)
            text(value['selection_reason'], 'Concrete reason for next competence')
        if not isinstance(value['evidence'], str) or value['evidence'] not in EVIDENCE:
            raise ValueError('Evidence must be confirmed, needs_practice or unknown')
        if 'assessment' in value: validate_assessment(value['assessment'], value['evidence'])
    if action == 'skip':
        if type(value['prompt_revision']) is not int or value['prompt_revision'] < 1:
            raise ValueError('Skip requires prompt revision')
        if value['next_competence'] is not None:
            text(value['next_competence'], 'Next competence', 1000)
        text(value['selection_reason'], 'Concrete reason for next competence')
        if 'skip_reason' in value and value['skip_reason'] is not None:
            text(value['skip_reason'], 'Skip reason')
    for field in ('difficulty', 'next_difficulty'):
        if field in value and (not isinstance(value[field], str) or value[field] not in DIFFICULTIES):
            raise ValueError('Difficulty must be basic, applied or transfer')
    return value


def question_history(current):
    rows = current['answers'] + current.get('skips', [])
    if current['pending_prompt'] is not None: rows = rows + [current['pending_prompt']]
    return sorted(rows, key=lambda row: row['revision'])


def assessed_competences(current):
    # Coverage is not mastery. Insufficient and legacy evidence do not invent a result.
    return {row['competence'] for row in current['answers']
            if row.get('assessment', {}).get('result') in ('correct', 'partial', 'incorrect')}


def check_selection(current, competence, prompt=None):
    from diagnostic_policy import POLICY,check_prompt
    if current.get('selection_policy') not in (SELECTION_POLICY,POLICY): return
    if current.get('selection_policy')==POLICY:check_prompt(current,competence)
    rows = question_history(current)
    if prompt is not None and any(row['prompt'] == prompt for row in rows):
        raise ValueError('Do not repeat an identical diagnostic prompt in this attempt')
    if (competence is not None and len(rows) >= 3
            and all(row['competence'] == competence for row in rows[-3:])
            and any(name != competence and name not in assessed_competences(current)
                    for name in current['competences'])):
        raise ValueError('Choose another unassessed competence after three consecutive questions')


def selection_guidance(current, starting):
    if current is None: return dict(valid=starting['valid'], action='not_started')
    rows = question_history(current)
    resolved = sorted(current['answers'] + current.get('skips', []), key=lambda row: row['revision'])
    last = resolved[-1] if resolved else None
    response = last if last and last.get('action') == 'answer' else None
    assessment = (response or {}).get('assessment', {})
    result = assessment.get('result')
    action = dict(correct='increase_or_change', partial='probe_gap', incorrect='check_prerequisite',
                  insufficient='clarify_response').get(result, 'review_legacy_evidence')
    if result == 'correct' and assessment['assistance']['status'] != 'independent': action = 'independent_recheck'
    if last is None: action = 'initial_question'
    elif response is None: action = 'switch_after_skip'
    if current['pending_prompt'] is not None: action = 'resume_pending'
    elif current['status'] == 'paused': action = 'resume_attempt'
    if current['status'] in ('completed', 'declined'): action = current['status']
    unassessed = [name for name in current['competences'] if name not in assessed_competences(current)]
    streak = 0
    for row in reversed(rows):
        if row['competence'] != rows[-1]['competence']: break
        streak += 1
    budget = current.get('question_budget', {})
    remaining = budget.get('remaining')
    near_end = remaining is not None and 0 < remaining <= 3
    indices = assessment.get('unverified' if result == 'insufficient' else 'unmet', [])
    if action == 'independent_recheck': indices = assessment['met']
    required = (response or {}).get('rubric', {}).get('required', [])
    result=dict(valid=starting['valid'], action=action, goal=deepcopy(starting.get('goal')),
        subject=deepcopy(starting.get('subject')), last_response=deepcopy(response),
        focus_criteria=[required[i-1] for i in indices], remaining=remaining, near_end=near_end,
        unassessed_competences=unassessed, priority_competences=unassessed if near_end else [],
        consecutive_competence=rows[-1]['competence'] if rows else None, consecutive_count=streak,
        rotation_recommended=streak >= 2 and len(current['competences']) > 1,
        rotation_required=streak >= 3 and any(name != rows[-1]['competence'] for name in unassessed),
        used_prompts=[row['prompt'] for row in rows],
        verification='selection_advice_not_mastery_or_automatic_question_generation')
    from diagnostic_policy import POLICY,guidance
    if current.get('selection_policy')==POLICY:
        result.update(guidance(current))
        result['priority_competences']=[n for n in result['priority_competences'] if n in result['eligible_competences']]
    return result


def apply(attempts, event, *, new=False):
    value = validate(event['value']); action = value['action']
    current = attempts[-1] if attempts else None
    if action in ('accept', 'decline'):
        if current and current['status'] not in ('declined', 'completed'):
            raise ValueError('Resume or complete the existing diagnostic first')
        current = dict(id=event['revision'], status='declined' if action == 'decline' else 'accepted',
                       competences=deepcopy(value.get('competences', [])),
                       next_difficulty=value.get('next_difficulty'), pending_prompt=None, answers=[])
        if 'question_policy' in value:
            current.update(question_policy=value['question_policy'], question_budget=dict(
                limit=QUESTION_LIMIT, asked=0, remaining=QUESTION_LIMIT, current_question=None, limit_reached=False))
        if 'selection_policy' in value: current['selection_policy'] = value['selection_policy']
        if 'competence_prerequisites' in value:current['competence_prerequisites']=deepcopy(value['competence_prerequisites'])
        attempts.append(current)
    else:
        if current is None: raise ValueError('Accept a diagnostic first')
        status = current['status']
        if action == 'resume':
            if status != 'paused': raise ValueError('Only a paused diagnostic can resume')
            current['status'] = 'accepted'
        else:
            if status != 'accepted': raise ValueError('Diagnostic must be active')
            if action == 'pause': current['status'] = 'paused'
            elif action == 'complete':
                current['status'] = 'completed'
            elif action == 'prompt':
                if current.get('selection_policy')=='adaptive_v2' and 'rubric' not in value:raise ValueError('adaptive_v2 requires a saved rubric')
                if new: check_selection(current, value['competence'], value['prompt'])
                budget = current.get('question_budget')
                if budget and budget['limit_reached']:raise ValueError('Diagnostic question limit reached')
                if current['pending_prompt'] is not None: raise ValueError('Answer the pending prompt first')
                if value['competence'] not in current['competences']: raise ValueError('Unknown diagnostic competence')
                if 'next_competence' in current and current['next_competence'] is None:
                    raise ValueError('Terminal answer requires completion before a new diagnostic')
                if 'next_competence' in current and value['competence'] != current['next_competence']:
                    raise ValueError('Prompt must use next competence')
                if value['difficulty'] != current['next_difficulty']: raise ValueError('Prompt must use next difficulty')
                current['pending_prompt'] = dict(revision=event['revision'], **deepcopy(value))
                if budget:
                    budget['asked'] += 1
                    budget.update(remaining=budget['limit']-budget['asked'],
                                  current_question=budget['asked'], limit_reached=budget['asked']==budget['limit'])
            elif action == 'answer':
                prompt = current['pending_prompt']
                if prompt is None or prompt['revision'] != value['prompt_revision']:
                    raise ValueError('Answer must reference the pending prompt')
                if ('rubric' in prompt) != ('assessment' in value):
                    raise ValueError('Structured assessment requires a rubric saved before the answer')
                if 'assessment' in value:
                    validate_assessment(value['assessment'], value['evidence'], prompt['rubric'])
                if current.get('selection_policy')=='adaptive_v2':
                    from diagnostic_policy import check_answer
                    check_answer(current,prompt,value)
                if ('next_competence' in value and value['next_competence'] is not None
                        and value['next_competence'] not in current['competences']):
                    raise ValueError('Unknown next diagnostic competence')
                current['answers'].append(dict(revision=event['revision'], competence=prompt['competence'],
                    prompt=prompt['prompt'], difficulty=prompt['difficulty'],
                    **({'format': prompt['format']} if 'format' in prompt else {}),
                    **({'rubric': deepcopy(prompt['rubric'])} if 'rubric' in prompt else {}), **deepcopy(value)))
                current['pending_prompt'] = None
                current['next_difficulty'] = value['next_difficulty']
                if 'next_competence' in value:
                    current['next_competence'] = value['next_competence']
                    current['selection_reason'] = value['selection_reason']
            elif action == 'skip':
                prompt = current['pending_prompt']
                if prompt is None or prompt['revision'] != value['prompt_revision']:
                    raise ValueError('Skip must reference the pending prompt')
                if (value['next_competence'] is not None
                        and value['next_competence'] not in current['competences']):
                    raise ValueError('Unknown next diagnostic competence')
                if value['next_competence'] == prompt['competence']:
                    raise ValueError('Skip must continue with another competence or complete')
                current.setdefault('skips', []).append(dict(revision=event['revision'],
                    competence=prompt['competence'], prompt=prompt['prompt'],
                    difficulty=prompt['difficulty'],
                    **({'format': prompt['format']} if 'format' in prompt else {}),
                    **({'rubric': deepcopy(prompt['rubric'])} if 'rubric' in prompt else {}),
                    skip_reason=value.get('skip_reason')))
                current['pending_prompt'] = None
                current['next_difficulty'] = value['next_difficulty']
                current['next_competence'] = value['next_competence']
                current['selection_reason'] = value['selection_reason']
    budget = current.get('question_budget')
    if budget and action in ('answer', 'skip', 'complete'):
        budget['current_question'] = None
        if budget['limit_reached'] and action != 'complete':
            current.update(status='completed', next_competence=None)
    if new and action in ('answer', 'skip') and current['status'] == 'accepted':
        check_selection(current, current.get('next_competence'))
    current['last_action'] = action
    current['revision'] = event['revision']


@cached
def load(path):
    from document_store import read
    from adaptive_learning import timestamp
    checked(path)
    journal = read(path, dict(schema=SCHEMA, revision=0, events=[]), 'diagnostic_learning')
    if (not isinstance(journal, dict) or set(journal) != {'schema', 'revision', 'events'}
            or journal['schema'] != SCHEMA or type(journal['revision']) is not int
            or journal['revision'] < 0 or not isinstance(journal['events'], list)
            or len(journal['events']) != journal['revision']):
        raise ValueError('Invalid diagnostic journal')
    attempts = []
    for revision, event in enumerate(journal['events'], 1):
        if (not isinstance(event, dict) or set(event) != {'revision', 'at', 'reason', 'session', 'value'}
                or type(event['revision']) is not int or event['revision'] != revision):
            raise ValueError('Invalid diagnostic history')
        timestamp(event['at']); text(event['reason'], 'Reason')
        if event['session'] is not None:
            import course
            course.slug(event['session'])
        apply(attempts, event)
    return journal, attempts


def path_for(args):
    from learning_tools import learner
    path = learner(args) / 'diagnostic-learning.json'
    if path.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Diagnostic must stay outside plugin')
    return path


def competence_card(current, name):
    answers = [row for row in current['answers'] if row['competence'] == name]
    skips = [row for row in current.get('skips', []) if row['competence'] == name]
    criteria = {}; evidence = []; legacy = []
    for answer in answers:
        assessment = answer.get('assessment')
        evidence.append(dict(answer_revision=answer['revision'], prompt_revision=answer.get('prompt_revision'),
            difficulty=answer['difficulty'], result=assessment['result'] if assessment else None,
            assistance=assessment['assistance']['status'] if assessment else None,
            legacy_evidence=answer['evidence'], explanation=answer['evaluation'],
            critical_errors=[answer['rubric']['critical_errors'][i-1] for i in assessment['critical_errors']] if assessment else None))
        if assessment is None:
            legacy.append(answer); continue
        for index, description in enumerate(answer['rubric']['required'], 1):
            key = (answer['difficulty'], description)
            row = criteria.setdefault(key, dict(difficulty=key[0], criterion=description,
                positive_revisions=[], independent_revisions=[], error_revisions=[], unverified_revisions=[], tasks=set()))
            revision = answer['revision']
            if index in assessment['unmet']: row['error_revisions'].append(revision)
            if index in assessment['met']:
                row['positive_revisions'].append(revision)
                if assessment['assistance']['status'] == 'independent' and assessment['result'] != 'insufficient':
                    row['independent_revisions'].append(revision); row['tasks'].add(answer['prompt'])
            if index in assessment['unverified'] or assessment['result'] == 'insufficient':
                row['unverified_revisions'].append(revision)
    rows = list(criteria.values())
    for row in rows:
        tasks = row.pop('tasks'); row['independent_tasks'] = len(tasks)
        row['status'] = ('gap' if row['error_revisions'] else
                         'confirmed' if len(tasks) >= 2 and not row['unverified_revisions'] else
                         'partial' if row['positive_revisions'] else 'insufficient')
    gaps = list(dict.fromkeys(row['criterion'] for row in rows if row['error_revisions']))
    uncovered_skips = any('rubric' not in skip or any(
        criteria.get((skip['difficulty'], description), {}).get('status') != 'confirmed'
        for description in skip['rubric']['required']) for skip in skips)
    status = ('gap' if gaps else
              'confirmed' if rows and all(row['status'] == 'confirmed' for row in rows) and not legacy and not uncovered_skips else
              'partial' if any(row['positive_revisions'] for row in rows) or any(a['evidence'] != 'unknown' for a in legacy) else
              'insufficient')
    levels = [level for level in ('basic', 'applied', 'transfer')
              if any(row['difficulty'] == level for row in rows)
              and all(row['status'] == 'confirmed' for row in rows if row['difficulty'] == level)
              and not any(a['difficulty'] == level for a in legacy)]
    reasons = (["observed_unmet_criteria"] if gaps else []) + (["legacy_scope_unknown"] if legacy else [])
    reasons += ["multiple_independent_tasks"] if status == 'confirmed' else ["confirmation_not_established"]
    return dict(competence=name, status=status, criteria=rows, evidence=evidence, gaps=gaps, reason_codes=reasons,
        confirmed_levels=levels, has_conflicts=any(row['positive_revisions'] and row['error_revisions'] for row in rows),
        skipped_questions=deepcopy(skips), scope='observed_criteria_and_difficulties_only',
        limitations=(['legacy_results_unclassified'] if legacy else []) +
                    (['unanswered_or_skipped_tasks'] if skips else []))


def summary(current):
    if current is None: return None
    cards = [competence_card(current, name) for name in current['competences']]
    names = lambda status: [row['competence'] for row in cards if row['status'] == status]
    recommendations = [dict(competence=row['competence'],
        action={'gap':'address_specific_gap', 'partial':'collect_independent_evidence', 'insufficient':'clarify_or_assess'}[row['status']],
        reason=row['reason_codes'], criteria=row['gaps'],
        focus_criteria=[dict(criterion=c['criterion'],difficulty=c['difficulty'],status=c['status'])
                        for c in row['criteria'] if c['status'] != 'confirmed'], evidence_revisions=[r['answer_revision'] for r in row['evidence']])
        for row in cards if row['status'] != 'confirmed']
    priority = {'address_specific_gap':0, 'collect_independent_evidence':1, 'clarify_or_assess':2}
    recommendations.sort(key=lambda row: priority[row['action']])
    first = recommendations[0] if recommendations else None
    basis = ({'address_specific_gap':'identified_gap', 'collect_independent_evidence':'partial_confirmation',
              'clarify_or_assess':'unverified'}[first['action']] if first else
             'diagnostic_declined' if current['status'] == 'declined' else 'observed_scope_confirmed')
    limitations = ['assistant_recorded_evidence_not_automatic_grading', 'unverified_areas_are_not_treated_as_gaps',
                  'confirmation_limited_to_observed_criteria_and_difficulty',
                  'criterion_matching_uses_exact_descriptions_not_semantic_equivalence']
    if current['status'] != 'completed': limitations.append('diagnostic_was_not_completed')
    if current.get('question_budget', {}).get('remaining', 0) and current['status'] == 'completed':
        limitations.append('diagnostic_completed_early')
    if current.get('skips'): limitations.append('skipped_questions_are_not_incorrect_answers')
    if any(row['has_conflicts'] for row in cards): limitations.append('conflicting_evidence_requires_fresh_verification')
    return dict(checked_competences=[name for name in current['competences'] if any(a['competence'] == name for a in current['answers'])],
        confirmed_knowledge=names('confirmed'), partially_confirmed_knowledge=names('partial'),
        identified_gaps=names('gap'), unverified_areas=names('insufficient'),
        skipped_questions=deepcopy(current.get('skips', [])), competence_map=cards, recommendations=recommendations,
        recommended_start=dict(competence=first['competence'] if first else None, basis=basis), limitations=limitations)


def view(args):
    _, limit = validate_read_options(args)
    path = path_for(args)
    try: journal, attempts = load(path)
    except (ValueError, OSError) as error:
        return dict(valid=False, revision=None, reason=str(error),
                    recovery='Preserve and repair diagnostic-learning.json before writing')
    current = attempts[-1] if attempts else None
    return dict(valid=True, revision=journal['revision'], status=current['status'] if current else 'not_started',
                current=current, attempts=attempts, history=journal['events'][-limit:] if limit else [],
                summary=summary(current),
                verification='assistant_recorded_evidence_not_automatic_grading')


def starting_guidance(args):
    from learning_tools import preferences
    from learning_intent import load as load_intent, path_for as intent_path
    try:
        answers=preferences(args)['onboarding_answers']
        _, intent=load_intent(intent_path(args))
        assessment=answers.get('self_assessment',{})
        level=assessment.get('value')
        if assessment.get('status')!='answered' or not isinstance(level,str):level=None
        levels=dict(zero='basic',basics='basic',basic_experience='applied',
                    confident='applied',significant_experience='transfer')
        difficulty=levels.get(level)
        # Natural-language interpretation belongs to the assistant, not a text heuristic.
        # None requires an explicit subject-specific calibration choice before accept.
        return dict(valid=True,difficulty=difficulty,calibration=difficulty is None,
            next_action=('interpret_text_or_select_subject_calibration' if difficulty is None
                         else 'select_subject_task'),usage='new_attempt_only',
            subject=deepcopy(answers.get('subject',{})),
            self_assessment=deepcopy(answers.get('self_assessment',{})),
            outcome=deepcopy(answers.get('outcome',{})),goal=deepcopy(intent['goal']),
            verification='self_report_for_question_selection_not_competence_evidence')
    except (ValueError,OSError) as error:
        return dict(valid=False,reason=str(error))


def diagnostic(args):
    import course
    from learning_tools import read_json
    incoming = getattr(args, 'update_file', None)
    if incoming:
        value=read_json(incoming)
        if isinstance(value,dict) and value.get('action')=='accept' and value.get('selection_policy')=='adaptive_v2':
            guidance=starting_guidance(args)
            if not guidance['valid']:raise ValueError('Cannot choose initial difficulty: '+guidance['reason'])
            if (guidance['difficulty'] is not None and 'next_difficulty' in value
                    and value['next_difficulty']!=guidance['difficulty'] and 'starting_reason' not in value):
                raise ValueError('Initial difficulty differs from self-assessment; provide a starting calibration reason')
        if isinstance(value,dict) and value.get('action')=='accept' and 'next_difficulty' not in value:
            guidance=starting_guidance(args)
            if not guidance['valid']:raise ValueError('Cannot choose initial difficulty: '+guidance['reason'])
            if guidance['difficulty'] is None:
                raise ValueError('Initial calibration requires explicit next_difficulty: interpret the self-report '
                                 'or select a subject-specific calibration task')
            value=dict(value,next_difficulty=guidance['difficulty'])
        value = validate(value, new=True); text(getattr(args, 'reason', None), 'Reason')
        expected = getattr(args, 'expected_revision', None)
        if type(expected) is not int or expected < 0: raise ValueError('Diagnostic write requires --expected-revision')
        validate_read_options(args)
        path = path_for(args)
        for directory in (path.parent.parent, path.parent):
            directory.mkdir(exist_ok=True); checked(directory, True)
        lock = path.with_suffix('.lock'); checked(lock)
        with file_lock(lock):
            checked(lock); journal, attempts = load(path)
            if journal['revision'] != expected: raise ValueError('Diagnostic changed in another chat; read again')
            event = dict(revision=expected + 1, at=course.now(), reason=args.reason,
                         session=getattr(args, 'session', None), value=value)
            apply(attempts, event, new=True)
            from document_store import write
            write(path, dict(schema=SCHEMA, revision=expected + 1, events=journal['events'] + [event]),
                  'diagnostic_learning', limit=MAX_BYTES)
    state = view(args); starting = starting_guidance(args)
    state['route_guidance'] = dict(goal=deepcopy(starting.get('goal')),
        recommendations=(state.get('summary') or {}).get('recommendations', []),
        diagnostic_revision=state.get('revision'), acceptance_required=True)
    guidance = (selection_guidance(state.get('current'), starting) if state['valid'] else
                dict(valid=False, action='repair_diagnostic', reason=state['reason']))
    return dict(state, starting_guidance=starting, selection_guidance=guidance)
