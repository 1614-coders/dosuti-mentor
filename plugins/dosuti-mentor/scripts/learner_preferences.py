"""Only explicit, general preferences are shared inside one managed learner home."""
from pathlib import Path
from atomic_files import atomic_json
from storage_lock import file_lock

GENERAL = {'style', 'format', 'detail', 'checks', 'communication_language'}
ANSWERS = {'learning_format', 'assistant_role', 'check_frequency', 'explanation_detail'}


def path_for(args):
    from managed_storage import find_home, safe, validate
    from course import root, slug
    explicit = getattr(args, 'learner_home', None)
    home = validate(safe(explicit)) if explicit else find_home(root(args))
    if home is None: return None
    return safe(home / 'learner-preferences' / slug(getattr(args, 'profile', 'default')) / 'preferences.json')


def read(args):
    from learning_tools import read_json, validate_preferences, onboarding_answers, revision
    from profile_storage import checked
    path = path_for(args)
    if path is None: return None, {}
    checked(path)
    data = read_json(path, {})
    if data:
        if (not isinstance(data.get('values', {}), dict)
                or not isinstance(data.get('onboarding_answers', {}), dict)
                or set(data) - {'schema', 'revision', 'values', 'onboarding', 'onboarding_answers'}
                or data.get('schema') != 'learner_preferences_v1'
                or data.get('onboarding') not in ('not_asked', 'complete', 'skipped')
                or set(data.get('values', {})) - GENERAL
                or set(data.get('onboarding_answers', {})) - ANSWERS):
            raise ValueError('Invalid shared learner preferences; preserve the file before repair')
        validate_preferences(data.get('values', {})); onboarding_answers(data.get('onboarding_answers', {}))
        revision(data)
    return path, data


def publish(args, values=None, answers=None, finished=None, reset=False, seed=False, expected=None):
    from learning_tools import revision
    from managed_storage import safe
    path, _ = read(args)
    if path is None:
        if expected is not None: raise ValueError('No managed learner home selected')
        return
    for folder in (path.parent.parent, path.parent):
        safe(folder).mkdir(mode=0o700, parents=True, exist_ok=True)
    from profile_storage import checked
    lock = safe(path.with_suffix('.lock')); checked(lock)
    with file_lock(lock):
        _, current = read(args)
        if expected is not None and revision(current) != expected:
            raise ValueError('Learner preferences changed; read settings before updating')
        if seed and current.get('onboarding') in ('complete', 'skipped'): return
        merged = {} if reset else dict(current.get('values', {}))
        incoming = {k: v for k, v in (values or {}).items() if k in GENERAL}
        merged = incoming | merged if seed else merged | incoming
        records = dict(current.get('onboarding_answers', {}))
        incoming_answers = {k: v for k, v in (answers or {}).items() if k in ANSWERS}
        records = incoming_answers | records if seed else records | incoming_answers
        state = finished if finished in ('complete', 'skipped') else current.get('onboarding', 'not_asked')
        saved = dict(schema='learner_preferences_v1', revision=revision(current) + 1,
                     values=merged, onboarding=state, onboarding_answers=records)
        atomic_json(path, saved)


def seed(args):
    """Explicit start can adopt the selected old profile; reads never migrate files."""
    from learning_tools import pref_file, read_json, validate_preferences, onboarding_answers
    _, shared = read(args)
    if shared.get('onboarding') in ('complete', 'skipped'): return
    local = read_json(pref_file(args, 'profile'), {})
    if local.get('course_setup') is not None: return  # Course-specific choices never seed general preferences.
    if local.get('onboarding') in ('complete', 'skipped'):
        publish(args, validate_preferences(local.get('values', {})),
                onboarding_answers(local.get('onboarding_answers', {})), local['onboarding'], seed=True)


COURSE_QUESTIONS = ('outcome', 'subject', 'self_assessment', 'constraints')
SETTING_QUESTIONS = ('learning_format', 'assistant_role', 'check_frequency', 'explanation_detail')


def course_setup(value):
    if value is None: return None
    if (not isinstance(value, dict) or set(value) != {'settings', 'questions', 'pending'}
            or value['settings'] not in ('pending', 'keep', 'change')):
        raise ValueError('Invalid course setup state')
    for key in ('questions', 'pending'):
        rows = value[key]
        if not isinstance(rows, list) or any(not isinstance(q, str) or q not in SETTING_QUESTIONS for q in rows) or len(rows) != len(set(rows)):
            raise ValueError('Invalid course setting questions')
    if set(value['pending']) - set(value['questions']): raise ValueError('Invalid pending course settings')
    return value
