"""Durable four-question intake; learning evidence remains in unit-learning."""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit
from atomic_files import atomic_json
from learning_tools import learner, read_json
from profile_storage import checked
from storage_lock import file_lock
from study_units import digest
from topic_research import fields, text

SCHEMA = 'topic_queue_v1'
QUESTIONS = {
    'title': 'Название темы — что будем изучать?',
    'source': 'Основной источник — пришли ссылку на источник по теме.',
    'homework': 'Домашнее задание — есть ли домашка? Если да, пришли её.',
    'additional': 'Дополнительные материалы — есть ли дополнительные ссылки, файлы или материалы?',
}
CHOICE = 'Тема сохранена. Начинаем изучение или добавим ещё одну тему?'


def empty():
    return dict(schema=SCHEMA, revision=0, topics=[], active_topic_id=None, mode='idle', receipts={})


def answer(step, value):
    text(value, 'topic ' + step, nullable=step in ('homework', 'additional'))
    if step == 'source':
        parts = urlsplit(value)
        if not (parts.scheme in ('https', 'http') and parts.hostname and not parts.username
                and not parts.password and not any(c.isspace() for c in value)) and not Path(value).is_absolute():
            raise ValueError('Use a source HTTP(S) link or an absolute local file path')


def validate(state):
    import course
    from source_integrity import digest as checksum
    fields(state, 'schema revision topics active_topic_id mode receipts')
    if state['schema'] != SCHEMA or type(state['revision']) is not int or state['revision'] < 0:
        raise ValueError('Invalid topic queue revision/schema')
    if not isinstance(state['topics'], list) or len(state['topics']) > 200: raise ValueError('Use at most 200 queued topics')
    if not isinstance(state['receipts'], dict) or len(state['receipts']) > 3000 or len(state['receipts']) != state['revision']:
        raise ValueError('Invalid topic queue receipts')
    for identity, value in state['receipts'].items(): course.slug(identity); checksum(value)
    ids = set(); units = set(); draft = None
    for item in state['topics']:
        fields(item, 'id answers binding started'); course.slug(item['id'])
        if item['id'] in ids: raise ValueError('Duplicate queued topic ID')
        ids.add(item['id']); answers = item['answers']
        if not isinstance(answers, dict) or set(answers) != set(list(QUESTIONS)[:len(answers)]):
            raise ValueError('Topic answers must follow all four questions')
        for step, value in answers.items(): answer(step, value)
        if type(item['started']) is not bool: raise ValueError('Invalid topic start flag')
        if len(answers) < 4:
            if draft is not None or item is not state['topics'][-1] or item['started'] or item['binding'] is not None:
                raise ValueError('Only the last topic may be incomplete')
            draft = item
        if item['binding'] is not None:
            binding = item['binding']; fields(binding, 'unit_id unit_sha256 source_review')
            course.slug(binding['unit_id']); checksum(binding['unit_sha256']); text(binding['source_review'], 'source review')
            if binding['unit_id'] in units: raise ValueError('Two queued topics cannot silently share one unit')
            units.add(binding['unit_id'])
    if state['mode'] not in ('idle', 'collecting', 'choice', 'learning'):
        raise ValueError('Invalid topic queue mode')
    if bool(draft) != (state['mode'] == 'collecting'): raise ValueError('Invalid unfinished topic state')
    if state['active_topic_id'] is not None:
        course.slug(state['active_topic_id'])
        if state['active_topic_id'] not in ids or not next(t for t in state['topics'] if t['id'] == state['active_topic_id'])['started']:
            raise ValueError('Invalid active queued topic')
    if state['mode'] == 'learning' and state['active_topic_id'] is None: raise ValueError('No selected topic')
    if (state['mode'] == 'idle' and state['topics']) or (state['mode'] == 'choice' and not state['topics']):
        raise ValueError('Invalid topic choice state')


def load(args, folder=None):
    path = (folder if folder is not None else learner(args)) / 'topic-queue.json'
    checked(path); state = read_json(path, empty()); validate(state)
    return path, state


