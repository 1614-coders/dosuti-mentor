"""Reviewed lesson memory and explicit, resumable pruning of managed media only."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

from atomic_files import atomic_json
from profile_storage import checked
from source_integrity import digest, file_hash, source_matches, verify_evidence, check_local_video
from storage_lock import file_lock

SCHEMA = 'compact_lesson_v1'
MEMORY = 'lecture_memory_v2'
LEGACY_MEMORY = 'lecture_memory_v1'
MAX_FILES = 20000
MAX_JSON = 8 * 1024 * 1024


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def read(path):
    checked(path)
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON: raise ValueError('Compact JSON limit exceeded')
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('Compact data must be an object')
    return value


def ordinary(path, directory):
    path = Path(path).absolute()
    directory = Path(directory).absolute()
    if '..' in path.parts or not path.is_relative_to(directory): raise ValueError('Compact path outside lesson')
    for parent in [path, *path.parents]:
        if parent.is_symlink(): raise ValueError('Compact paths must not be symbolic links')
        if parent == directory: break
    checked(path, path.is_dir())
    return path


def asset(directory, name):
    if (not isinstance(name, str) or not name or '\\' in name or Path(name).is_absolute()
            or '..' in Path(name).parts or Path(name).as_posix() != name):
        raise ValueError('Invalid compact relative path')
    return ordinary(directory / name, directory)


def row(path, directory):
    ordinary(path, directory)
    if not path.is_file(): raise ValueError('Compact asset missing')
    return dict(file=path.relative_to(directory).as_posix(), sha256=file_hash(path), size=path.stat().st_size)


def verify_rows(directory, rows, missing=False):
    if not isinstance(rows, list) or len(rows) > MAX_FILES: raise ValueError('Invalid compact inventory')
    seen = set()
    for item in rows:
        if (not isinstance(item, dict) or set(item) != {'file', 'sha256', 'size'}
                or type(item['size']) is not int or item['size'] < 0
                or not isinstance(item['file'], str) or item['file'] in seen):
            raise ValueError('Invalid compact inventory row')
        seen.add(item['file']);digest(item['sha256'])
        p = asset(directory, item['file'])
        if missing and not p.exists(): continue
        if row(p, directory) != item: raise ValueError('Compact asset changed: ' + item['file'])


def retained(directory, excluded):
    result = []
    pending = [directory]
    visited = 0
    while pending:
        folder = pending.pop();ordinary(folder, directory)
        for p in folder.iterdir():
            visited += 1
            if visited > MAX_FILES: raise ValueError('Too many lesson files for compact inventory')
            relative = p.relative_to(directory)
            # Jobs/video remain protected except explicitly verified remove rows.
            if relative.parts[0] in ('.jobs', 'video', '.compact-history'): continue
            if p == directory / 'compact.json' or p.name.endswith('.lock'): continue
            if p.name == 'progress.json' and p.parent.parent.name == 'prepared': continue
            ordinary(p, directory)
            if p.is_dir(): pending.append(p)
            elif relative.as_posix() not in excluded: result.append(row(p, directory))
    return sorted(result, key=lambda item: item['file'])


def inspect(directory, data):
    """Read-only inspection. It does not approve the meaning of ASR or screenshots."""
    import course
    from lecture_context import assemble, coverage
    from media_input import probe_media
    from storage_paths import source_kind
    from job_cleanup import inspect as inspect_job
    ordinary(directory / 'lesson.json', directory)
    checksum = digest(data['video'].get('sha256'))
    check_local_video(data['video'])
    video = Path(data['video'].get('path') or data['video']['source']).expanduser()
    if not video.is_file(): raise ValueError('Original video required for initial compact inspection')
    media = probe_media(video)
    end = media['video']['end']
    if not isinstance(end, (int, float)) or not math.isfinite(end) or end <= 0:
        raise ValueError('Invalid original media duration')
    fragments = data.get('prepared_fragments', [])
    if not fragments: raise ValueError('Prepare the complete lecture first')
    max_end = max(f['coverage']['end'] for f in fragments)
    duration = max_end if abs(max_end - end) <= .1 else end
    covered, gaps = coverage([(f['coverage']['start'], f['coverage']['end']) for f in fragments], 0, duration)
    if gaps or not covered['complete']: raise ValueError('Unprepared lecture intervals; compacting is blocked')
    if any(job.get('status') != 'complete' for field in ('processing_jobs', 'screen_jobs')
           for job in data.get(field, {}).values()):
        raise ValueError('Finish or resolve pending/failed processing before compacting')
    for fragment in fragments: ordinary(Path(fragment['folder']), directory)
    context = assemble(directory, data, 0, duration, include_verbal_homework=False)
    from code_repositories import snapshots
    from source_integrity import verify_snapshot
    for code in snapshots(data):
        if code.get('status') == 'provided':
            ordinary(Path(code['snapshot']), directory);verify_snapshot(code)
    for frame in context['frames']: ordinary(Path(frame['absolute_path']), directory)
    if any(s.get('stale') for s in context['code_stages'] + context['term_notes']):
        raise ValueError('Review stale code stages or term notes before compacting')
    if course.homework_integrity(data['homework']) not in ('not_provided', 'verified'):
        raise ValueError('Homework is missing or changed')
    if data['homework'].get('status') == 'provided': ordinary(Path(data['homework']['file']), directory)
    for resource in context['resources'].get('items', []):
        for field in ('source_integrity', 'text_integrity'):
            if field in resource and resource[field] != 'verified':
                raise ValueError('Saved additional material is missing or changed')
        for name in (resource.get('origin', {}).get('raw_file'), resource.get('extracted_file')):
            if name: ordinary(Path(name), directory)
    drops = []; jobs = []
    for fragment in fragments:
        folder = ordinary(Path(fragment['folder']), directory)
        if not folder.is_relative_to(directory / 'prepared'): raise ValueError('Prepared material must be managed inside lesson')
        manifest = course.read(folder / 'manifest.json');source_matches(manifest, checksum)
        verify_evidence(folder, manifest)
        audio = folder / 'audio.wav'
        if audio.exists():
            verify_evidence(folder, manifest, audio=True);drops.append(row(audio, directory))
    for job in data.get('processing_jobs', {}).values():
        key = job['job_key'];course.slug(key)
        working = ordinary(directory / '.jobs' / key, directory)
        if not (working / 'payload-cleaned.json').exists():
            plan = inspect_job(directory, data, key)
            jobs.append(dict(key=key, publication=plan['publication']))
            drops.extend(row(working / item['file'], directory) for item in plan['files'])
    # Only a remote-source cache with a matching download receipt is ours to delete.
    if source_kind(data['video']['source']) == 'remote' and video.absolute().parent == directory / 'video':
        ordinary(video, directory)
        receipt = read(directory / 'video/download.json')
        if (receipt.get('source') != data['video']['source'] or receipt.get('file') != video.name
                or receipt.get('sha256') != checksum or receipt.get('size') != video.stat().st_size
                or not re.fullmatch(r'video-[0-9a-f]{32}\.[a-zA-Z0-9]+', video.name)):
            raise ValueError('Video is not a verified managed download')
        drops.append(row(video, directory))
    # Protect explicit cross-lesson references to the source/audio/work payload.
    for index, meta in enumerate((directory.parents[1] / 'lessons').glob('*/lesson.json')):
        if index >= 2000: raise ValueError('Too many lessons to check media references')
        if meta.parent == directory: continue
        ordinary(meta, directory.parents[1]);other = course.read(meta)
        from maintenance import strings
        for value in strings(other):
            if Path(value).is_absolute():
                ref = Path(value).resolve()
                if any((directory / item['file']).is_relative_to(ref) for item in drops):
                    raise ValueError('Another lesson references media selected for removal')
    unique = {item['file']: item for item in drops}
    from maintenance import strings
    # Current material references are also protected. Prepared folder containers
    # are expected provenance, but arbitrary homework/resources are not.
    extra = {k: v for k, v in data.items() if k not in (
        'video', 'processing_jobs', 'storage_paths', 'prepared_fragments', 'retired_fragments',
        'screen_scans', 'retired_screen_scans')}
    for value in strings(extra):
        if Path(value).is_absolute():
            ref = Path(value).resolve()
            if any((directory / name).is_relative_to(ref) for name in unique):
                raise ValueError('Lesson material references media selected for removal')
    from spoken_homework import resolve as resolve_homework
    context['homework'] = resolve_homework(directory, data)
    if context['homework'].get('status') == 'needs_review':
        raise ValueError('Review the changed spoken homework before compacting')
    keep = retained(directory, unique)
    return dict(source_sha256=checksum, duration=duration, keep=keep,
                remove=sorted(unique.values(), key=lambda item: item['file']), jobs=jobs,
                source_revision=signature(keep)), context


def draft(directory, data):
    import course
    info, context = inspect(directory, data)
    return dict(schema=MEMORY, source_sha256=info['source_sha256'], source_revision=info['source_revision'],
                duration=info['duration'], review_scope=dict(transcript='not_checked', frames='not_checked',
                    checked_frame_ids=[], audio='not_checked', speakers='not_checked', code='not_checked',
                    homework_revision=course.homework_revision(context['homework']), unresolved=[]),
                review=dict(speech='unreviewed', screen='unreviewed', code='unreviewed', homework='unreviewed', notes=''),
                topics=[dict(title=data['title'], start=0, end=info['duration'], teacher_explanation='',
                             teacher_examples=[], additions='', frame_ids=[], code_examples=[], uncertainties=[])],
                limitations=['ASR and periodic frames do not prove full semantic coverage.'])


def text(value, empty=False, limit=30000):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError('Invalid lecture memory text')


def texts(values):
    if not isinstance(values, list) or len(values) > 100: raise ValueError('Invalid lecture memory list')
    for value in values: text(value)


def validate_memory(memory, info, context):
    fields = {'schema', 'source_sha256', 'source_revision', 'duration', 'review', 'topics', 'limitations'}
    if isinstance(memory, dict) and memory.get('schema') == MEMORY: fields.add('review_scope')
    if (not isinstance(memory, dict) or set(memory) != fields
            or memory['schema'] not in (MEMORY, LEGACY_MEMORY) or memory['source_sha256'] != info['source_sha256']
            or memory['source_revision'] != info['source_revision'] or memory['duration'] != info['duration']):
        raise ValueError('Lecture memory must match current evidence revision and complete duration')
    from lecture_context import number
    number(memory['duration'], 'Memory duration')
    review = memory['review']
    if not isinstance(review, dict) or set(review) != {'speech', 'screen', 'code', 'homework', 'notes'}:
        raise ValueError('Invalid memory review')
    for field in ('speech', 'screen', 'code', 'homework'):
        if review[field] not in ('reviewed', 'not_present'): raise ValueError('Review all lecture memory areas first')
    if ((context['speech'] and review['speech'] != 'reviewed') or (context['frames'] and review['screen'] != 'reviewed')
            or (any(c.get('status') == 'provided' for c in context['teacher_repositories'].values()) and review['code'] != 'reviewed')
            or (context['homework'].get('status') == 'provided' and review['homework'] != 'reviewed')):
        raise ValueError('Present lesson material cannot be marked absent')
    text(review['notes']);texts(memory['limitations'])
    if memory['schema'] == MEMORY: validate_review_scope(memory['review_scope'], review, context)
    topics = memory['topics']
    if not isinstance(topics, list) or not 1 <= len(topics) <= 200: raise ValueError('Memory needs 1..200 topics')
    frames = {f['id']: f for f in context['frames']}
    ranges = []
    from lecture_context import interval, coverage, number
    for topic in topics:
        if not isinstance(topic, dict) or set(topic) != {'title', 'start', 'end', 'teacher_explanation', 'teacher_examples', 'additions', 'frame_ids', 'code_examples', 'uncertainties'}:
            raise ValueError('Invalid memory topic')
        start, end = interval(topic, 'Memory topic')
        if end > info['duration']: raise ValueError('Memory topic exceeds lecture')
        ranges.append((start, end));text(topic['title']);text(topic['teacher_explanation']);text(topic['additions'], True)
        texts(topic['teacher_examples']);texts(topic['uncertainties'])
        ids = topic['frame_ids']
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) or i not in frames for i in ids):
            raise ValueError('Unknown memory frame')
        if any(not start <= frames[i]['approx_seconds'] <= end for i in ids): raise ValueError('Memory frame outside topic')
        if not isinstance(topic['code_examples'], list) or len(topic['code_examples']) > 100: raise ValueError('Invalid code examples')
        for example in topic['code_examples']:
            if not isinstance(example, dict) or set(example) != {'language', 'code', 'at', 'frame_id', 'stage'}:
                raise ValueError('Invalid memory code example')
            for key in ('language', 'code', 'stage'): text(example[key])
            at = number(example['at'], 'Code example time')
            if not start <= at <= end or example['frame_id'] not in ids: raise ValueError('Code example needs a retained topic frame')
    if coverage(ranges, 0, info['duration'])[1]: raise ValueError('Memory topics have uncovered intervals')


def validate_review_scope(scope, review, context):
    fields = {'transcript', 'frames', 'checked_frame_ids', 'audio', 'speakers', 'code', 'homework_revision', 'unresolved'}
    if not isinstance(scope, dict) or set(scope) != fields:
        raise ValueError('Invalid memory review scope')
    for key, options in dict(transcript=('not_checked', 'read'),
            frames=('not_checked', 'sampled', 'all_retained'),
            audio=('not_checked', 'sampled', 'full'), speakers=('not_checked', 'sampled', 'full'),
            code=('not_checked', 'reviewed')).items():
        if scope[key] not in options: raise ValueError('Invalid review scope ' + key)
    ids = scope['checked_frame_ids']; available = {f['id'] for f in context['frames']}
    if (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)
            or len(ids) != len(set(ids)) or not set(ids) <= available):
        raise ValueError('Unknown or repeated checked frame')
    if (scope['frames'] == 'not_checked' and ids or scope['frames'] == 'sampled' and not ids
            or scope['frames'] == 'all_retained' and set(ids) != available):
        raise ValueError('Review frame scope does not match checked frame IDs')
    if context['speech'] and review['speech'] == 'reviewed' and scope['transcript'] != 'read':
        raise ValueError('Declare transcript review scope')
    if context['frames'] and review['screen'] == 'reviewed' and scope['frames'] == 'not_checked':
        raise ValueError('Declare sampled or all-retained screen review scope')
    if review['code'] == 'reviewed' and scope['code'] != 'reviewed':
        raise ValueError('Declare code review scope')
    from course import homework_revision
    if scope['homework_revision'] != homework_revision(context['homework']):
        raise ValueError('Spoken/file homework revision changed since memory review')
    texts(scope['unresolved'])


def plan(directory, data, memory):
    info, context = inspect(directory, data);validate_memory(memory, info, context)
    value = dict(schema=SCHEMA, source_sha256=info['source_sha256'], source_revision=info['source_revision'],
                 duration=info['duration'], keep=info['keep'], remove=info['remove'], jobs=info['jobs'], memory=memory)
    value['revision'] = signature(value)
    # Bound the exact pretty-printed receipt, including its envelope and the
    # longest state name. An unreadable receipt must never precede deletion.
    envelope = dict(schema=SCHEMA, state='reopened', plan=value)
    if len(json.dumps(envelope, ensure_ascii=False, indent=2, allow_nan=False).encode()) > MAX_JSON:
        raise ValueError('Compact plan too large')
    return value


def check_plan(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'source_sha256', 'source_revision', 'duration', 'keep', 'remove', 'jobs', 'memory', 'revision'}
            or value['schema'] != SCHEMA or value['revision'] != signature({k: v for k, v in value.items() if k != 'revision'})):
        raise ValueError('Invalid compact plan checksum or fields')
    digest(value['source_sha256']);digest(value['source_revision'])
    if value['source_revision'] != signature(value['keep']): raise ValueError('Compact retained revision changed')


def receipt(directory):
    value = read(directory / 'compact.json');check_plan(value.get('plan'))
    if set(value) != {'schema', 'state', 'plan'} or value['schema'] != SCHEMA or value['state'] not in ('pruning', 'compact', 'reopened'):
        raise ValueError('Invalid compact receipt')
    return value


@contextmanager
def locked(directory):
    import course
    with ExitStack() as stack:
        paths = [directory / '.lesson.lock', directory / 'video/.download.lock']
        jobs = directory / '.jobs'
        if jobs.exists():
            ordinary(jobs, directory)
            for index, folder in enumerate(jobs.iterdir()):
                if index >= 256: raise ValueError('Too many jobs to lock for compacting')
                ordinary(folder, directory)
                if folder.is_dir(): paths.append(folder / '.processing.lock')
        for path in paths:
            ordinary(path, directory);stack.enter_context(file_lock(path, timeout=.1));ordinary(path, directory)
        yield course.read(directory / 'lesson.json')


def apply(directory, value):
    import course
    check_plan(value)
    with locked(directory) as data:
        current = receipt(directory) if (directory / 'compact.json').exists() else None
        resuming = bool(current and current['state'] in ('pruning', 'compact'))
        if resuming:
            if current['plan'] != value: raise ValueError('Use the original compact plan to resume')
            verify_rows(directory, value['keep'])
            verify_rows(directory, value['remove'], missing=True)
        else:
            if plan(directory, data, value['memory']) != value: raise ValueError('Compact plan changed; inspect again')
            if current:
                history = directory / '.compact-history';ordinary(history, directory);history.mkdir(exist_ok=True)
                atomic_json(history / (current['plan']['revision'] + '.json'), current)
        verify_rows(directory, value['keep']);verify_rows(directory, value['remove'], missing=resuming)
        target = directory / 'compact.json';ordinary(target, directory)
        # Durable receipt precedes every destructive operation. All keep bytes
        # are checked before resuming, not merely the existence of this marker.
        state = dict(schema=SCHEMA, state='pruning', plan=value);atomic_json(target, state)
        for job in value['jobs']:
            course.slug(job['key'])
            path = asset(directory, '.jobs/' + job['key'] + '/payload-cleaned.json')
            atomic_json(path, dict(schema_version=1, job_key=job['key'], source_sha256=value['source_sha256'], publication=job['publication']))
        removed = 0
        for item in value['remove']:
            p = asset(directory, item['file'])
            if p.exists():
                if row(p, directory) != item: raise ValueError('Media changed during compacting')
                p.unlink();removed += item['size']
        verify_rows(directory, value['keep'])
        state['state'] = 'compact';atomic_json(target, state)
        return dict(state='compact', removed_logical_bytes=removed, resumed=resuming,
                    limitations=['Logical bytes are not guaranteed filesystem space reclaimed.',
                                 'Material review is an assistant/human judgement, not automatic semantic proof.'])


def memory_context(directory, start, stop, selected_frame_ids=None):
    if not (directory / 'compact.json').exists(): return None
    state = receipt(directory)
    if state['state'] == 'reopened': return dict(state='reopened', memory_available=False)
    value = state['plan'];verify_rows(directory, value['keep'])
    stop = min(stop, value['duration'])
    topics = []
    for original in value['memory']['topics']:
        if original['start'] >= stop or original['end'] <= start: continue
        topic = deepcopy(original)
        if not (start <= topic['start'] and stop >= topic['end']):
            # Prose has no sentence-level timestamps: never present the whole
            # topic as if its later results were known in this short window.
            topic['teacher_explanation'] = ''
            topic['teacher_examples'] = []
            topic['additions'] = ''
            topic['uncertainties'] = []
            topic['code_examples'] = [e for e in topic['code_examples'] if start <= e['at'] < stop
                                      and (selected_frame_ids is None or e['frame_id'] in selected_frame_ids)]
            topic['frame_ids'] = [i for i in topic['frame_ids'] if selected_frame_ids is not None and i in selected_frame_ids]
            topic['selection'] = dict(overview_withheld=True, reason='topic_exceeds_requested_window',
                                      full_topic_window=[topic['start'], topic['end']],
                                      next_step='Use selected ASR/frames; request the full topic for its overview.')
        topics.append(topic)
    scope = deepcopy(value['memory'].get('review_scope', dict(status='legacy_unknown',
        transcript='unknown', frames='unknown', checked_frame_ids=[], audio='unknown',
        speakers='unknown', code='unknown', unresolved=['Legacy memory has no structured review scope.'])))
    return dict(state=state['state'], memory_available=True, source_sha256=value['source_sha256'],
                source_revision=value['source_revision'], review=value['memory']['review'], review_scope=scope,
                review_scope_assurance='self_reported_not_independently_measured',
                requested_window=[start, stop], topics=topics, limitations=value['memory']['limitations'],
                verification='reviewed_saved_material_not_original_video_reverification',
                original_media_required_for=['new_frames', 'speech_recheck', 'rebuild'])


def require_media_open(directory):
    if (directory / 'compact.json').exists() and receipt(directory)['state'] in ('pruning', 'compact'):
        raise ValueError('Lesson is compact: use retained context/memory; explicitly compact reopen before restoring media')


def dispatch(args):
    import course
    directory = course.lesson_path(args)
    data = course.read(directory / 'lesson.json')
    action = args.action
    if action == 'draft': return draft(directory, data)
    if action == 'plan':
        if args.memory_file is None: raise ValueError('plan requires --memory-file')
        value = plan(directory, data, read(args.memory_file))
        return dict(read_only=True, plan=value, removable_logical_bytes=sum(r['size'] for r in value['remove']),
                    note='Apply only after reviewing retained material and obtaining explicit media-removal authorization.')
    if action == 'apply':
        if args.plan_file is None: raise ValueError('apply requires --plan-file')
        return apply(directory, read(args.plan_file))
    if action == 'status': return memory_context(directory, 0, float('inf')) or dict(state='full', memory_available=False)
    if action == 'reopen':
        with locked(directory):
            state = receipt(directory)
            if state['state'] == 'pruning': raise ValueError('Finish interrupted compact apply before reopening')
            state['state'] = 'reopened';atomic_json(directory / 'compact.json', state)
        return dict(state='reopened', media_restored=False, next_step='Restore the pinned original; use prepare --rebuild for deleted audio/work payload.')
    raise ValueError('Unknown compact action')
