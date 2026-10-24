"""Local course storage and processing. No paid APIs and no automatic teaching."""
import argparse
import difflib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlparse, quote
import uuid
import zipfile
from storage_lock import file_lock
from managed_storage import managed_operation, find_home, session
from preparation_status import tracked, phase
from preparation_control import run as preparation_run
from storage_paths import source_kind, local_path, anchored, resolved
from source_integrity import (digest, source_matches, verify_evidence, check_local_video,
                              seal_snapshot, verify_snapshot)
from read_snapshot import observed, watch

SCRIPT_DIR = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def slug(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}', value):
        raise ValueError('IDs must contain letters, digits, _ or -, and start with a letter or digit')
    return value


@observed
def read(path):
    path = Path(path)
    value = json.loads(path.read_text(encoding='utf-8'))
    return resolved(value, path) if path.name == 'lesson.json' else value


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name == 'lesson.json':
        value = anchored(value, path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2))
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def update_lesson(directory, change):
    """Apply a small mutation to fresh state, never a stale processing snapshot."""
    with file_lock(directory / '.lesson.lock'):
        data = read(directory / 'lesson.json')
        change(data)
        data['revision'] = data.get('revision', 0) + 1
        data['updated'] = now()
        save(directory / 'lesson.json', data)


def material_history(current):
    previous = dict(current)
    history = list(previous.pop('history', []))
    if previous.get('status') not in (None, 'not_asked'):
        history.append(previous)
    return history


def homework_revision(homework):
    if homework.get('status') == 'provided' and homework.get('source_kind') == 'lecture_speech':
        return homework['revision']
    if homework.get('status') == 'provided' and homework.get('sha256'):
        return 'sha256:' + homework['sha256']
    return homework.get('status', 'not_asked')


def homework_integrity(homework):
    if homework.get('status') != 'provided':
        return 'not_provided'
    try:
        path = Path(homework['file'])
        if path.is_symlink() or not path.is_file():
            return 'missing_or_linked'
        if file_hash(path) != digest(homework.get('sha256'), 'Homework SHA256'):
            return 'changed'
    except (OSError, ValueError, KeyError):
        return 'unverifiable'
    return 'verified'


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def root(args):
    return args.course.expanduser().resolve()


def lesson_path(args):
    directory = root(args) / 'lessons' / slug(args.lesson)
    if not (directory / 'lesson.json').exists():
        raise ValueError('Lesson is not registered')
    return directory


def init(args):
    directory = root(args)
    with file_lock(directory / '.course.lock'):
        if (directory / 'course.json').exists():
            raise ValueError('Course already exists; use status')
        save(directory / 'course.json', {'schema_version': 1, 'id': uuid.uuid4().hex,
             'title': args.title, 'created': now(), 'processing': 'local',
             'teaching': 'Codex chat', 'default_model': 'small', 'default_profile': 'default'})
    print(directory)


def db(args):
    from profile_storage import open_progress
    connection = open_progress(root(args), slug(getattr(args, 'profile', 'default')))
    connection.row_factory = sqlite3.Row
    connection.executescript('''
        CREATE TABLE IF NOT EXISTS progress (
          lesson TEXT NOT NULL, topic TEXT NOT NULL, stage TEXT NOT NULL,
          details TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (lesson, topic));
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, lesson TEXT NOT NULL, topic TEXT NOT NULL,
          stage TEXT NOT NULL, details TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bindings (chat TEXT PRIMARY KEY, lesson TEXT NOT NULL);
    ''')
    # Migrate a profile lazily. BEGIN IMMEDIATE also serializes two first readers.
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        for table in ['progress', 'events']:
            columns = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
            if 'homework_revision' not in columns:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN homework_revision TEXT')
    return connection


def visible_progress(rows, lessons):
    """Keep attempts intact, but never expose an obsolete pass as a current pass."""
    result = []
    integrity = {key: homework_integrity(lesson.get('homework', {})) for key, lesson in lessons.items()}
    for row in rows:
        state = dict(row)
        homework = lessons.get(state['lesson'], {}).get('homework', {})
        current_revision = homework_revision(homework)
        assessment = state['homework_revision'] is not None or state['stage'] == 'homework_passed'
        stale = assessment and (homework.get('status') != 'provided' or
                               state['homework_revision'] != current_revision or
                               integrity.get(state['lesson']) != 'verified')
        state['invalidated'] = stale
        if assessment:
            state['homework_integrity'] = integrity.get(state['lesson'], 'missing_lesson')
        if stale:
            state['previous_stage'] = state['stage']
            state['stage'] = 'needs_review'
            state['details'] = 'Homework changed, unavailable or revision unknown; review current task'
            state['current_homework_revision'] = current_revision
        result.append(state)
    return result


