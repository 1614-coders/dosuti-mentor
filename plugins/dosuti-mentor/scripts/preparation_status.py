"""Whole prepare attempt, with a worker overlay; teaching readiness stays in the chat."""
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from atomic_files import atomic_json
from processing_status import Reporter, LABELS, describe as worker_status
from storage_lock import file_lock
from managed_storage import safe

_CURRENT = ContextVar('preparation_reporter', default=None)


def stamp(path):
    info = Path(path).stat()
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def inputs_match(directory, inputs):
    if not isinstance(inputs, dict) or not 1 <= len(inputs) <= 2000: return False
    try:
        for name, signature in inputs.items():
            path = safe(directory / name)
            if Path(name).is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                return False
            if stamp(path) != signature: return False
        return True
    except (OSError, TypeError, ValueError):
        return False


def phase(stage, **details):
    from preparation_control import check_cancel
    check_cancel()
    report = _CURRENT.get()
    if report is not None:
        report.set(stage, **details)


def tracked(function=None, *, complete_stage='review'):
    if function is None:
        return lambda target: tracked(target, complete_stage=complete_stage)
    @wraps(function)
    def wrapped(args):
        import course
        from compact_lesson import require_media_open
        directory = course.lesson_path(args)
        require_media_open(directory)
        base = safe(directory / '.preparation')
        attempt = uuid.uuid4().hex
        folder = base / attempt
        folder.mkdir(parents=True)
        with file_lock(folder / '.processing.lock'):
            report = Reporter(folder)
            data = course.read(directory / 'lesson.json')
            report.data.update(source_url_sha256=hashlib.sha256(data['video']['source'].encode()).hexdigest(),
                               start=getattr(args, 'start', 0), requested_duration=getattr(args, 'duration', None))
            report.set('download')
            with file_lock(directory / '.lesson.lock'):
                atomic_json(base / 'current.json', dict(attempt=attempt))
            token = _CURRENT.set(report)
            from preparation_control import REQUEST, check_cancel
            cancel_token = REQUEST.set(folder / 'cancel.json')
            try:
                result = function(args)
                check_cancel()
                report.set(complete_stage, 'complete')
                return result
            except BaseException as error:
                report.set(report.data['stage'], 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed', error)
                raise
            finally:
                REQUEST.reset(cancel_token)
                _CURRENT.reset(token)
    return wrapped


def describe(directory):
    directory = Path(directory)
    base = directory / '.preparation'
    try:
        base = safe(base)
        index = safe(base / 'current.json')
        if base.is_symlink() or index.is_symlink() or not index.is_file():
            return dict(status='unavailable')
        if index.stat().st_size > 4096: raise ValueError('Oversized preparation index')
        attempt = json.loads(index.read_text(encoding='utf-8'))['attempt']
        if not isinstance(attempt, str) or not re.fullmatch('[a-f0-9]{32}', attempt):
            raise ValueError('Invalid preparation attempt')
        folder = safe(base / attempt)
        if folder.is_symlink(): raise ValueError('Linked preparation attempt')
        result = worker_status(folder)
        if result['status'] == 'unavailable': return result
        import course
        data = course.read(directory / 'lesson.json')
        if result.get('source_url_sha256') != hashlib.sha256(data['video']['source'].encode()).hexdigest():
            return dict(status='unavailable', reason='source_changed')
        if result.get('source_sha256') and data['video'].get('sha256') != result['source_sha256']:
            return dict(status='unavailable', reason='source_changed')
        key = result.get('worker_key')
        if key and result['stage'] == 'worker':
            working = directory / '.jobs' / course.slug(key)
            child = worker_status(working) if working.resolve().is_relative_to(directory.resolve()) else {}
            fresh = child.get('started_at', 0) >= result['started_at']
            if child.get('status') == 'running' or (fresh and child.get('status') == 'failed' and result['status'] == 'failed'):
                result.update(stage=child['stage'], label=child['label'], status=child['status'])
                if child['status'] == 'running':
                    result['elapsed_seconds'] = round(max(0, time.time() - result['started_at']), 2)
                    result.pop('elapsed_is_last_observed', None)
                if child.get('error'): result['error'] = child['error']
            elif fresh and result['status'] == 'interrupted' and child.get('status') == 'interrupted':
                result.update(stage=child['stage'], label=LABELS[child['stage']])
        ready = result['stage'] == 'ready' and result['status'] == 'complete'
        inputs = result.get('review_inputs', {})
        if result['stage'] in ('frames_review', 'code_review', 'ready') and not inputs_match(directory, inputs):
            result.update(stage='review', status='complete', label='Материалы изменились после сверки; требуется новая проверка')
            ready = False
        if result.get('chat_reported') and result['status'] == 'reported':
            result['elapsed_seconds'] = round(max(0, time.time() - result['started_at']), 2)
        result.update(schema='preparation_status_v1', teaching_ready=ready, percent=None,
                      elapsed_scope='current_prepare_attempt')
        if result.get('chat_reported'):
            result['elapsed_scope'] = 'wall_since_attempt_start_including_waits'
            result['readiness_basis'] = 'assistant_report_for_scope_not_automatic_proof'
            result['freshness_check'] = 'metadata_only_reopen_context_before_teaching'
        if result['status'] in ('failed', 'interrupted'):
            result['message'] = 'Остановка: ' + result['label'] + '. ' + result.get('error', 'Процесс завершился без записи результата; причина неизвестна.')
        else:
            result['message'] = result['label']
        result['attempt_id'] = attempt
        result['cancellation_requested'] = (folder / 'cancel.json').is_file()
        if result['cancellation_requested'] and result['status'] == 'running':
            result['message'] = 'Останавливаю подготовку; дождись подтверждения interrupted. ' + result['label']
        result.pop('review_inputs', None)  # Keep potentially large dependency lists out of chat/tool output.
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return dict(status='unavailable')


def cancel(args):
    """Signal this exact live attempt without waiting for its exclusive QA lock."""
    import course
    directory = course.lesson_path(args)
    view = describe(directory)
    if view.get('attempt_id') != args.attempt:
        raise ValueError('Preparation attempt changed; read preparation-status before cancelling')
    if view['status'] != 'running':
        return dict(requested=False, status=view['status'], attempt_id=args.attempt)
    folder = safe(directory / '.preparation' / args.attempt)
    request = safe(folder / 'cancel.json')
    # Do not recreate parents if cleanup won a race after the live status read.
    try:
        with request.open('x', encoding='utf-8') as stream:
            json.dump(dict(requested_at=time.time()), stream)
    except FileExistsError:
        pass
    return dict(requested=True, attempt_id=args.attempt, status='cancellation_requested',
                message='Запрос остановки принят. Дождись interrupted: проверки файлов и публикация могут завершаться не сразу.')


def record(args):
    """Record the assistant's actual chat work after checking the selected evidence."""
    import course
    from lecture_context import assemble, number
    directory = course.lesson_path(args)
    view = describe(directory)
    if view.get('status') not in ('complete', 'reported'):
        raise ValueError('Finish automatic preparation before recording chat review')
    base = directory / '.preparation'
    attempt = json.loads((base / 'current.json').read_text(encoding='utf-8'))['attempt']
    if attempt != view.get('attempt_id') or not re.fullmatch('[a-f0-9]{32}', attempt):
        raise ValueError('Preparation attempt changed; read the current status')
    folder = base / attempt  # describe validated the current attempt; recheck under lock below.
    with file_lock(folder / '.processing.lock', timeout=0):
        if json.loads((base / 'current.json').read_text(encoding='utf-8'))['attempt'] != attempt:
            raise ValueError('Preparation attempt changed; read the current status')
        data = course.read(directory / 'lesson.json')
        start, window = number(args.time, 'time'), number(args.window, 'window')
        if start < 0 or window <= 0: raise ValueError('Require nonnegative time and positive window')
        scope = [start, start + window]
        context = None
        if args.step == 'ready':
            context = assemble(directory, data, start, window)
            if not context['coverage']['complete']:
                raise ValueError('Prepare missing intervals before marking this scope')
            code = data['code'].get('status') == 'provided' or any(
                row.get('status') == 'provided' for row in data.get('code_repositories', {}).values())
            allowed = ('code_review',) if code else ('frames_review', 'code_review')
            if view['stage'] not in allowed or view.get('review_scope') != scope:
                raise ValueError('Record the actual frames/code review for this exact scope before ready')
        paths = {directory / 'lesson.json'}
        if context is not None:
            paths.update(Path(source[key]) for source in context['sources'] for key in ('packet_file', 'notes_file'))
            paths.update(Path(frame['absolute_path']) for frame in context['frames'])
            paths.update(Path(row['absolute_file']) for row in context['code_stages'] if not row['stale'])
        if len(paths) > 2000: raise ValueError('Select a smaller teaching scope for chat review')
        inputs = {}
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError('Chat review status requires evidence inside the lesson')
            inputs[path.relative_to(directory).as_posix()] = stamp(path)
        saved = json.loads((folder / 'processing-status.json').read_text(encoding='utf-8'))
        saved.update(stage=args.step, label=LABELS[args.step],
                     status='complete' if args.step == 'ready' else 'reported',
                     elapsed_seconds=round(max(0, time.time() - saved['started_at']), 2),
                     chat_reported=True, review_scope=scope, review_inputs=inputs)
        if len(json.dumps(saved, ensure_ascii=False).encode('utf-8')) > 60000:
            raise ValueError('Select a smaller teaching scope for the bounded status')
        atomic_json(folder / 'processing-status.json', saved)
    return describe(directory)
