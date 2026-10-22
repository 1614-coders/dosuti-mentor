"""Explicit start: preserve connections, otherwise use the managed default course."""
import argparse
import contextlib
import io
import json
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


def start(project=None, chat=None, home=None):
    if project is not None:
        state = existing(Path(project), chat)
        if state is not None:
            return state  # Includes disabled, broken and legacy explicit connections.
    home = validate(safe(home if home is not None else default_home()))
    if home.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Learning data must stay outside the installed plugin')
    lock = safe(home / '.learning-start.lock')
    with file_lock(lock):
        initial_revision = connection.status(home, chat)['revision']
        state = existing(home, chat)
        if state is not None:
            return state
        root = safe(home / 'courses/default')
        if not (root / 'course.json').exists():
            if root.exists() and any(root.iterdir()):
                raise ValueError('Default course folder is not empty; preserve it before choosing another course')
            import course
            with contextlib.redirect_stdout(io.StringIO()):
                course.init(NS(course=root, title='Моё обучение'))
        value = dict(course_path=str(root), profile='default')
        connection.course_ready(value)
        profile_path(root, 'default').mkdir(parents=True, exist_ok=True)
        return connection.dispatch(NS(project=home, action='connect', scope='project', chat=chat,
            expected_revision=initial_revision, course=root, profile='default',
            runtime=home / 'runtime'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--chat')
    parser.add_argument('--home', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(start(args.project, args.chat, args.home), ensure_ascii=False))
    except (ValueError, OSError, RuntimeError) as error:
        print('Cannot start learning: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