def read_progress(args):
    """Read legacy SQLite without creating a profile, migrating tables or writing."""
    from profile_storage import profile_path, checked
    path=profile_path(root(args),getattr(args,'profile','default'))/'progress.sqlite3'
    result=dict(progress=[],events=[],event_count=0,chat_bindings=[])
    for suffix in ('','-journal','-wal','-shm'):watch(Path(str(path)+suffix))
    if checked(path) is None:return result
    for suffix in ('-journal','-wal','-shm'):checked(Path(str(path)+suffix))
    # SQLite readers can write WAL shared-memory bookkeeping even with mode=ro.
    # Preserve such databases and report the limitation instead of touching them.
    with path.open('rb') as stream:header=stream.read(20)
    if header[:16]==b'SQLite format 3\x00' and header[18:20]!=b'\x01\x01':
        raise ValueError('WAL lesson progress requires an external checkpoint before read-only reporting')
    try:connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    except sqlite3.Error as error:raise ValueError('Cannot read lesson progress: '+str(error)) from error
    connection.row_factory=sqlite3.Row
    try:
        tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'progress' in tables:
            result['progress']=[dict(homework_revision=None,**dict(row)) if 'homework_revision' not in row.keys() else dict(row)
                                for row in connection.execute('SELECT * FROM progress ORDER BY updated,lesson,topic')]
        if 'events' in tables:
            result['event_count']=connection.execute('SELECT count(*) FROM events').fetchone()[0]
            result['events']=[dict(row) for row in connection.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')][::-1]
        if 'bindings' in tables:
            result['chat_bindings']=[dict(row) for row in connection.execute('SELECT * FROM bindings ORDER BY chat')]
    except sqlite3.Error as error:
        raise ValueError('Cannot read lesson progress: '+str(error)) from error
    finally:connection.close()
    return result


def bind(args):
    lesson_path(args)
    with db(args) as connection:
        connection.execute('INSERT INTO bindings VALUES (?, ?) ON CONFLICT(chat) DO UPDATE SET lesson=excluded.lesson',
                           (args.chat, args.lesson))
    print('Chat bound to course/profile; progress is shared with other bindings')


def context(args):
    from learning_tools import preferences, cursor
    from lecture_context import assemble
    directory = lesson_path(args)
    data = read(directory / 'lesson.json')
    if getattr(args, 'topic_id', None):
        from semantic_context import select
        selected = select(directory, data, args.topic_id)
    else:
        selected = assemble(directory, data, args.time, args.window)
    data = dict(data, homework=selected['homework'])
    selected['homework'] = dict(data['homework'], integrity=homework_integrity(data['homework']))
    selected['preferences'] = preferences(args)
    from learning_intent import view as intent_view
    selected['learning_intent'] = intent_view(args)
    selected['learning_checkpoint'] = cursor(args)
    selected['learning_checkpoint']['applies_to_lesson'] = (
        (selected['learning_checkpoint'].get('state') or {}).get('lesson') == args.lesson)
    selected['learner_progress'] = visible_progress(
        [row for row in read_progress(args)['progress'] if row['lesson']==args.lesson],{args.lesson:data})
    print(json.dumps(selected, ensure_ascii=False, indent=2))


def frame(args):
    import imageio_ffmpeg
    from lecture_context import number
    directory = lesson_path(args)
    data = read(directory / 'lesson.json')
    if number(args.time, 'Frame time') < 0:
        raise ValueError('Time must be nonnegative')
    video, checksum = checked_video(directory, data)
    output = directory / 'evidence' / 'frames' / f'{round(args.time*1000):012d}.jpg'
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                    '-y', '-ss', str(args.time), '-i', str(video), '-frames:v', '1',
                    '-q:v', '2', str(output)], check=True)
    if file_hash(video) != checksum:
        output.unlink(missing_ok=True)
        raise ValueError('Video changed during frame extraction')
    if not output.exists():
        raise ValueError('No frame at requested time; check video duration')
    print(output)


def add_lesson(args):
    read(root(args) / 'course.json')
    directory = root(args) / 'lessons' / slug(args.lesson)
    with file_lock(directory / '.lesson.lock'):
        if (directory / 'lesson.json').exists():
            raise ValueError('Lesson already exists; use attach to add missing materials')
        save(directory / 'lesson.json', {'schema_version': 1, 'id': args.lesson, 'title': args.title,
             'created': now(), 'order': args.order, 'revision': 0,
             'video': {'source': args.video if source_kind(args.video) == 'remote' else str(local_path(args.video)), 'status': 'registered'},
             'code': {'status': 'not_asked'}, 'homework': {'status': 'not_asked'},
             'resources': {'status': 'not_asked', 'items': []}, 'prepared_fragments': []})
        source = local_path(args.video) if source_kind(args.video) == 'local' else None
        if source is not None and source.is_file():
            data = read(directory / 'lesson.json')
            checksum = file_hash(source)
            data['video'].update(path=str(source.resolve()), sha256=checksum,
                                 revision='sha256:' + checksum)
            save(directory / 'lesson.json', data)
    print(directory)


def scan_screen(args):
    from screen_evidence import scan, bounds, scan_settings
    bounds(args.start,args.duration,args.sample_every)
    if args.duration>300 or args.duration/args.sample_every>1200:
        raise ValueError('Scan at most 300 seconds and 1200 sample intervals')
    directory=lesson_path(args)
    video,checksum=checked_video(directory,read(directory/'lesson.json'))
    settings, key = scan_settings(checksum, args.start, args.duration, args.sample_every)
    chosen = []
    def select(latest):
        jobs = latest.setdefault('screen_jobs', {})
        generation = jobs.get(key, {}).get('generation')
        if generation is None or getattr(args, 'rebuild', False):
            generation = uuid.uuid4().hex[:12]
        jobs[key] = dict(generation=generation, status='pending')
        chosen.append(generation)
    update_lesson(directory, select)
    generation = chosen[0]
    matching=[old for old in read(directory/'lesson.json').get('screen_scans',[]) if
              old.get('scan_key') == key and old.get('generation') == generation]
    try:
        folder=scan(video,directory/'evidence/screen-scans',args.start,args.duration,args.sample_every,checksum,
                    rebuild=getattr(args,'rebuild',False),preferred=matching[-1]['folder'] if matching else None,
                    generation=generation)
    except Exception as error:
        def failed(latest):
            if latest['screen_jobs'][key]['generation'] == generation:
                latest['screen_jobs'][key].update(status='failed',error=str(error)[:300])
        update_lesson(directory,failed)
        raise
    receipt=read(folder/'screen.json')
    entry=dict(folder=str(folder),source_sha256=checksum,requested_interval=receipt['requested_interval'],
               receipt_sha256=file_hash(folder/'screen.json'),status='screen_needs_review',
               scan_key=read(folder/'completion.json')['key'],sample_every=args.sample_every,generation=generation)
    activated = []
    def publish(latest):
        source_matches(receipt,latest['video']['sha256'])
        if latest['screen_jobs'][key]['generation'] != generation:
            return
        old_scans=latest.get('screen_scans',[])
        retired=[old for old in old_scans if old['folder'] != str(folder) and
                 old.get('sample_every') == args.sample_every and old['requested_interval'] == entry['requested_interval']]
        if retired:latest.setdefault('retired_screen_scans',[]).extend(retired)
        latest['screen_scans']=[old for old in old_scans if old['folder']!=str(folder) and old not in retired]+[entry]
        latest['retired_screen_scans']=[old for old in latest.get('retired_screen_scans',[]) if old['folder'] != str(folder)]
        latest['screen_jobs'][key]['status']='complete'
        activated.append(True)
    update_lesson(directory,publish)
    print('Screen evidence prepared:' if activated else 'Superseded scan saved but not activated:',folder,'samples:',len(receipt['frames']),
          'review candidates:',sum(f['review_candidate'] for f in receipt['frames']))


