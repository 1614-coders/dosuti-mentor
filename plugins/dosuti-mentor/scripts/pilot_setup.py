"""One local pilot preparation flow, with diagnostics before a course exists.

No plugin installation, system Python installation, telemetry or automatic grading.
Existing courses/projects are not switched. Repeated runs preserve attempt history.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import uuid

from atomic_files import atomic_json, atomic_text
from profile_storage import checked
from storage_lock import file_lock

SCHEMA = 'pilot_setup_v1'
SCRIPTS = Path(__file__).resolve().parent
MAX_BYTES = 900000
MAX_OUTPUT = 160000


def now(): return datetime.now(timezone.utc).isoformat()


def safe_path(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Setup paths must not contain symbolic links')
    return path.resolve()


def read(root):
    from pilot_lifecycle import active
    active(root)
    path = root / 'setup.json';checked(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES: raise ValueError('Missing or oversized setup journal')
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('test_only') is not True
            or not isinstance(value.get('attempts'), list) or len(value['attempts']) > 30):
        raise ValueError('Invalid setup journal; preserve it before repair')
    for i, attempt in enumerate(value['attempts'], 1):
        if (not isinstance(attempt, dict) or attempt.get('number') != i or not isinstance(attempt.get('events'), list)
                or len(attempt['events']) > 20 or type(attempt['number']) is not int
                or attempt.get('status') not in ('running','passed','failed','interrupted')
                or not isinstance(attempt.get('environment'),dict)):
            raise ValueError('Invalid setup attempt history')
        for event in attempt['events']:
            if (not isinstance(event,dict) or event.get('status') not in ('running','passed','failed')
                    or any(not isinstance(event.get(k),str) or len(event[k])>8000
                           for k in ('step','observation','at'))):
                raise ValueError('Invalid setup event')
    return value


def save(root, value):
    if len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_BYTES:
        raise ValueError('Setup journal limit; keep it and start a separate pilot')
    checked(root / 'setup.json');atomic_json(root / 'setup.json', value)
    lines = ['# DoSuti Mentor: SETUP REPORT', '', 'TEST ONLY. Local observations; not Windows acceptance or ASR accuracy proof.',
             'Review personal paths/notes before sharing. No automatic upload.', '']
    from pilot import literal
    for attempt in value['attempts']:
        lines += [f"## Attempt {attempt['number']}: {attempt['status']}", '',
                  literal(json.dumps(attempt['environment'], ensure_ascii=False)), '']
        for event in attempt['events']:
            lines += [f"### {event['step']}: {event['status']}", '', literal(event['observation']), '']
            from resource_usage import markdown
            lines += markdown(event.get('resources')) + ['']
        if attempt.get('inventory'):
            lines += ['Dependency inventory:', literal(json.dumps(attempt['inventory'], ensure_ascii=False)), '']
    checked(root / 'SETUP_REPORT.md');atomic_text(root / 'SETUP_REPORT.md', '\n'.join(lines))


def redact(value):
    value = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', value)
    value = re.sub(r'(?i)((?:token|password|authorization|api[_-]?key)[=: ]+)[^\s]+', r'\1[redacted]', value)
    value = re.sub(r'(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+|sk-proj-[A-Za-z0-9_-]+)', '[redacted]', value)
    return value


def run(command, timeout, log_path=None, poll_interval=15):
    """Drain output continuously; retain only a bounded, redacted tail on disk."""
    import threading
    tail = bytearray()
    env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONUTF8='1', PIP_NO_INPUT='1', HF_HUB_DISABLE_TELEMETRY='1')
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                               start_new_session=os.name != 'nt')
    def consume():
        while True:
            chunk = process.stdout.read1(4096)
            if not chunk: break
            tail.extend(chunk)
            if len(tail) > MAX_OUTPUT: del tail[:-MAX_OUTPUT]
    def snapshot():
        output = redact(bytes(tail).decode('utf-8', errors='replace'))
        if log_path is not None:
            checked(log_path);atomic_text(log_path, output)
        return output
    thread = threading.Thread(target=consume, daemon=True);thread.start()
    def stop():
        if process.poll() is not None: return
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=15)
            else:
                import signal
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
        finally:
            if process.poll() is None: process.kill()
            process.wait(timeout=15)
    timed_out = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise subprocess.TimeoutExpired(command, timeout)
            try:
                process.wait(timeout=min(poll_interval, remaining));break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline: raise
                snapshot()
                print('[working] subprocess is still running; bounded step log updated', flush=True)
    except subprocess.TimeoutExpired:
        timed_out = True;stop()
    except BaseException:
        stop();raise
    finally:
        thread.join(timeout=5)
        if not thread.is_alive(): process.stdout.close()
    output = snapshot()
    return dict(returncode=process.returncode, timed_out=timed_out, output=output)


def inventory(output):
    import importlib
    import importlib.metadata as metadata
    for name in ('faster_whisper', 'ctranslate2', 'av', 'PIL', 'imageio_ffmpeg', 'certifi', 'onnxruntime', 'yt_dlp'):
        importlib.import_module(name)
    import imageio_ffmpeg
    import ctranslate2
    ffmpeg = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], capture_output=True, timeout=15)
    if ffmpeg.returncode: raise RuntimeError('FFmpeg failed to start')
    types = sorted(ctranslate2.get_supported_compute_types('cpu'))
    if 'int8' not in types: raise RuntimeError('CPU int8 inference unavailable')
    from maintenance import ocr_capability
    value = dict(python=platform.python_version(), system=platform.system(), machine=platform.machine(),
        packages={name: metadata.version(name) for name in ('faster-whisper','ctranslate2','av','Pillow',
                  'imageio-ffmpeg','certifi','onnxruntime','yt-dlp','numpy')}, cpu_compute_types=types,
        ffmpeg=ffmpeg.stdout.decode(errors='replace').splitlines()[0], screen_ocr=ocr_capability())
    atomic_json(output, value)


def smoke_check(folder, output):
    from source_integrity import verify_evidence, file_hash
    from profile_storage import checked
    for name in ('checkpoint.json', 'manifest.json', 'transcript.json', 'tutor_packet.json'): checked(folder / name)
    state = json.loads((folder / 'checkpoint.json').read_text(encoding='utf-8'))
    if state.get('status') != 'complete': raise ValueError('Smoke job not complete')
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    verify_evidence(folder, manifest, audio=True)
    packet = json.loads((folder / 'tutor_packet.json').read_text(encoding='utf-8'))
    if file_hash(folder / 'tutor_packet.json') != state['stages']['packet']['sha256']:
        raise ValueError('Smoke packet changed')
    if not packet['frames']: raise ValueError('Smoke extraction returned no frames')
    if not packet['speech']:
        raise ValueError('No recognized speech: choose a short interval with audible speech; quality is not yet checked')
    atomic_json(output, dict(status='passed', speech_records=len(packet['speech']), frames=len(packet['frames']),
        note='Actual CPU processing completed; transcript correctness and teaching require human review.'))


def video_worker(root, source, output, offline=False):
    """Register once in QA; reuse the same pinned course asset for smoke and lessons."""
    from types import SimpleNamespace
    import course
    import pilot
    from pilot_activity import course_scope
    root=pilot.checked_root(root);pilot.load(root)
    folder=root/'course'
    lesson='video-'+hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]
    directory=folder/'lessons'/lesson
    if not (directory/'lesson.json').exists():
        course.add_lesson(SimpleNamespace(course=folder,lesson=lesson,title='Первая лекция',video=source,order=1))
    data=course.read(directory/'lesson.json')
    if data['video']['source']!=source: raise ValueError('Registered source differs; preserve the course')
    with course_scope(folder,'prepare'):
        video,checksum=course.checked_video(directory,data,offline=offline)
    atomic_json(output,dict(path=str(video),sha256=checksum,lesson=lesson,course=str(folder)))


def prepare(args):
    root = safe_path(args.root)
    from pilot_lifecycle import active
    active(root)
    plugin = SCRIPTS.parent
    if root.is_relative_to(plugin) or plugin.is_relative_to(root):
        raise ValueError('Keep setup outside the plugin install tree')
    # Do not adopt an arbitrary pre-existing directory as our managed storage.
    from managed_storage import MARKER, validate, initialize, session
    if (root / 'setup.json').exists(): read(root)
    elif (root / MARKER).exists():
        validate(root)
        save(root, dict(schema=SCHEMA, test_only=True, owner=uuid.uuid4().hex, attempts=[]))
    elif root.exists(): read(root)  # Preserve refusal to adopt unrelated existing folders.
    else:
        root.mkdir(parents=True, exist_ok=False)
        save(root, dict(schema=SCHEMA, test_only=True, owner=uuid.uuid4().hex, attempts=[]))
    initialize(root)
    lock = root / '.setup.lock';checked(lock)
    with file_lock(lock, timeout=.1):
        active(root)
        value = read(root)
        if len(value['attempts']) >= 30: raise ValueError('Setup attempt limit reached')
        if value['attempts'] and value['attempts'][-1]['status'] == 'running':
            value['attempts'][-1]['status'] = 'interrupted'
        number = len(value['attempts']) + 1
        attempt = dict(number=number, status='running', started=now(), environment=dict(system=platform.system(),
            machine=platform.machine(), python=platform.python_version(),
            plugin_version=json.loads((SCRIPTS.parent/'plugin.json').read_text(encoding='utf-8'))['version']), events=[])
        value['attempts'].append(attempt);save(root, value)
        def step(name, command, timeout=900):
            event = dict(step=name, status='running', observation='Started', at=now())
            attempt['events'].append(event);save(root, value)
            print(f'[{name}] started', flush=True)
            started = time.monotonic()
            try:
                log = root / f'attempt-{number:02d}-{name}.log';checked(log)
                atomic_text(log, '')
                from resource_usage import measure
                def publish(metrics):
                    event['resources']=metrics
                    save(root,value)
                with session(root), measure(root,publish):
                    result = run(command, timeout, log_path=log)
                checked(log);atomic_text(log, result['output'])
                if result['returncode'] != 0 or result['timed_out']:
                    raise RuntimeError(f'{name} failed; returncode={result["returncode"]}; timeout={result["timed_out"]}; see {log.name}; output tail: {result["output"][-3000:]}')
                event.update(status='passed', observation=f'Completed in {time.monotonic()-started:.2f}s; log={log.name}')
            except Exception as error:
                event.update(status='failed', observation=redact(str(error))[:8000], seconds=round(time.monotonic()-started,6), finished=now(), timing_method='monotonic');save(root, value);raise
            event.update(seconds=round(time.monotonic()-started,6), finished=now(), timing_method='monotonic')
            save(root, value);print(f'[{name}] passed', flush=True)
        try:
            if sys.version_info < (3, 11): raise ValueError('Python3.11+ required; use3.12 for the pilot')
            if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 60:
                raise ValueError('Use a smoke interval of1..60seconds')
            if not math.isfinite(args.start) or args.start < 0: raise ValueError('Invalid smoke start')
            from storage_paths import source_kind
            remote=source_kind(str(args.video))=='remote'
            video=None if remote else safe_path(args.video)
            if video is not None:
                checked(video)
                if not video.is_file(): raise ValueError('Choose an existing local video with audible speech')
            runtime = safe_path(args.runtime or root / 'runtime')
            model = safe_path(args.model_path or root / 'models' / args.model)
            if any(p.is_relative_to(plugin) for p in (runtime, model)):
                raise ValueError('Runtime and model must be outside plugin cache')
            # External runtime/model paths are accepted for read-only offline reuse only.
            if not args.offline and (not runtime.is_relative_to(root) or not model.is_relative_to(root)):
                raise ValueError('Online setup manages only its own runtime/model; use --offline to reuse external paths')
            python = runtime / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            safe_path(runtime / 'venv')
            if not args.offline:
                step('dependencies', [sys.executable, '-B', str(SCRIPTS / 'setup_local.py'), '--runtime', str(runtime)])
            elif not python.is_file(): raise ValueError('Offline runtime missing; run online setup first')
            if remote:
                import pilot
                run_root=root/'run'
                if run_root.exists(): pilot.load(run_root)
                else: pilot.start(run_root)
                source_report=root/f'attempt-{number:02d}-source.json';checked(source_report)
                command=[str(python),'-B',str(Path(__file__)),'--worker','video','--root',str(run_root),
                         '--video',str(args.video),'--output',str(source_report)]
                if args.offline: command.append('--offline')
                step('video',command,3600)
                source_data=json.loads(source_report.read_text(encoding='utf-8'))
                video=safe_path(source_data['path']);checked(video)
                if not video.is_relative_to(run_root/'course') or not video.is_file():
                    raise ValueError('Downloaded video must remain inside the QA course')
                from source_integrity import file_hash
                if file_hash(video)!=source_data['sha256']: raise ValueError('Downloaded video changed before smoke')
                attempt['source']=dict(lesson=source_data['lesson'],sha256=source_data['sha256'])
                save(root,value)
            probe = root / f'attempt-{number:02d}-inventory.json';checked(probe)
            step('inventory', [str(python), '-B', str(Path(__file__)), '--worker', 'inventory', '--output', str(probe)])
            attempt['inventory'] = json.loads(probe.read_text(encoding='utf-8'));save(root, value)
            if not args.offline:
                step('model', [str(python), '-B', str(SCRIPTS / 'download_model.py'), args.model, '--output', str(model)], 3600)
            # Even offline mode validates the complete model before inference.
            step('model-check', [str(python), '-B', str(Path(__file__)), '--worker', 'model', '--model-path', str(model)])
            smoke = root / f'smoke-{number:02d}';checked(smoke, True)
            step('smoke', [str(python), '-B', str(SCRIPTS / 'lecture.py'), str(video), '--output', str(smoke),
                '--model', str(model), '--start', str(args.start), '--duration', str(args.seconds), '--frame-every', '10',
                '--language', args.language, '--cpu-mode', getattr(args, 'cpu_mode', 'standard')], 600)
            verification = root / f'attempt-{number:02d}-smoke.json';checked(verification)
            step('smoke-check', [str(python), '-B', str(Path(__file__)), '--worker', 'smoke', '--folder', str(smoke), '--output', str(verification)])
            import pilot
            from types import SimpleNamespace
            run_root = root / 'run'
            if run_root.exists(): pilot.load(run_root)
            else: pilot.start(run_root)
            import connection
            project = root / 'project'
            revision = connection.status(project)['revision'] if project.exists() else 0
            pilot.connect_project(run_root, project, revision, runtime, model)
            # Record actual environment success only; installation/onboarding stay unchecked.
            journal = pilot.load(run_root)
            pilot.record(run_root, journal['revision'], dict(step='environment', status='passed',
                observation=f'CPU smoke pipeline completed. Setup attempt{number}; dependency versions and earlier failures: ../SETUP_REPORT.md. ASR accuracy not established.', tester_feedback=''))
            attempt.update(status='passed', finished=now(), project=str(project), pilot=str(run_root));save(root, value)
            pilot.export(run_root)
            return dict(status='passed', project=str(project), pilot=str(run_root), setup_report=str(root / 'SETUP_REPORT.md'),
                report=str(run_root / 'TEST_REPORT.md'), next_step='Open this project in Codex; invoke $test-learning. New chats in the same project can discover this QA run.')
        except Exception as error:
            attempt.update(status='failed', finished=now(), error=redact(str(error))[:8000])
            if not attempt['events'] or attempt['events'][-1]['status'] != 'failed':
                attempt['events'].append(dict(step='setup', status='failed', observation=attempt['error'], at=now()))
            save(root, value)
            raise RuntimeError(f'{attempt["error"]}; partial report: {root / "SETUP_REPORT.md"}') from error


def main():
    if os.name == 'nt':
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--video', help='Local video path or public HTTP(S) lecture URL')
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--model-path', type=Path)
    parser.add_argument('--model', choices=['tiny','base','small','medium','large-v3'], default='small')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--language', default='ru')
    parser.add_argument('--cpu-mode', choices=['standard', 'economy'], default='standard')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--worker', choices=['inventory','smoke','model','video'], help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--folder', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker == 'inventory': inventory(args.output);return
        if args.worker == 'smoke': smoke_check(args.folder, args.output);return
        if args.worker == 'video': video_worker(args.root,args.video,args.output,args.offline);return
        if args.worker == 'model':
            from model_readiness import checked_model
            checked_model(args.model_path);return
        if args.root is None: parser.error('--root is required')
        if args.status: result = read(safe_path(args.root))
        else:
            if args.video is None: parser.error('--video is required')
            result = prepare(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print('Error:', error, file=sys.stderr);sys.exit(1)


if __name__ == '__main__': main()
