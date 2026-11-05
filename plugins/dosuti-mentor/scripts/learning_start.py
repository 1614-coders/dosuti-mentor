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


def known_courses(home, project, profile, *, include_unavailable=False, errors=None):
    """Only configured bindings of this learner; do not scan other courses or chats."""
    from learning_tools import read_json
    result = {}
    for location in dict.fromkeys(p for p in (home, project) if p is not None):
        try:
            data, state = connection.read(location)
            if not isinstance(data,dict):raise ValueError('Invalid course connection object; preserve settings before repair')
        except (ValueError,OSError,TypeError,KeyError,AttributeError) as error:
            if errors is None:raise
            errors.append(dict(location=str(location),reason=str(error)));continue
        values = list(state['chats'].values())
        if state['project_enabled']: values.append({k: data[k] for k in connection.FIELDS if k in data} | {'profile': data.get('profile', 'default')})
        for value in values:
            if value is None or value['profile'] != profile: continue
            binding = dict(value)
            reason=None;meta={}
            try:
                binding = connection.resolve(location, value)
                connection.course_ready(binding)
                meta = read_json(Path(binding['course_path']) / 'course.json')
                if (not isinstance(meta,dict) or type(meta.get('schema_version')) is not int or meta['schema_version']!=1
                        or any(not isinstance(meta.get(k),str) or not meta[k].strip() for k in ('id','title'))):
                    raise ValueError('Course metadata changed or is invalid')
            except (ValueError, OSError, RuntimeError, TypeError, KeyError, AttributeError) as error:
                if not include_unavailable:continue
                reason=str(error);meta={}
            identity = json.dumps(binding, sort_keys=True)
            result[identity] = dict(course_path=binding['course_path'], title=meta['title'][:300] if not reason else None, profile=profile, binding=binding)
            if include_unavailable:result[identity].update(available=reason is None,reason=reason,course_id=meta.get('id'))
    return list(result.values())


def choice_state(state, home, profile, chat):
    return dict(state, enabled=False, binding=None, course_status='choice_required',
                learner_home=str(home), profile=profile, start_action='choose_course',
                start_message=CHOICE_MESSAGE, choices=CHOICES, needs_chat_id=chat is None)


def check_profile(state, requested_profile):
    if (requested_profile is not None and state.get('binding')
            and state['binding']['profile'] != requested_profile):
        raise ValueError('Chat is bound to another learner; choose a binding explicitly')


def bound_state(state, home, requested_profile=None):
    check_profile(state, requested_profile)
    action = ('connection_unavailable' if state['enabled'] and state['course_status'] != 'available'
              else 'continue' if state['enabled'] else 'without_course')
    result=dict(learner_context(state, home), start_action=action)
    if action=='continue':result['next_action']='study_next'
    return result


def start(project=None, chat=None, home=None, *, choice=None, course=None, profile=None, expected_revision=None):
    requested_profile = profile
    if choice not in (None, 'existing', 'new', 'none'): raise ValueError('Invalid course choice')
    chat = chat if chat is not None else os.environ.get('CODEX_THREAD_ID') or None
    from course import slug
    if chat is not None: slug(chat)
    project = Path(project).expanduser().resolve() if project is not None else None
    explicit = existing(project, chat) if project is not None else None
    if explicit is not None and choice is None:
        check_profile(explicit, requested_profile)
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
        if not has_profile and choice is None: return bound_state(explicit, home, requested_profile)
    if choice is None and has_profile:
        return choice_state(state, home, profile, chat)
    if choice == 'existing' and course is None:
        from course_catalog import list_courses
        catalog=list_courses(home=home,project=project,profile=profile)
        return dict(state, learner_home=str(home), start_action='select_course',
                    courses=catalog['courses'],catalog_valid=catalog['valid'],catalog_errors=catalog['errors'],
                    needs_chat_id=chat is None)
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
            result['next_action']='study_next'
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--chat')
    parser.add_argument('--home', type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--choice', choices=['existing', 'new', 'none'])
    mode.add_argument('--list-courses',action='store_true',help='Read known courses of an explicitly selected learner without changing bindings')
    parser.add_argument('--limit',type=int,default=10)
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--course', type=Path)
    parser.add_argument('--profile')
    parser.add_argument('--expected-revision', type=int)
    args = parser.parse_args()
    if args.list_courses and (args.course is not None or args.expected_revision is not None):
        parser.error('--list-courses cannot be combined with course selection or a write revision')
    if not args.list_courses and (args.limit!=10 or args.offset):parser.error('--limit/--offset require --list-courses')
    try:
        if args.list_courses:
            from course_catalog import list_courses
            result=list_courses(home=args.home,project=args.project,profile=args.profile,limit=args.limit,offset=args.offset)
        else:result=start(args.project, args.chat, args.home, choice=args.choice, course=args.course, profile=args.profile, expected_revision=args.expected_revision)
        print(json.dumps(result,ensure_ascii=False))
    except (ValueError, OSError, RuntimeError) as error:
        print('Cannot start learning: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