def github_url(source):
    url = urlparse(source)
    return url.scheme == 'https' and url.hostname == 'github.com' and len(url.path.strip('/').split('/')) == 2


def attach(args):
    repository = getattr(args, 'repository', None)
    if repository is not None:
        from code_repositories import repository_id
        repository_id(repository)
        if args.kind != 'code':
            raise ValueError('--repository is only valid for code')
    if not args.none and args.source and args.kind in ('code','homework'):
        remote=source_kind(args.source)=='remote'
        html_homework=args.kind=='homework' and Path(args.source).suffix.lower() in ('.html','.htm')
        if (remote and not (args.kind=='code' and github_url(args.source))) or html_homework:
            from material_sources import attach_material
            return attach_material(args)
    return attach_local(args)


def attach_local(args):
    directory = lesson_path(args)
    material = None
    item = None
    if args.none:
        material = {'status': 'absent', **({'items': []} if args.kind == 'resources' else {})}
    elif not args.source:
        raise ValueError('Provide --source or --none')
    elif args.kind == 'code':
        target = directory / 'code' / uuid.uuid4().hex[:12]
        if github_url(args.source):
            command = ['git', 'clone', '--depth', '1']
            if args.ref:
                command += ['--branch', args.ref]
            subprocess.run(command + ['--', args.source, str(target)], check=True)
            commit = subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip()
            origin = {'url': args.source, 'commit': commit, 'ref': args.ref}
        else:
            source = Path(args.source).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if target.is_relative_to(source):
                    raise ValueError('Code source cannot contain the course storage')
                shutil.copytree(source, target, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '.venv', 'venv', 'node_modules', 'target', '__pycache__'))
            elif source.is_file() and zipfile.is_zipfile(source):
                with zipfile.ZipFile(source) as archive:
                    from material_sources import validate_archive
                    validate_archive(archive)
                    for entry in archive.infolist():
                        destination = (target / entry.filename).resolve()
                        if not destination.is_relative_to(target.resolve()) or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError('Archive contains unsafe paths or symbolic links')
                    archive.extractall(target)
            elif source.is_file():
                target.mkdir()
                shutil.copyfile(source, target / source.name)
            else:
                raise ValueError('Provide a code folder, archive, file or GitHub repository URL')
            origin = getattr(args,'material_origin',None) or {'path': str(source)}
        material = {'status': 'provided', 'snapshot': str(target), 'origin': origin,
                    'lecture_match': 'unverified', 'revision': uuid.uuid4().hex}
        # Links are copied as links, then rejected before opening any target file.
        if (target / '.git').is_dir():
            shutil.rmtree(target / '.git')
        inventory, checksum = seal_snapshot(target)
        material.update(inventory=inventory, content_sha256=checksum,
                        integrity='inventory_recorded', sealed_at=now())
    elif args.kind == 'homework':
        source = Path(args.source).expanduser().resolve()
        if not source.is_file():
            raise ValueError('Save homework instructions as a local file first')
        target = directory / 'homework' / (uuid.uuid4().hex[:8] + '-' + source.name)
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        material = {'status': 'provided', 'file': str(target), 'sha256': file_hash(target),
                    'origin': getattr(args,'material_origin',None) or {'source':str(source)}}
        target.chmod(target.stat().st_mode & ~0o222)
    else:
        if source_kind(args.source) == 'remote':
            item = {'source': args.source, 'kind': args.resource_type, 'status': 'registered_not_read'}
        else:
            source = Path(args.source).expanduser().resolve()
            if not source.is_file():
                raise ValueError('Resource must be an HTTPS URL or a local file')
            target = directory / 'resources' / (uuid.uuid4().hex[:8] + '-' + source.name)
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            item = {'source': str(target), 'kind': args.resource_type, 'status': 'downloaded',
                    'origin': {'source':str(source),'raw_file':str(target),'sha256':file_hash(target),'size':target.stat().st_size,'media_type':''}}
            target.chmod(target.stat().st_mode & ~0o222)
        item['id'] = uuid.uuid4().hex
    if args.kind == 'homework':
        material['revision'] = homework_revision(material)

    def publish(data):
        if item is not None:
            data['resources'] = {'status': 'provided',
                                 'items': data['resources'].get('items', []) + [item]}
        else:
            repository = getattr(args, 'repository', None)
            if args.kind == 'code' and repository is not None:
                repositories = data.setdefault('code_repositories', {})
                material['history'] = material_history(repositories.get(repository, {}))
                repositories[repository] = material
                return
            if args.kind in ('code', 'homework'):
                material['history'] = material_history(data[args.kind])
            data[args.kind] = material

    update_lesson(directory, publish)
    print('Attached:', args.kind)


def download_video(directory, data, fragments=4, site_timeout=1800, quality='best'):
    from video_sources import acquire
    return acquire(directory, data['video'], fragments, site_timeout, quality)


