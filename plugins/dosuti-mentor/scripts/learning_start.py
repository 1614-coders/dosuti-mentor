"""Explicit start: preserve bindings; isolate new projects in managed courses."""
import argparse
import contextlib
import io
import json
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import connection
from bootstrap import default_home
from managed_storage import safe, validate
from profile_storage import profile_path
from storage_lock import file_lock


def existing(project, chat):
    state = connection.dispatch(NS(project=project, action='status', chat=chat))
    data, _ = connection.read(project)
    if state['enabled'] or state['needs_chat_id'] or connection.KEY in data or 'course_path' in data:
        return state
    return None


def learner_context(state, home=None):
    if state.get('course_status') == 'available':
        from learner_preferences import seed
        from managed_storage import find_home, MARKER
        if home is None:
            home = find_home(Path(state['binding']['course_path']))
            if home is None:
                candidate = default_home()
                if (candidate / MARKER).is_file(): home = validate(safe(candidate))
        args = NS(course=Path(state['binding']['course_path']), profile=state['binding']['profile'], learner_home=home,
                  profiles_dir=Path(state['binding']['profiles_dir']) if state['binding'].get('profiles_dir') else None)
        seed(args)
        if home is not None: state = dict(state, learner_home=str(home))
    return state


CHOICE_MESSAGE = 'Этот проект (чат) будет продолжением существующего курса или начнём новый?'
CHOICES = ['Продолжить существующий курс.', 'Создать новый курс.', 'Работать без привязки к курсу.']


def known_courses(home, project, profile):
    """Only configured bindings of this learner; do not scan other courses or chats."""
    from learning_tools import read_json
    result = {}
    for location in dict.fromkeys(p for p in (home, project) if p is not None):
        data, state = connection.read(location)
        values = list(state['chats'].values())
        if state['project_enabled']: values.append({k: data[k] for k in connection.FIELDS if k in data} | {'profile': data.get('profile', 'default')})
        for value in values:
            if value is None or value['profile'] != profile: continue
            binding = connection.resolve(location, value)
            try:
                connection.course_ready(binding)
                meta = read_json(Path(binding['course_path']) / 'course.json')
            except (ValueError, OSError, TypeError, KeyError): continue
            identity = json.dumps(binding, sort_keys=True)
            result[identity] = dict(course_path=binding['course_path'], title=meta['title'][:300], profile=profile, binding=binding)
    return list(result.values())


def choice_state(state, home, profile, chat):
    return dict(state, enabled=False, binding=None, course_status='choice_required',
                learner_home=str(home), profile=profile, start_action='choose_course',
                start_message=CHOICE_MESSAGE, choices=CHOICES, needs_chat_id=chat is None)


def bound_state(state, home, requested_profile=None):
    if (requested_profile is not None and state.get('binding')
            and state['binding']['profile'] != requested_profile):
        raise ValueError('Chat is bound to another learner; choose a binding explicitly')
    action = ('connection_unavailable' if state['enabled'] and state['course_status'] != 'available'
              else 'continue' if state['enabled'] else 'without_course')
    return dict(learner_context(state, home), start_action=action)


