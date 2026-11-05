"""Per-course/per-learner permission, not a search service or proof of consent."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from atomic_files import atomic_json
from learning_tools import learner, read_json
from profile_storage import checked
from storage_lock import file_lock
from topic_research import fields, text

SCHEMA = 'career_market_v1'


def empty():
    return dict(schema=SCHEMA, revision=0, context=None,
                consent=dict(status='not_asked', statement=None, at=None), result=None)


def validate(state):
    fields(state, 'schema revision context consent result')
    if state['schema'] != SCHEMA or type(state['revision']) is not int or state['revision'] < 0:
        raise ValueError('Invalid market research state')
    context = state['context']
    if context is not None:
        fields(context, 'professional role region level')
        if type(context['professional']) is not bool: raise ValueError('Classify professional intent explicitly')
        for key in ('role', 'region', 'level'): text(context[key], key, nullable=key != 'role')
    consent = state['consent']; fields(consent, 'status statement at')
    if consent['status'] not in ('not_asked', 'granted', 'declined'): raise ValueError('Invalid market consent')
    if consent['status'] == 'not_asked':
        if consent['statement'] is not None or consent['at'] is not None: raise ValueError('Invalid unasked consent')
    else:
        if not context or not context['professional']: raise ValueError('Consent requires professional context')
        text(consent['statement'], 'learner decision'); text(consent['at'], 'decision time')
    result = state['result']
    if result is not None:
        fields(result, 'status note sample_id sample_sha256 recorded_at')
        if consent['status'] != 'granted' or not context['region'] or not context['level']:
            raise ValueError('Research result requires consent and known market/level')
        if result['status'] not in ('recorded', 'unavailable', 'insufficient'): raise ValueError('Invalid search result')
        text(result['note'], 'search limitations'); text(result['recorded_at'], 'result time')
        if result['status'] == 'recorded':
            import course
            from source_integrity import digest
            course.slug(result['sample_id']); digest(result['sample_sha256'])
        elif result['sample_id'] is not None or result['sample_sha256'] is not None:
            raise ValueError('Unavailable search cannot claim sample evidence')


def load(args):
    path = learner(args) / 'career-market.json'; checked(path)
    state = read_json(path, empty()); validate(state)
    return path, state


def sample_view(args, context, name):
    import career_evidence, course
    sample = career_evidence.career(SimpleNamespace(course=course.root(args), sample_id=name))
    card = sample.get('sample')
    if (not card or not sample.get('valid') or sample['freshness']['needs_review']
            or sample['current_vacancy_count'] < 2):
        raise ValueError('Need at least two intact current observations; use an ordinary roadmap otherwise')
    if any(card['scope'][key].strip().casefold() != context[key].strip().casefold()
           for key in ('role', 'region', 'level')):
        raise ValueError('Sample role, region or level differs from agreed search scope')
    repeated = [r for r in sample['requirements'] if not r['classification_conflicts']
                and sum(r['current_counts'][k] for k in ('required', 'preferred')) >= 2]
    if not repeated: raise ValueError('No supported recurring requirements; report insufficient data')
    return sample


def update(args, event, state):
    import course
    state = deepcopy(state)
    action = event.get('action')
    if action == 'context':
        fields(event, 'action professional role region level')
        context = {k: event[k] for k in ('professional', 'role', 'region', 'level')}
        old = state['context']
        # Filling unknown scope after consent is allowed; changing an agreed scope is not.
        changed = old and (any(old[k] != context[k] for k in ('professional', 'role'))
                          or any(old[k] is not None and old[k] != context[k] for k in ('region', 'level')))
        if changed: state.update(consent=empty()['consent'], result=None)
        state['context'] = context
    elif action == 'decision':
        fields(event, 'action decision statement')
        if event['decision'] not in ('granted', 'declined'): raise ValueError('Record an explicit grant or decline')
        state.update(consent=dict(status=event['decision'], statement=event['statement'], at=course.now()), result=None)
    elif action == 'result':
        fields(event, 'action status note sample_id')
        context = state['context']
        if (state['consent']['status'] != 'granted' or not context
                or not context['region'] or not context['level']):
            raise ValueError('Ask permission and clarify market/level before researching vacancies')
        checksum = None
        if event['status'] == 'recorded':
            checksum = sample_view(args, context, event['sample_id'])['sample']['content_sha256']
        state['result'] = dict(status=event['status'], note=event['note'], sample_id=event['sample_id'],
                               sample_sha256=checksum, recorded_at=course.now())
    else: raise ValueError('Unknown market research action')
    state['revision'] += 1; validate(state)
    return state


def market(args):
    import course
    path, state = load(args)
    incoming = getattr(args, 'market_file', None)
    if incoming:
        if course.root(args).is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError('Market decisions must stay outside the plugin')
        event = read_json(incoming)
        expected = getattr(args, 'expected_market_revision', None)
        if type(expected) is not int or expected < 0: raise ValueError('Market write requires expected market revision')
        # Shared source lock, then local permission lock; never acquire in reverse order.
        source_lock = course.root(args) / '.study-units.lock'; checked(source_lock)
        with file_lock(source_lock):
            checked(source_lock)
            for directory in (path.parent.parent, path.parent):
                directory.mkdir(exist_ok=True); checked(directory, True)
            lock = path.with_suffix('.lock'); checked(lock)
            with file_lock(lock):
                checked(lock); path, state = load(args)
                if expected != state['revision']: raise ValueError('Market decision changed; read before writing')
                candidate = update(args, event, state)
                checked(path); atomic_json(path, candidate); state = candidate
    result = deepcopy(state); context = state['context']; consent = state['consent']['status']
    result.update(may_search=False, should_offer=False, basis_valid=False,
                  requirement_groups=dict(frequent=[], additional=[]), next_action='ordinary_roadmap')
    if context is None: result['next_action'] = 'classify_direction'
    elif context['professional'] and consent == 'not_asked':
        result.update(should_offer=True, next_action='offer_market_research')
    elif context['professional'] and consent == 'granted':
        if not context['region'] or not context['level']: result['next_action'] = 'clarify_market_scope'
        elif state['result'] is None: result.update(may_search=True, next_action='search_with_available_tools')
        elif state['result']['status'] == 'recorded':
            try:
                sample = sample_view(args, context, state['result']['sample_id'])
                if sample['sample']['content_sha256'] != state['result']['sample_sha256']:
                    raise ValueError('Recorded sample changed; review before reuse')
                result.update(basis_valid=True, next_action='use_dated_sample',
                              sample_as_of=sample['sample']['as_of'], freshness=sample['freshness'],
                              limitations=sample['sample']['limitations'] + sample['limitations'])
                for row in sample['requirements']:
                    counts = row['current_counts']
                    if row['classification_conflicts'] or not (counts['required'] or counts['preferred']): continue
                    group = 'frequent' if counts['required'] and counts['required'] + counts['preferred'] >= 2 else 'additional'
                    links = [link for link in row['evidence'] if link['valid'] and link['current_sample']]
                    sources = [{k: link[k] for k in ('url', 'observed_on', 'start_line', 'end_line', 'classification')} for link in links[:3]]
                    result['requirement_groups'][group].append(dict(id=row['id'], title=row['title'], counts=counts,
                        sources=sources, omitted_source_links=max(0, len(links)-3)))
            except (ValueError, OSError) as error:
                result.update(next_action='offer_refresh_or_ordinary', warning=str(error))
    return result