def checked_video(directory, data, fragments=4, site_timeout=1800, quality='best', offline=False):
    from compact_lesson import require_media_open
    require_media_open(directory)
    if offline and source_kind(data['video']['source'])=='remote':
        from video_sources import cached
        video=cached(directory/'video',data['video'])
        if video is None: raise ValueError('Offline video cache is unavailable; retry online or provide a local file')
    else:
        video = download_video(directory, data, fragments, site_timeout, quality)
    checksum = file_hash(video)
    expected = data['video'].get('sha256')
    if expected and checksum != digest(expected, 'Registered video SHA256'):
        raise ValueError('Video changed since registration; register it as a new lesson')
    def publish(latest):
        if latest['video']['source'] != data['video']['source']:
            raise ValueError('Video source changed during processing')
        pinned = latest['video'].get('sha256')
        if pinned and pinned != checksum:
            raise ValueError('Video changed since registration; register it as a new lesson')
        latest['video'].update(status='available', path=str(video), sha256=checksum,
                               revision='sha256:' + checksum)
    update_lesson(directory, publish)
    return video, checksum


@managed_operation
@tracked
def prepare(args):
    from model_readiness import checked_model, ModelNotReady, repair_type
    from publication import publish_job
    directory = lesson_path(args)
    from compact_lesson import require_media_open, receipt
    require_media_open(directory)
    if ((directory/'compact.json').exists() and receipt(directory)['state']=='reopened'
            and not getattr(args,'rebuild',False)):
        raise ValueError('Reopened compact lesson requires prepare --rebuild; retained context remains available')
    data = read(directory / 'lesson.json')
    base_key = job_key = None
    try:
        if args.import_prepared:
            phase('publish')
            source = args.import_prepared.resolve()
            imported_manifest = read(source / 'manifest.json')
            if not isinstance(imported_manifest, dict):
                raise ValueError('Imported manifest must be an object')
            digest(imported_manifest.get('source_sha256'), 'Imported source SHA256')
            if not data['video'].get('sha256'):
                phase('download')
                checked_video(directory, data, getattr(args,'download_fragments',4), getattr(args,'site_timeout',1800), getattr(args,'video_quality','best'))
                data = read(directory / 'lesson.json')
                phase('publish')
            check_local_video(data['video'])
            source_matches(imported_manifest, data['video']['sha256'])
            verify_evidence(source, imported_manifest)
            target = directory / 'prepared' / ('import-' + uuid.uuid4().hex[:8])
            target.mkdir(parents=True)
            for name in ['manifest.json', 'transcript.json']:
                shutil.copyfile(source / name, target / name)
            shutil.copytree(source / 'frames', target / 'frames')
            if (source / 'visual_notes.json').exists():
                shutil.copyfile(source / 'visual_notes.json', target / 'visual_notes.json')
            verify_evidence(target, read(target / 'manifest.json'))
            from tutor_packet import build_packet
            build_packet(target)
        else:
            from asr_vocabulary import recognition_prompt
            prompt = recognition_prompt(getattr(args, 'prompt', ''), getattr(args, 'vocabulary_file', None))
            video, checksum = checked_video(directory, data, getattr(args,'download_fragments',4), getattr(args,'site_timeout',1800), getattr(args,'video_quality','best'))
            duration = args.duration
            if duration is None:
                from media_input import probe_media
                duration = probe_media(video)['video']['end'] - args.start
            if duration <= 0:
                raise ValueError('Fragment duration must be positive')
            home = find_home(root(args))
            phase('model_install', source_sha256=checksum, actual_duration=duration)
            models = home / 'models' if home else root(args) / '.runtime/models'
            model_path = args.model_path.resolve() if args.model_path else models / args.model
            try:
                model_info = checked_model(model_path)
            except ModelNotReady:
                preparation_run([sys.executable, str(SCRIPT_DIR / 'download_model.py'), repair_type(model_path, args.model),
                                '--output', str(model_path)], check=True)
                model_info = checked_model(model_path)
            settings = dict(source_sha256=checksum, start=float(args.start), duration=float(duration),
                            frame_every=float(args.frame_every), model=model_info['fingerprint'],
                            language=args.language, prompt=prompt, pipeline_version=7,
                            audio_track=getattr(args,'audio_track',None), audio_channel=getattr(args,'audio_channel',None))
            base_key = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
            selection = []
            def choose_job(latest):
                jobs = latest.setdefault('processing_jobs', {})
                key = jobs.get(base_key, {}).get('job_key', base_key)
                if getattr(args, 'rebuild', False):
                    key = base_key + '-' + uuid.uuid4().hex[:8]
                jobs[base_key] = dict(job_key=key, settings=settings, status='pending')
                selection.append(key)
            update_lesson(directory, choose_job)
            job_key = selection[0]
            working = directory / '.jobs' / job_key
            command = [sys.executable, str(SCRIPT_DIR / 'lecture.py'), str(video), '--output', str(working),
                       '--start', str(args.start), '--duration', str(duration), '--frame-every', str(args.frame_every),
                       '--model', str(model_path), '--language', args.language]
            command += ['--cpu-mode', getattr(args, 'cpu_mode', 'standard')]
            if prompt:
                command += ['--prompt', prompt]
            for field in ('audio_track','audio_channel'):
                if getattr(args,field,None) is not None:
                    command += ['--'+field.replace('_','-'), str(getattr(args,field))]
            command += ['--resume']
            recent = read(directory / 'lesson.json')
            if (working / 'payload-cleaned.json').exists():
                phase('publish')
                from job_cleanup import cleaned_publication
                target = cleaned_publication(working, directory, recent, job_key)
            else:
                phase('worker', worker_key=job_key)
                preparation_run(command, check=True)
                phase('publish')
                recent = read(directory / 'lesson.json')
                matching = [f for f in recent.get('retired_fragments', []) + recent['prepared_fragments']
                            if f.get('job_key') == job_key]
                target = publish_job(working, directory / 'prepared', job_key,
                                     preferred=matching[-1]['folder'] if matching else None)
        packet = read(target / 'tutor_packet.json')
        latest = read(directory / 'lesson.json')
        check_local_video(latest['video'])
        source_matches(packet, latest['video']['sha256'])
        verify_evidence(target, read(target / 'manifest.json'))
        fragment = {'folder': str(target), 'coverage': packet['coverage'],
                    'source_sha256': packet['source_sha256'],
                    'packet_sha256': file_hash(target / 'tutor_packet.json'),
                    'status': 'prepared_needs_review'}
        if not args.import_prepared:
            fragment['job_key'] = job_key
            fragment['job_base'] = base_key
        activated = []
        deferred = []
        def publish(latest):
            source_matches(packet, latest['video'].get('sha256'))
            if not args.import_prepared and latest['processing_jobs'][base_key]['job_key'] != job_key:
                return
            from fragment_selection import defer_to_selection
            disposition = defer_to_selection(latest, fragment)
            if disposition is not None:
                if not args.import_prepared:
                    latest['processing_jobs'][base_key].update(status='complete', activation_status=disposition)
                latest.pop('last_processing_error', None)
                (activated if disposition == 'active' else deferred).append(True)
                return
            retired = [f for f in latest['prepared_fragments']
                       if f.get('source_sha256') != packet['source_sha256'] or
                       (fragment.get('job_base') and f.get('job_base', f.get('job_key')) == fragment['job_base'] and f['folder'] != str(target))]
            if retired:
                latest.setdefault('retired_fragments', []).extend(retired)
            latest['prepared_fragments'] = [f for f in latest['prepared_fragments']
                if f['folder'] != str(target) and f not in retired] + [fragment]
            latest['retired_fragments'] = [f for f in latest.get('retired_fragments',[]) if f['folder'] != str(target)]
            if not args.import_prepared:
                latest['processing_jobs'][base_key].update(status='complete', activation_status='active')
            latest.pop('last_processing_error', None)
            activated.append(True)
        update_lesson(directory, publish)
        message = ('Prepared for assistant:' if activated else
                   'Prepared candidate; explicit selection preserved:' if deferred else
                   'Superseded job saved but not activated:')
        print(message, target)
    except (Exception, KeyboardInterrupt) as error:
        def failed(latest):
            if job_key is not None:
                current=latest.get('processing_jobs',{}).get(base_key,{})
                if current.get('job_key') != job_key:
                    return
                current.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                               error=type(error).__name__ + ': ' + str(error)[:300])
            latest['last_processing_error']=type(error).__name__ + ': ' + str(error)[:300]
        update_lesson(directory,failed)
        raise


