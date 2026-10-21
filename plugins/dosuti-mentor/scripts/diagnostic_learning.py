"""Voluntary diagnostic evidence; questions and evaluations come from the assistant."""
from copy import deepcopy
from pathlib import Path
from profile_storage import checked
from storage_lock import file_lock
from learning_intent import text
from read_snapshot import cached

SCHEMA = 'diagnostic_learning_v1'
MAX_BYTES = 64 * 1024 * 1024
DIFFICULTIES = {'basic', 'applied', 'transfer'}
EVIDENCE = {'confirmed', 'needs_practice', 'unknown'}
FORMATS = {'single_choice', 'multiple_choice', 'open', 'practical',
           'code_analysis', 'bug_search', 'explanation'}


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
    optional = {'skip': {'skip_reason'}}
    required = fields.get(action, set()) | {'action'}
    extra = extensions.get(action, set())
    allowed = required | extra | optional.get(action, set())
    if (action not in fields or not required <= set(value) or set(value) - allowed
            or (new and action in extensions and not extra <= set(value))
            or (action == 'answer' and bool(set(value) & extra) and not extra <= set(value))):
        raise ValueError('Invalid diagnostic action fields')
    if action == 'accept':
        names = value['competences']
        if not isinstance(names, list) or not 1 <= len(names) <= 50:
            raise ValueError('Diagnostic requires 1..50 concrete competences')
        for name in names: text(name, 'Competence', 1000)
        if len(set(names)) != len(names): raise ValueError('Competences must be unique')
    if action == 'prompt':
        text(value['competence'], 'Competence', 1000)
        text(value['prompt'], 'Actual diagnostic prompt')
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


def apply(attempts, event):
    value = validate(event['value']); action = value['action']
    current = attempts[-1] if attempts else None
    if action in ('accept', 'decline'):
        if current and current['status'] not in ('declined', 'completed'):
            raise ValueError('Resume or complete the existing diagnostic first')
        current = dict(id=event['revision'], status='declined' if action == 'decline' else 'accepted',
                       competences=deepcopy(value.get('competences', [])),
                       next_difficulty=value.get('next_difficulty'), pending_prompt=None, answers=[])
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
                if current['pending_prompt'] is not None: raise ValueError('Answer the pending prompt first')
                if value['competence'] not in current['competences']: raise ValueError('Unknown diagnostic competence')
                if 'next_competence' in current and current['next_competence'] is None:
                    raise ValueError('Terminal answer requires completion before a new diagnostic')
                if 'next_competence' in current and value['competence'] != current['next_competence']:
                    raise ValueError('Prompt must use next competence')
                if value['difficulty'] != current['next_difficulty']: raise ValueError('Prompt must use next difficulty')
                current['pending_prompt'] = dict(revision=event['revision'], **deepcopy(value))
            elif action == 'answer':
                prompt = current['pending_prompt']
                if prompt is None or prompt['revision'] != value['prompt_revision']:
                    raise ValueError('Answer must reference the pending prompt')
                if ('next_competence' in value and value['next_competence'] is not None
                        and value['next_competence'] not in current['competences']):
                    raise ValueError('Unknown next diagnostic competence')
                current['answers'].append(dict(revision=event['revision'], competence=prompt['competence'],
                    prompt=prompt['prompt'], difficulty=prompt['difficulty'],
                    **({'format': prompt['format']} if 'format' in prompt else {}), **deepcopy(value)))
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
                    skip_reason=value.get('skip_reason')))
                current['pending_prompt'] = None
                current['next_difficulty'] = value['next_difficulty']
                current['next_competence'] = value['next_competence']
                current['selection_reason'] = value['selection_reason']
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


def summary(current):
    if current is None:
        return None
    if not current['competences']:
        return dict(checked_competences=[], confirmed_knowledge=[],
                    partially_confirmed_knowledge=[], identified_gaps=[],
                    unverified_areas=[], skipped_questions=[],
                    recommended_start=dict(competence=None, basis='diagnostic_declined'),
                    limitations=['diagnostic_was_not_completed',
                                 'assistant_recorded_evidence_not_automatic_grading'])
    latest = {answer['competence']: answer for answer in current['answers']}
    groups = {name: [] for name in EVIDENCE}
    for competence in current['competences']:
        answer = latest.get(competence)
        if answer is not None:
            groups[answer['evidence']].append(competence)
    unverified = [name for name in current['competences'] if name not in latest]
    if groups['unknown']:
        start = dict(competence=groups['unknown'][0], basis='identified_gap')
    elif groups['needs_practice']:
        start = dict(competence=groups['needs_practice'][0], basis='partial_confirmation')
    elif unverified:
        start = dict(competence=unverified[0], basis='unverified')
    else:
        start = dict(competence=current['competences'][0], basis='confirm_with_transfer_task')
    limitations = ['assistant_recorded_evidence_not_automatic_grading',
                   'unverified_areas_are_not_treated_as_gaps']
    skips = current.get('skips', [])
    if skips:
        limitations.append('skipped_questions_are_not_incorrect_answers')
    return dict(checked_competences=[name for name in current['competences'] if name in latest],
                confirmed_knowledge=groups['confirmed'],
                partially_confirmed_knowledge=groups['needs_practice'],
                identified_gaps=groups['unknown'], unverified_areas=unverified,
                skipped_questions=deepcopy(skips), recommended_start=start,
                limitations=limitations)


def view(args):
    import course
    session = getattr(args, 'session', None)
    if session is not None: course.slug(session)
    limit = getattr(args, 'history_limit', 0)
    if type(limit) is not int or not 0 <= limit <= 100: raise ValueError('History limit must be 0..100')
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


def diagnostic(args):
    import course
    from learning_tools import read_json
    incoming = getattr(args, 'update_file', None)
    if incoming:
        value = validate(read_json(incoming), new=True); text(getattr(args, 'reason', None), 'Reason')
        expected = getattr(args, 'expected_revision', None)
        if type(expected) is not int or expected < 0: raise ValueError('Diagnostic write requires --expected-revision')
        view(args)  # Validate read options before writing.
        path = path_for(args)
        for directory in (path.parent.parent, path.parent):
            directory.mkdir(exist_ok=True); checked(directory, True)
        lock = path.with_suffix('.lock'); checked(lock)
        with file_lock(lock):
            checked(lock); journal, attempts = load(path)
            if journal['revision'] != expected: raise ValueError('Diagnostic changed in another chat; read again')
            event = dict(revision=expected + 1, at=course.now(), reason=args.reason,
                         session=getattr(args, 'session', None), value=value)
            apply(attempts, event)
            from document_store import write
            write(path, dict(schema=SCHEMA, revision=expected + 1, events=journal['events'] + [event]),
                  'diagnostic_learning', limit=MAX_BYTES)
    return view(args)
