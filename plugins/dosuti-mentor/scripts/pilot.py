"""Isolated external acceptance run. No network, automatic grading or telemetry."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import os
import sys
import uuid
from atomic_files import atomic_json, atomic_text
from storage_lock import file_lock

STEPS = ('installation', 'environment', 'onboarding', 'route', 'preparation', 'teaching', 'practice', 'resume', 'feedback', 'removal')
STATUSES = ('passed', 'failed', 'skipped', 'blocked', 'not_checked')
MAX_BYTES = 900_000


def now():
    return datetime.now(timezone.utc).isoformat()


def checked_root(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Pilot path must not contain symbolic links')
    return path.resolve()


def load(root):
    if (root / '.cleanup-pending.json').exists():
        raise ValueError('Pilot cleanup is pending; resume cleanup instead of learning')
    path = root / 'pilot.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Missing, linked or oversized pilot journal')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema') not in ('dosuti_mentor_pilot_v1', 'lecture_tutor_pilot_v1') or data.get('test_only') is not True:
        raise ValueError('Not a pilot journal')
    if type(data.get('revision')) is not int or not isinstance(data.get('events'), list):
        raise ValueError('Invalid pilot journal')
    if data['revision'] != len(data['events']) or len(data['events']) > 200 or not isinstance(data.get('version'), str) or not isinstance(data.get('environment'), dict):
        raise ValueError('Invalid pilot history')
    for index, event in enumerate(data['events'], 1):
        if (not isinstance(event, dict) or event.get('revision') != index or
                event.get('step') not in STEPS or event.get('status') not in STATUSES or
                any(not isinstance(event.get(k), str) for k in ('at', 'observation', 'tester_feedback'))):
            raise ValueError('Invalid pilot event')
    if not isinstance(data.get('id'), str) or not data['id']:
        raise ValueError('Invalid pilot identity')
    if (root / 'course').is_symlink():
        raise ValueError('Pilot course must not be linked')
    return data


def start(path):
    root = checked_root(path)
    # Exclusive creation refuses any pre-existing directory, even an empty real course.
    root.mkdir(parents=True, exist_ok=False)
    version = json.loads(Path(__file__).parents[1].joinpath('plugin.json').read_text(encoding='utf-8'))['version']
    data = dict(schema='dosuti_mentor_pilot_v1', test_only=True, revision=0,
                id=uuid.uuid4().hex, created=now(), version=version,
                cleanup_owner=uuid.uuid4().hex, environment=dict(system=platform.system(), machine=platform.machine(), python=platform.python_version()), events=[])
    atomic_json(root / 'pilot.json', data)
    # Journal first: interrupted initialization is still reportable.
    from course import init
    from types import SimpleNamespace
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        init(SimpleNamespace(course=root / 'course', title='DoSuti Mentor — TEST ONLY'))
    return view(root, data)


def view(root, data):
    latest = {e['step']: e for e in data['events']}
    return dict(test_only=True, revision=data['revision'], course=str(root / 'course'),
                profile='pilot', steps={s: latest.get(s, dict(status='not_checked')) for s in STEPS},
                journal=data, note='Use only this course/profile for pilot learning. No automatic success or feedback collection.')


def record(path, revision, value):
    root = checked_root(path)
    if not isinstance(value, dict) or set(value) != {'step', 'status', 'observation', 'tester_feedback'}:
        raise ValueError('Expected step, status, observation, tester_feedback')
    if value['step'] not in STEPS or value['status'] not in STATUSES:
        raise ValueError('Unknown pilot step or status')
    for key in ('observation', 'tester_feedback'):
        if not isinstance(value[key], str) or len(value[key]) > 8000:
            raise ValueError('Notes must be strings up to 8000 characters')
    if value['status'] != 'not_checked' and not value['observation'].strip():
        raise ValueError('A status requires factual grounds')
    # Do not create lock directories when the journal does not exist.
    load(root)
    with file_lock(root / '.pilot.lock'):
        data = load(root)
        if data['revision'] != revision:
            raise ValueError('Pilot revision conflict; read status again')
        if len(data['events']) >= 200:
            raise ValueError('Pilot event limit reached; export report')
        data['revision'] += 1
        data['events'].append(dict(revision=data['revision'], at=now(), **value))
        if len(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_BYTES:
            raise ValueError('Pilot journal limit reached; export report')
        atomic_json(root / 'pilot.json', data)
    return view(root, data)


def literal(value):
    # Treat notes as text, not Markdown/HTML or an executable report instruction.
    return '\n'.join('    ' + line for line in str(value).splitlines()) or '    —'


def export(path, output=None):
    root = checked_root(path)
    data = load(root)
    lines = ['# DoSuti Mentor: TEST REPORT', '', 'Тестовый прогон. Не является подтверждением совместимости или качества всего продукта.', '',
             f"Версия: {data['version']}; ревизия отчёта: {data['revision']}", '',
             literal(json.dumps(data['environment'], ensure_ascii=False)), '',
             'Отчёт содержит вручную записанные наблюдения. passed не проверяется автоматически.',
             'Перед передачей проверь заметки на личные данные. Отправка только вручную.', '']
    for step, event in view(root, data)['steps'].items():
        lines += [f"## {step}: {event['status']}", '', 'Наблюдение:', literal(event.get('observation', '')), '',
                  'Отзыв человека (пусто, если не получен):', literal(event.get('tester_feedback', '')), '']
    lines += ['## История изменений', '']
    for event in data['events']:
        lines += [f"### {event['revision']}: {event['step']} / {event['status']} / {event['at']}", '',
                  literal(event['observation']), '', 'Отзыв человека:', literal(event['tester_feedback']), '']
    from pilot_report import sections, output_path
    lines += sections(root)
    output = output_path(root, output)
    if output.is_symlink():
        raise ValueError('Report must not be linked')
    atomic_text(output, '\n'.join(lines))
    return dict(report=str(output), revision=data['revision'], automatically_sent=False)


PROJECT_SCHEMA = 'dosuti_pilot_project_v1'


def project_marker(project):
    from profile_storage import checked
    project = checked_root(project)
    checked(project, True)
    path = project / '.dosuti-pilot.json'
    checked(path)
    if not path.exists(): return None
    if path.stat().st_size > 16000: raise ValueError('Pilot project marker too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or set(value) != {'schema', 'pilot_path'}
            or value['schema'] != PROJECT_SCHEMA or not isinstance(value['pilot_path'], str)
            or not value['pilot_path'].strip()):
        raise ValueError('Invalid pilot project marker; preserve it before repair')
    root = checked_root(project / value['pilot_path'])
    load(root)
    return root


def connect_project(path, project, expected_revision, runtime=None, model_path=None):
    """Explicit connection to a dedicated empty or already-owned test project."""
    from types import SimpleNamespace
    import connection
    from profile_storage import checked
    import os
    root = checked_root(path);load(root)
    project = checked_root(project)
    if (project.is_relative_to(Path(__file__).resolve().parents[1])
            or project.is_relative_to(root / 'course') or root.is_relative_to(project)):
        raise ValueError('Choose a separate dedicated test project outside the course/plugin')
    project.mkdir(parents=True, exist_ok=True);checked(project, True)
    if project_marker(project) is None and any(p.name != '.pilot-project.lock' for p in project.iterdir()):
        raise ValueError('Use an empty dedicated test project; existing project files are preserved')
    lock = project / '.pilot-project.lock';checked(lock)
    with file_lock(lock, timeout=.1):
        checked(lock)
        owner = project_marker(project)
        if owner is not None and owner != root: raise ValueError('Project belongs to another pilot')
        current = connection.status(project)
        if type(expected_revision) is not int or current['revision'] != expected_revision:
            raise ValueError('Connection revision conflict; read project status again')
        selected = current.get('binding') or {}
        if (current['enabled'] and (selected.get('course_path') != str(root / 'course')
                                   or selected.get('profile') != 'pilot')):
            raise ValueError('Project is connected to another course; do not replace it for QA')
        if owner is None and any(p.name != '.pilot-project.lock' for p in project.iterdir()):
            raise ValueError('Use an empty dedicated test project; existing project files are preserved')
        if owner is None:
            try: relative = os.path.relpath(root, project)
            except ValueError: relative = str(root)
            # Marker first allows recovery if connection writing is interrupted.
            atomic_json(project / '.dosuti-pilot.json', dict(schema=PROJECT_SCHEMA, pilot_path=relative))
        if current['enabled']:
            for key, requested in (('runtime', runtime), ('model_path', model_path)):
                if requested is not None and selected.get(key) != str(Path(requested).expanduser().resolve()):
                    raise ValueError('Project runtime/model differs; update the connection explicitly')
        if not current['enabled']:
            connection.dispatch(SimpleNamespace(project=project, action='connect', scope='project', chat=None,
                course=root / 'course', profile='pilot', runtime=runtime, model_path=model_path,
                expected_revision=expected_revision))
        return project_status(project)


def project_status(project, chat=None):
    import connection
    project = checked_root(project)
    if not project.is_dir(): raise ValueError('Project directory missing')
    root = project_marker(project)
    if root is None: return dict(test_only=False, connected=False, reason='No pilot marker in this project')
    selected = connection.status(project, chat)
    binding = selected.get('binding') or {}
    matches = (selected['enabled'] and selected['course_status'] == 'available'
               and binding.get('course_path') == str(root / 'course') and binding.get('profile') == 'pilot'
               and not selected['needs_chat_id'])
    return dict(test_only=True, connected=matches, pilot=str(root), project=str(project),
                connection=selected, **({'run': view(root, load(root))} if matches else
                {'reason': 'Respect the current chat/project connection; do not switch courses automatically.'}))


def main():
    if os.name == 'nt':
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'status', 'record', 'export', 'connect-project', 'project-status', 'activity-start', 'activity-finish', 'activity-status', 'assessment-plan', 'assessment-answer', 'assessment-status', 'cleanup-plan', 'cleanup-apply'))
    parser.add_argument('--pilot', type=Path)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--chat')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stage')
    parser.add_argument('--session', default='')
    parser.add_argument('--detail', default='')
    parser.add_argument('--activity')
    parser.add_argument('--result', choices=('passed','failed','interrupted'), default='passed')
    parser.add_argument('--plan-file', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--model-path', type=Path)
    parser.add_argument('--expected-revision', type=int)
    parser.add_argument('--event-file', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'project-status':
            if args.project is None: raise ValueError('project-status requires --project')
            result = project_status(args.project, args.chat)
        elif args.action == 'connect-project':
            if args.pilot is None or args.project is None: raise ValueError('connect-project requires --pilot and --project')
            result = connect_project(args.pilot, args.project, args.expected_revision, args.runtime, args.model_path)
        elif args.pilot is None: raise ValueError('--pilot is required')
        elif args.action == 'start': result = start(args.pilot)
        elif args.action == 'status':
            root = checked_root(args.pilot)
            result = view(root, load(root))
        elif args.action == 'export': result = export(args.pilot, args.output)
        elif args.action.startswith('activity-'):
            import pilot_activity as activity
            root = checked_root(args.pilot)
            if args.action == 'activity-start': result = activity.start(root,args.stage,args.session,args.detail)
            elif args.action == 'activity-finish': result = activity.finish(root,args.activity,args.result,args.detail)
            else: result = activity.read(root)
        elif args.action.startswith('assessment-'):
            import pilot_assessment as assessment
            root = checked_root(args.pilot)
            if args.action == 'assessment-status': result = assessment.read(root)
            else:
                if args.event_file is None or args.event_file.stat().st_size>200000: raise ValueError('Bounded --event-file required')
                result = assessment.record(root,args.expected_revision,args.action.split('-')[1],json.loads(args.event_file.read_text(encoding='utf-8')))
        elif args.action.startswith('cleanup-'):
            import pilot_cleanup as cleanup
            if args.action == 'cleanup-plan': result = cleanup.plan(args.pilot,args.output)
            else: result = cleanup.apply(args.pilot,args.plan_file)

        else:
            if args.expected_revision is None or args.event_file is None:
                raise ValueError('record requires --expected-revision and --event-file')
            if args.event_file.stat().st_size > 70000:
                raise ValueError('Event file too large')
            result = record(args.pilot, args.expected_revision, json.loads(args.event_file.read_text(encoding='utf-8')))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, RuntimeError, RecursionError) as error:
        print('Error:', error, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