def progress(args):
    directory = lesson_path(args)
    revision = getattr(args, 'homework_revision', None)
    with file_lock(directory / '.lesson.lock'):
        from spoken_homework import resolve as resolve_homework
        homework = resolve_homework(directory, read(directory / 'lesson.json'))
        if args.stage == 'homework_passed' and not revision:
            raise ValueError('homework_passed requires --homework-revision from the reviewed task')
        if revision is not None:
            if homework.get('status') != 'provided':
                raise ValueError('No homework provided; record independent practice separately')
            if revision != homework_revision(homework):
                raise ValueError('Homework changed; read the current task before saving assessment')
            if homework_integrity(homework) != 'verified':
                raise ValueError('Homework content changed or is unavailable; attach the trusted task and review again')
        with db(args) as connection:
            previous = connection.execute(
                'SELECT stage,homework_revision FROM progress WHERE lesson=? AND topic=?',
                (args.lesson, args.topic)).fetchone()
            if revision is None and previous and (previous['homework_revision'] is not None or
                                                  previous['stage'] == 'homework_passed'):
                raise ValueError('This topic tracks homework; provide --homework-revision or use a separate practice topic')
            connection.execute('''INSERT INTO progress(lesson,topic,stage,details,updated,homework_revision)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(lesson,topic) DO UPDATE SET
                stage=excluded.stage, details=excluded.details, updated=excluded.updated,
                homework_revision=excluded.homework_revision''',
                (args.lesson, args.topic, args.stage, args.details, now(), revision))
            connection.execute('INSERT INTO events(lesson,topic,stage,details,created,homework_revision) VALUES (?, ?, ?, ?, ?, ?)',
                               (args.lesson, args.topic, args.stage, args.details, now(), revision))
            if revision is not None and homework_integrity(homework) != 'verified':
                raise ValueError('Homework changed during assessment; no result saved')
    from learning_intent import view as intent_view
    print(json.dumps(dict(saved=True,profile=args.profile,
                          learning_progress=intent_view(args)['learning_progress']),ensure_ascii=False,indent=2))


def diff_code(args):
    from code_repositories import current as repositories
    name = getattr(args, 'repository', None) or 'default'
    current = repositories(read(lesson_path(args) / 'lesson.json')).get(name, {})
    previous_path = root(args) / 'lessons' / slug(args.previous) / 'lesson.json'
    previous = repositories(read(previous_path)).get(name, {})
    if not current.get('snapshot') or not previous.get('snapshot'):
        raise ValueError('Both lessons need teacher code snapshots')
    verify_snapshot(previous)
    verify_snapshot(current)
    before, after = Path(previous['snapshot']), Path(current['snapshot'])
    def paths(directory):
        return {str(p.relative_to(directory)): p for p in directory.rglob('*')
                if p.is_file() and not p.is_symlink() and '.git' not in p.parts}
    old, new = paths(before), paths(after)
    result = {'before': args.previous, 'after': args.lesson, 'lecture_match': 'unverified', 'changes': []}
    for name in sorted(set(old) | set(new)):
        if args.file and name != args.file:
            continue
        left = old[name].read_bytes() if name in old else b''
        right = new[name].read_bytes() if name in new else b''
        if left == right:
            continue
        change = {'file': name, 'status': 'modified' if name in old and name in new else ('added' if name in new else 'removed')}
        try:
            change['diff'] = ''.join(difflib.unified_diff(left.decode('utf-8').splitlines(True),
                right.decode('utf-8').splitlines(True), fromfile=args.previous+'/'+name, tofile=args.lesson+'/'+name))
        except UnicodeDecodeError:
            change['diff'] = 'Binary or non-UTF8 file; inspect separately'
        result['changes'].append(change)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def preparation_status(args):
    from preparation_status import describe
    print(json.dumps(describe(lesson_path(args)), ensure_ascii=False, indent=2))