def apply(args, state, event):
    import course, study_units
    identity = event.get('request_id'); course.slug(identity)
    action = event.get('action'); state = deepcopy(state)
    if action == 'add':
        fields(event, 'request_id action')
        if state['mode'] == 'collecting': raise ValueError('Finish the current four questions before adding another topic')
        topic = dict(id='topic-' + digest(identity)[:24], answers={}, binding=None, started=False)
        state['topics'].append(topic); state['mode'] = 'collecting'
    else:
        course.slug(event.get('topic_id'))
        topic = next((t for t in state['topics'] if t['id'] == event['topic_id']), None)
        if topic is None: raise ValueError('Queued topic not found in this course/profile')
        if action == 'answer':
            fields(event, 'request_id action topic_id step answer')
            expected = list(QUESTIONS)[len(topic['answers'])] if len(topic['answers']) < 4 else None
            if state['mode'] != 'collecting' or expected is None or event['step'] != expected:
                raise ValueError('Answer only the next missing topic question')
            answer(expected, event['answer']); topic['answers'][expected] = event['answer']
            if len(topic['answers']) == 4: state['mode'] = 'choice'
        elif action == 'start':
            fields(event, 'request_id action topic_id')
            if state['mode'] == 'collecting': raise ValueError('Complete all four questions before teaching')
            if len(topic['answers']) != 4: raise ValueError('Topic collection is incomplete')
            topic['started'] = True; state.update(active_topic_id=topic['id'], mode='learning')
        elif action == 'bind':
            fields(event, 'request_id action topic_id unit_id unit_sha256 source_review')
            if len(topic['answers']) != 4: raise ValueError('Collect all four answers before preparing a unit')
            course.slug(event['unit_id'])
            registry = study_units.load(course.root(args), include_history=False)
            if event['unit_id'] not in registry['units']: raise ValueError('Prepare an existing unit before linking it')
            unit = study_units.checked_units(course.root(args), registry, [event['unit_id']], include_contexts=False)[event['unit_id']]
            if unit['stale'] or unit['content_sha256'] != event['unit_sha256']: raise ValueError('Unit or sources changed; review before linking')
            topic['binding'] = {k: event[k] for k in ('unit_id', 'unit_sha256', 'source_review')}
        else: raise ValueError('Unknown topic queue action')
    state['receipts'][identity] = digest(event); state['revision'] += 1; validate(state)
    if len(json.dumps(state, ensure_ascii=False, indent=2).encode()) > 900000:
        raise ValueError('Topic queue size limit reached; preserve data, do not truncate answers')
    return state