def start(project=None, chat=None, home=None, *, choice=None, course=None, profile=None, expected_revision=None):
    requested_profile = profile
    if choice not in (None, 'existing', 'new', 'none'): raise ValueError('Invalid course choice')
    chat = chat if chat is not None else os.environ.get('CODEX_THREAD_ID') or None
    from course import slug
    if chat is not None: slug(chat)
    project = Path(project).expanduser().resolve() if project is not None else None
    explicit = existing(project, chat) if project is not None else None
    if explicit is not None and choice is None and explicit['course_status'] != 'available':
        return dict(explicit, start_action='without_course' if explicit['explicitly_disabled'] else 'connection_unavailable')
    if explicit is not None:
        if profile is None or not explicit.get('binding') or explicit['binding']['profile'] == profile:
            explicit = learner_context(explicit, home)
        home = home or explicit.get('learner_home')
    home = validate(safe(home if home is not None else default_home()))
    if home.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Learning data must stay outside the installed plugin')
    profile = profile or (explicit['binding']['profile'] if explicit and explicit.get('binding') else 'default')
    slug(profile)
    location = project if explicit is not None else home
    project_key = 'project-' + hashlib.sha256(os.path.normcase(str(project)).encode('utf-8')).hexdigest()[:32] if project else 'managed-default'
    key = chat if explicit is not None else ('context-' + hashlib.sha256((project_key + ':' + chat).encode()).hexdigest()[:32] if chat else project_key)
    _, bindings = connection.read(location)
    state = connection.status(location, key)
    if chat is not None and key in bindings['chats'] and choice is None:
        return bound_state(state, home, requested_profile)
    legacy = connection.status(home)
    if requested_profile is None and explicit is None and legacy.get('binding'):
        profile = legacy['binding']['profile']
    if legacy.get('course_status') == 'available' and legacy['binding']['profile'] == profile:
        learner_context(legacy, home)
    from learner_preferences import read as read_shared
    _, shared = read_shared(NS(course=home / 'courses/default', profile=profile, learner_home=home))
    has_profile = bool(shared)
    if explicit and explicit.get('binding'):
        args = NS(course=Path(explicit['binding']['course_path']), profile=profile)
        from learning_tools import pref_file
        has_profile = has_profile or pref_file(args, 'profile').is_file()
        if not has_profile and choice is None: return learner_context(explicit, home)
    if choice is None and has_profile:
        return choice_state(state, home, profile, chat)
    if choice == 'existing' and course is None:
        return dict(state, learner_home=str(home), start_action='select_course',
                    courses=known_courses(home, project, profile), needs_chat_id=chat is None)
    if choice is not None and chat is None: raise ValueError('A real chat ID is required to save a chat choice')
    if choice is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ValueError('Course choice requires the current connection revision')
    with file_lock(safe(home / '.learning-start.lock')):
        _, current = connection.read(location)
        expected = current['revision'] if choice is None else expected_revision
        if current['revision'] != expected: raise ValueError('Connections changed; read start before choosing')
        if choice is None and chat is not None and key in current['chats']:
            return bound_state(connection.status(location, key), home, requested_profile)
        if choice is None:
            _, fresh = read_shared(NS(course=home / 'courses/default', profile=profile, learner_home=home))
            if fresh: return choice_state(connection.status(location, key), home, profile, chat)
        if choice == 'none':
            result = connection.dispatch(NS(project=location, action='disconnect', scope='chat', chat=key, expected_revision=expected))
            return dict(result, learner_home=str(home), start_action='without_course',
                        start_message='Работаем без привязки к курсу и без изменения учебного прогресса.')
        selected = {}
        if choice == 'existing':
            root = Path(course).expanduser().resolve()
            matches = [item['binding'] for item in known_courses(home, project, profile) if item['course_path'] == str(root)]
            if len(matches) > 1: raise ValueError('Multiple configured bindings; choose their settings explicitly before connecting')
            selected = matches[0] if matches else {}
            connection.course_ready(dict(course_path=str(root), profile=profile))
        else:
            import uuid
            root = safe(home / 'courses' / ('course-' + uuid.uuid4().hex if choice == 'new' else project_key if project else 'default'))
            if not (root / 'course.json').exists():
                if root.exists() and any(root.iterdir()): raise ValueError('Course folder is not empty; preserve existing files')
                import course as course_module
                with contextlib.redirect_stdout(io.StringIO()): course_module.init(NS(course=root, title='Моё обучение'))
            profile_path(root, profile).mkdir(parents=True, exist_ok=True)
            if not has_profile:
                from learner_preferences import publish
                publish(NS(course=root, profile=profile, learner_home=home))
            if choice == 'new' and has_profile:
                from atomic_files import atomic_json
                atomic_json(profile_path(root, profile) / 'preferences.json', dict(values={}, revision=0, onboarding='not_asked',
                    course_setup=dict(settings='pending', questions=[], pending=[])))
        result = connection.dispatch(NS(project=location, action='connect', scope='chat' if chat else 'project', chat=key if chat else None,
            expected_revision=expected, course=root, profile=profile, runtime=Path(selected.get('runtime', home / 'runtime')),
            model_path=Path(selected['model_path']) if selected.get('model_path') else None,
            profiles_dir=Path(selected['profiles_dir']) if selected.get('profiles_dir') else None))
        result = dict(learner_context(result, home), start_action='continue' if choice == 'existing' else 'new_course')
        if choice == 'existing':
            result['start_message'] = 'Что именно хочешь изучать или делать в рамках этого курса в новом проекте?'
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--chat')
    parser.add_argument('--home', type=Path)
    parser.add_argument('--choice', choices=['existing', 'new', 'none'])
    parser.add_argument('--course', type=Path)
    parser.add_argument('--profile')
    parser.add_argument('--expected-revision', type=int)
    args = parser.parse_args()
    try:
        print(json.dumps(start(args.project, args.chat, args.home, choice=args.choice, course=args.course, profile=args.profile, expected_revision=args.expected_revision), ensure_ascii=False))
    except (ValueError, OSError, RuntimeError) as error:
        print('Cannot start learning: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