def preparation_step(args):
    from preparation_status import record
    print(json.dumps(record(args), ensure_ascii=False, indent=2))


def status(args):
    from learning_tools import preferences, cursor
    from learning_intent import view as intent_view
    course = read(root(args) / 'course.json')
    lessons = [read(p) for p in (root(args) / 'lessons').glob('*/lesson.json')]
    lessons.sort(key=lambda x: (x['order'], x['id']))
    from material_sources import describe
    from download_diagnostics import describe as download_status
    from processing_status import describe as processing_status
    for lesson in lessons:
        slug(lesson['id'])
        from spoken_homework import resolve as resolve_homework
        lesson['homework'] = resolve_homework(root(args)/'lessons'/lesson['id'], lesson)
        lesson['resources']=describe(lesson['resources'])
        lesson['download_diagnostics']=download_status(root(args)/'lessons'/lesson['id'],lesson['video'])
        directory = root(args) / 'lessons' / lesson['id']
        from preparation_status import describe as preparation_view
        lesson['preparation_status'] = preparation_view(directory)
        for job in lesson.get('processing_jobs', {}).values():
            working = directory / '.jobs' / slug(job['job_key'])
            job['processing_status'] = (processing_status(working)
                if working.resolve().is_relative_to(directory.resolve()) else {'status': 'unavailable'})
    legacy=read_progress(args)
    states=visible_progress(legacy['progress'],{lesson['id']: lesson for lesson in lessons})
    bindings=legacy['chat_bindings']
    intent=intent_view(args)
    print(json.dumps({'course': course, 'lessons': lessons, 'profile': args.profile,
                      'preferences': preferences(args), 'learning_checkpoint': cursor(args),
                      'learning_intent': intent, 'learning_progress':intent['learning_progress'],
                      'progress': states, 'chat_bindings': bindings}, ensure_ascii=False, indent=2))


def history(args):
    lesson_path(args)
    if args.limit <= 0 or args.limit > 1000:
        raise ValueError('History limit must be between 1 and 1000')
    with db(args) as connection:
        events = [dict(row) for row in connection.execute(
            'SELECT * FROM events WHERE lesson=? ORDER BY id DESC LIMIT ?',
            (args.lesson, args.limit))]
    print(json.dumps({'lesson': args.lesson, 'profile': args.profile,
                      'events': events}, ensure_ascii=False, indent=2))