def view(args, state, *, units=None, journal=None):
    import course, study_units, unit_learning
    from adaptive_learning import recommendations
    names = sorted({t['binding']['unit_id'] for t in state['topics'] if t['binding']})
    error = None; grouped = {}
    if names:
        try:
            if units is None:
                registry = study_units.load(course.root(args), include_history=False)
                units = study_units.checked_units(course.root(args), registry,
                    [name for name in names if name in registry['units']], include_contexts=False)
            if journal is None: journal = unit_learning.load(learner(args) / 'unit-learning.json', compact=True)
            grouped = unit_learning.journals_by_unit(journal)
        except (ValueError, OSError) as failure: error = str(failure)
    rows = []; learning = {}; continuation_views = {}
    for item in state['topics']:
        row = dict(id=item['id'], title=item['answers'].get('title'), status='waiting', needs_review=False,
                   collection_complete=len(item['answers']) == 4)
        binding = item['binding']
        if item['started']: row['status'] = 'studying'
        if binding:
            unit = (units or {}).get(binding['unit_id'])
            valid = bool(not error and unit and not unit['stale'] and unit['content_sha256'] == binding['unit_sha256'])
            row.update(unit_id=binding['unit_id'], needs_review=not valid)
            if valid:
                data = grouped[unit['id']]; progress = unit_learning.view(args, unit, data)
                adaptive = recommendations(unit, data, course.now())
                verified = progress['all_criteria_demonstrated'] and all(r['time_valid'] for r in adaptive['criteria'])
                if item['started'] and verified: row['status'] = 'completed'
                learning[item['id']] = dict(unit_id=unit['id'], unit_sha256=unit['content_sha256'],
                    revision=progress['revision'], criteria=progress['criteria'], checkpoint=progress['checkpoint'],
                    assessment_and_time_verified=verified)
                continuation_views[item['id']] = unit_learning.lesson_continuations({unit['id']: unit}, data)
        rows.append(row)
    draft = state['topics'][-1] if state['mode'] == 'collecting' else None
    choice = state['topics'][-1] if state['mode'] == 'choice' else None
    selected_id = getattr(args, 'topic_id', None) or (draft or choice or {}).get('id') or state['active_topic_id']
    if selected_id is None and state['topics']: selected_id = state['topics'][-1]['id']
    selected = next((t for t in state['topics'] if t['id'] == selected_id), None)
    if getattr(args, 'topic_id', None) is not None and selected is None: raise ValueError('Unknown queued topic')
    limit = getattr(args, 'limit', 10); offset = getattr(args, 'queue_offset', 0)
    if type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or offset < 0:
        raise ValueError('Queue limit must be 1..20 and offset nonnegative')
    pending = next((r['id'] for r in rows if r['status'] == 'waiting' and r['collection_complete']), None)
    result = dict(schema=SCHEMA, revision=state['revision'], mode=state['mode'], active_topic_id=state['active_topic_id'],
        topics=rows[offset:offset+limit], total_topics=len(rows), truncated=offset+limit<len(rows),
        next_waiting_topic_id=pending, selected_topic=deepcopy(selected), draft=deepcopy(draft),
        next_question=None, message=None, choices=[], next_action='add_topic',
        learning=learning.get(selected_id), continuations=continuation_views.get(selected_id, []), learning_error=error,
        collection_blocks_teaching=state['mode'] in ('collecting', 'choice'),
        verification='collection_not_preparation_or_mastery; completion_uses_existing_unit_evidence')
    if draft:
        step = list(QUESTIONS)[len(draft['answers'])]
        result.update(next_question=dict(step=step, text=QUESTIONS[step]), next_action='ask_next_question')
    elif state['mode'] == 'choice':
        result.update(message=CHOICE, choices=['Начать изучение', 'Добавить ещё одну тему'], next_action='await_choice')
    elif state['mode'] == 'learning':
        active = next(t for t in state['topics'] if t['id'] == state['active_topic_id'])
        row = next(r for r in rows if r['id'] == active['id'])
        result['next_action'] = ('review_linked_sources' if row['needs_review'] or error else
            'offer_next_topic' if row['status'] == 'completed' else
            'continue_existing_learning' if active['binding'] else 'prepare_selected_sources')
    return result


def topic_queue(args):
    import course
    path, state = load(args); incoming = getattr(args, 'event_file', None); replayed = False; event_topic_id = None
    if incoming:
        if course.root(args).is_relative_to(Path(__file__).resolve().parents[1]): raise ValueError('Queue must stay outside plugin')
        event = read_json(incoming); course.slug(event.get('request_id'))
        event_topic_id = 'topic-' + digest(event['request_id'])[:24] if event.get('action') == 'add' else event.get('topic_id')
        expected = getattr(args, 'expected_revision', None)
        if type(expected) is not int or expected < 0: raise ValueError('Queue write requires expected revision')
        source_lock = course.root(args) / '.study-units.lock'; checked(source_lock)
        with file_lock(source_lock):
            checked(source_lock)
            for folder in (path.parent.parent, path.parent): folder.mkdir(exist_ok=True); checked(folder, True)
            lock = path.with_suffix('.lock'); checked(lock)
            with file_lock(lock):
                checked(lock); path, state = load(args)
                receipt = state['receipts'].get(event['request_id'])
                if receipt is not None:
                    if receipt != digest(event): raise ValueError('Request ID already used with different data')
                    replayed = True
                else:
                    if expected != state['revision']: raise ValueError('Topic queue changed; read before writing')
                    candidate = apply(args, state, event); checked(path); atomic_json(path, candidate); state = candidate
    return dict(view(args, state), replayed=replayed, event_topic_id=event_topic_id)