def make_portable(args):
    """Anchor existing managed files before moving an older course directory."""
    read(root(args) / 'course.json')
    paths = sorted((root(args) / 'lessons').glob('*/lesson.json'))
    for path in paths:
        update_lesson(path.parent, lambda data: None)
    print('Anchored managed paths in', len(paths), 'lessons; external video/runtime paths stay external')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['init', 'add-lesson', 'attach', 'prepare', 'progress', 'bind', 'status', 'context', 'frame', 'scan-screen', 'diff-code', 'history', 'make-portable', 'settings', 'resume', 'topic', 'search', 'project-map', 'resource', 'code-stage', 'term-note', 'storage-report', 'download-plan', 'doctor', 'cleanup-plan', 'cleanup-progress', 'cleanup-job', 'fragments', 'intent', 'diagnostic', 'unit', 'learn-unit', 'research', 'project-timeline', 'study-next', 'career', 'career-route', 'ocr-frame', 'compact', 'spoken-homework', 'preparation-status', 'preparation-step', 'preparation-cancel', 'preparation-summary', 'material-status']:
        command = commands.add_parser(name)
        command.add_argument('--course', type=Path, required=True)
        if name in ['status', 'progress', 'bind', 'context', 'history', 'settings', 'resume', 'intent', 'diagnostic', 'learn-unit', 'study-next', 'career-route', 'cleanup-progress']:
            command.add_argument('--profile', default='default')
        if name in ['status','context','resume','intent','diagnostic','learn-unit','study-next','career-route']:
            command.add_argument('--session',help='Stable ID for this learning session/chat task')
        if name in ['status', 'context', 'settings', 'learn-unit']:
            command.add_argument('--profiles-dir', type=Path, help='Optional shared personal preferences directory outside plugin')
        if name in ['attach', 'prepare', 'progress', 'bind', 'add-lesson', 'context', 'frame', 'scan-screen', 'diff-code', 'history', 'topic', 'resource', 'code-stage', 'term-note', 'fragments', 'ocr-frame', 'compact', 'spoken-homework', 'preparation-status', 'preparation-step', 'preparation-cancel', 'preparation-summary']:
            command.add_argument('--lesson', required=True)
        if name == 'preparation-cancel':
            command.add_argument('--attempt', required=True, help='Exact attempt_id from preparation-status')
        if name in ['init', 'add-lesson']:
            command.add_argument('--title', required=True)
        if name == 'spoken-homework':
            command.add_argument('action', choices=['draft','accept','status'])
            command.add_argument('--time', type=float, default=0)
            command.add_argument('--window', type=float, default=90)
            command.add_argument('--assignment-file', type=Path)
            command.add_argument('--expected-revision', type=int)
        elif name == 'compact':
            command.add_argument('action',choices=['draft','plan','apply','status','reopen'])
            command.add_argument('--memory-file',type=Path)
            command.add_argument('--plan-file',type=Path)
        elif name == 'career-route':
            command.add_argument('--route-file',type=Path)
            command.add_argument('--expected-revision',type=int)
            command.add_argument('--reason')
            command.add_argument('--history-limit',type=int,default=0)
        elif name == 'career':
            command.add_argument('--sample-id',required=True)
            command.add_argument('--sample-file',type=Path)
            command.add_argument('--expected-revision',type=int)
        elif name == 'study-next':
            command.add_argument('--limit',type=int,default=5)
        elif name == 'project-timeline':
            group=command.add_mutually_exclusive_group()
            group.add_argument('--timeline-file',type=Path)
            group.add_argument('--timeline-id')
            command.add_argument('--expected-revision',type=int)
        elif name == 'research':
            command.add_argument('--research-id',required=True)
            command.add_argument('--event-file',type=Path)
            command.add_argument('--expected-revision',type=int)
        elif name == 'learn-unit':
            command.add_argument('--unit-id',required=True)
            group=command.add_mutually_exclusive_group()
            group.add_argument('--event-file',type=Path)
            group.add_argument('--checkpoint-file',type=Path)
            group.add_argument('--review-file',type=Path)
            group.add_argument('--lesson-context-file',type=Path)
            group.add_argument('--lesson-state-file',type=Path)
            group.add_argument('--practice-file',type=Path)
            command.add_argument('--lesson-id')
            command.add_argument('--expected-revision',type=int)
            command.add_argument('--expected-unit-sha256')
            command.add_argument('--history-limit',type=int,default=0)
        elif name == 'material-status':
            selection = command.add_mutually_exclusive_group()
            selection.add_argument('--unit-id')
            selection.add_argument('--lesson')
            command.add_argument('--time', type=float, default=0)
            command.add_argument('--window', type=float, default=120)
        elif name == 'unit':
            group=command.add_mutually_exclusive_group()
            group.add_argument('--unit-file',type=Path)
            group.add_argument('--unit-id')
            command.add_argument('--expected-revision',type=int)
        elif name in ('intent', 'diagnostic'):
            command.add_argument('--update-file',type=Path)
            command.add_argument('--expected-revision',type=int)
            command.add_argument('--reason')
            command.add_argument('--history-limit',type=int,default=0)
        elif name == 'cleanup-progress':
            command.add_argument('--apply',action='store_true')
        elif name == 'cleanup-job':
            command.add_argument('--lesson',required=True)
            command.add_argument('--job-key',required=True)
            command.add_argument('--apply',action='store_true')
            command.add_argument('--expected-revision')
        elif name == 'download-plan':
            command.add_argument('--lesson',required=True)
            command.add_argument('--video-quality',choices=['best','360','480','720','1080'],default='best')
        elif name == 'doctor':
            command.add_argument('--model-path',type=Path)
        elif name == 'storage-report':
            command.add_argument('--max-entries',type=int,default=200000)
        elif name == 'term-note':
            group = command.add_mutually_exclusive_group()
            group.add_argument('--note-file', type=Path)
            group.add_argument('--remove')
        elif name == 'fragments':
            command.add_argument('--keep', action='append')
            command.add_argument('--expected-revision', type=int)
        elif name == 'code-stage':
            group = command.add_mutually_exclusive_group()
            group.add_argument('--stage-file', type=Path)
            group.add_argument('--remove')
        elif name == 'resource':
            command.add_argument('--action',choices=['list','fetch','extract','review'],default='list')
            command.add_argument('--resource-id')
            command.add_argument('--expected-revision')
            command.add_argument('--notes')
        elif name == 'settings':
            command.add_argument('--scope', choices=['profile','course'], default='profile')
            command.add_argument('--set', action='append', default=[])
            command.add_argument('--reset', action='store_true')
            command.add_argument('--onboarding', choices=['in_progress','complete','skipped'])
            command.add_argument('--onboarding-file', type=Path)
            command.add_argument('--onboarding-skip', action='append', default=[])
        elif name == 'resume':
            command.add_argument('--state-file', type=Path)
            command.add_argument('--expected-revision', type=int)
        elif name == 'topic':
            command.add_argument('--topic-file', type=Path)
        elif name == 'search':
            command.add_argument('--query', required=True)
            command.add_argument('--limit', type=int, default=10)
            command.add_argument('--lesson')
        elif name == 'add-lesson':
            command.add_argument('--video', required=True)
            command.add_argument('--order', type=int, default=1)
        elif name == 'attach':
            command.add_argument('--repository')
            command.add_argument('--kind', choices=['code', 'homework', 'resources'], required=True)
            command.add_argument('--source')
            command.add_argument('--none', action='store_true')
            command.add_argument('--ref')
            command.add_argument('--resource-type', choices=['article', 'video', 'file'], default='article')
        elif name == 'prepare':
            command.add_argument('--cpu-mode', choices=['standard', 'economy'], default='standard',
                                 help='Economy limits CPU parallelism; keeps the same model and decoding')
            command.add_argument('--video-quality',choices=['best','360','480','720','1080'],default='best')
            command.add_argument('--download-fragments',type=int,choices=range(1,9),default=4)
            command.add_argument('--site-timeout',type=int,default=1800,help='Site download timeout in seconds: 120..7200')
            command.add_argument('--start', type=float, default=0)
            command.add_argument('--duration', type=float)
            command.add_argument('--frame-every', type=float, default=10,
                                 help='Seconds between navigation frames (default: 10); use scan-screen for brief changes')
            command.add_argument('--model', choices=['tiny', 'base', 'small', 'medium', 'large-v3'], default='small')
            command.add_argument('--model-path', type=Path)
            command.add_argument('--language', default='ru')
            command.add_argument('--prompt', default='')
            command.add_argument('--vocabulary-file', type=Path,
                                 help='JSON list of reviewed technical terms; ASR hints, not corrections')
            command.add_argument('--audio-track', type=int, help='Absolute audio stream index')
            command.add_argument('--audio-channel', type=int, help='Zero-based channel index')
            command.add_argument('--import-prepared', type=Path)
            command.add_argument('--rebuild', action='store_true', help='Create a new private job; preserve the previous source artifacts')
        elif name == 'progress':
            command.add_argument('--topic', required=True)
            command.add_argument('--stage', choices=['explained', 'understood', 'theory_checked', 'practice_checked', 'homework_passed', 'needs_review'], required=True)
            command.add_argument('--details', required=True)
            command.add_argument('--homework-revision', help='Revision from the homework actually reviewed; required for homework_passed')
        elif name == 'history':
            command.add_argument('--limit', type=int, default=100)
        elif name == 'ocr-frame':
            command.add_argument('--review-file',type=Path)
            command.add_argument('--expected-revision',type=int)
            command.add_argument('--frame-id',required=True)
            command.add_argument('--start',type=float,default=0)
            command.add_argument('--window',type=float,default=120)
            command.add_argument('--region',type=float,nargs=4)
            command.add_argument('--run',action='store_true',help='Run optional local OCR; otherwise read cache only')
        elif name == 'preparation-step':
            command.add_argument('--step', choices=['frames_review','code_review','ready'], required=True)
            command.add_argument('--time', type=float, required=True)
            command.add_argument('--window', type=float, required=True)
        elif name == 'scan-screen':
            command.add_argument('--start',type=float,default=0)
            command.add_argument('--duration',type=float,default=120)
            command.add_argument('--sample-every',type=float,default=.5)
            command.add_argument('--rebuild',action='store_true')
        elif name == 'bind':
            command.add_argument('--chat', required=True)
        elif name in ['context', 'frame']:
            if name == 'context':
                selection = command.add_mutually_exclusive_group(required=True)
                selection.add_argument('--time', type=float)
                selection.add_argument('--topic-id')
                command.add_argument('--window', type=float, default=120, help='Window for --time; --topic-id uses the saved full interval')
            else:
                command.add_argument('--time', type=float, required=True)
        elif name == 'diff-code':
            command.add_argument('--repository')
            command.add_argument('--previous', required=True)
            command.add_argument('--file')
    args = parser.parse_args()
    try:
        if args.command in ('preparation-status', 'preparation-cancel', 'material-status'):
            # Observe a live preparation operation without taking its exclusive use lock
            # or creating a managed runtime/temp session.
            from course_lifecycle import active
            directory = root(args)
            if directory.name == 'course': active(directory.parent)
            if args.command == 'material-status':
                from material_status import view
                print(json.dumps(view(args), ensure_ascii=False, indent=2))
            elif args.command == 'preparation-status': preparation_status(args)
            else:
                from preparation_status import cancel
                print(json.dumps(cancel(args), ensure_ascii=False, indent=2))
            return
        scope = getattr(args, 'course', None)
        home = find_home(scope) if scope is not None and args.command != 'prepare' else None
        with session(home):
            if args.command == 'preparation-summary':
                from preparation_summary import summarize
                print(json.dumps(summarize(lesson_path(args)), ensure_ascii=False, indent=2))
                return
            if args.command == 'spoken-homework':
                from spoken_homework import dispatch
                print(json.dumps(dispatch(args), ensure_ascii=False, indent=2))
                return
            if args.command=='compact':
                from compact_lesson import dispatch
                print(json.dumps(dispatch(args),ensure_ascii=False,indent=2))
            elif args.command=='ocr-frame':
                from screen_ocr import view
                print(json.dumps(view(args),ensure_ascii=False,indent=2))
            elif args.command=='career-route':
                from career_route import route
                print(json.dumps(route(args),ensure_ascii=False,indent=2))
            elif args.command=='career':
                from career_evidence import career
                print(json.dumps(career(args),ensure_ascii=False,indent=2))
            elif args.command=='study-next':
                from adaptive_learning import study_next
                print(json.dumps(study_next(args),ensure_ascii=False,indent=2))
            elif args.command=='learn-unit':
                from unit_learning import learn_unit
                print(json.dumps(learn_unit(args),ensure_ascii=False,indent=2))
            elif args.command=='unit':
                from study_units import unit
                print(json.dumps(unit(args),ensure_ascii=False,indent=2))
            elif args.command=='project-timeline':
                from project_timeline import timeline
                print(json.dumps(timeline(args),ensure_ascii=False,indent=2))
            elif args.command=='research':
                from topic_research import research
                print(json.dumps(research(args),ensure_ascii=False,indent=2))
            elif args.command=='diagnostic':
                from diagnostic_learning import diagnostic
                print(json.dumps(diagnostic(args),ensure_ascii=False,indent=2))
            elif args.command=='intent':
                from learning_intent import intent
                print(json.dumps(intent(args),ensure_ascii=False,indent=2))
            elif args.command=='cleanup-progress':
                from progress_cleanup import cleanup
                print(json.dumps(cleanup(args),ensure_ascii=False,indent=2))
            elif args.command=='cleanup-job':
                from job_cleanup import cleanup_job
                print(json.dumps(cleanup_job(args),ensure_ascii=False,indent=2))
            elif args.command in ('download-plan','doctor','cleanup-plan'):
                from maintenance import dispatch
                print(json.dumps(dispatch(args),ensure_ascii=False,indent=2))
            elif args.command=='storage-report':
                from storage_report import storage_report
                print(json.dumps(storage_report(args),ensure_ascii=False,indent=2))
            elif args.command=='term-note':
                from term_notes import term_note
                print(json.dumps(term_note(args),ensure_ascii=False,indent=2))
            elif args.command=='fragments':
                from fragment_selection import fragments
                print(json.dumps(fragments(args),ensure_ascii=False,indent=2))
            elif args.command=='code-stage':
                from code_stages import code_stage
                print(json.dumps(code_stage(args),ensure_ascii=False,indent=2))
            elif args.command=='resource':
                from material_sources import resource
                print(json.dumps(resource(args),ensure_ascii=False,indent=2))
            elif args.command in ('settings','resume','topic','search','project-map'):
                import learning_tools
                print(json.dumps(getattr(learning_tools,args.command.replace('-','_'))(args),ensure_ascii=False,indent=2))
            else:
                globals()[args.command.replace('-', '_')](args)
    except KeyboardInterrupt as error:
        print(str(error) or 'Подготовка прервана; готовые этапы сохранены.', file=sys.stderr)
        sys.exit(130)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print('Error:', error, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
