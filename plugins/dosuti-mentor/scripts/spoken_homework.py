"""Reviewed ASR-derived assignments, outside the sealed lesson inventory.

These are assistant/human interpretations of recorded instructions, never an
uploaded teacher file or independently verified verbatim audio transcription.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from atomic_files import atomic_json
from compact_lesson import MAX_JSON, ordinary, read, signature, text, texts
from source_integrity import file_hash
from storage_lock import file_lock

SCHEMA = 'spoken_homework_v1'
FIELDS = {'schema', 'source', 'instructions', 'review', 'uncertainties'}


def location(directory):
    import course
    directory = Path(directory).absolute()
    course.slug(directory.name)
    base = directory.parent.parent
    folder = ordinary(base / 'verbal-homework', base)
    return folder, ordinary(folder / (directory.name + '.json'), base)


def draft(directory, data, start, window):
    from lecture_context import assemble
    context = assemble(directory, data, start, window, include_verbal_homework=False,
                       include_code_stages=False, include_term_notes=False)
    if context['gaps']: raise ValueError('Prepare the complete assignment interval first')
    speech = [{k: row[k] for k in ('id', 'start', 'end', 'text')} for row in context['speech']]
    if not speech: raise ValueError('No speech in assignment interval')
    source = dict(video_sha256=data['video']['sha256'], start=context['requested_window'][0],
                  end=context['requested_window'][1], speech=speech)
    source['revision'] = signature(source)
    return dict(schema=SCHEMA, source=source, instructions='',
                review=dict(status='draft', notes=''), uncertainties=[])


def validate(directory, data, assignment):
    if not isinstance(assignment, dict) or set(assignment) != FIELDS or assignment['schema'] != SCHEMA:
        raise ValueError('Invalid spoken homework fields')
    source = assignment['source']
    if not isinstance(source, dict) or set(source) != {'video_sha256', 'start', 'end', 'speech', 'revision'}:
        raise ValueError('Invalid spoken homework source')
    expected = draft(directory, data, source['start'], source['end'] - source['start'])['source']
    if source != expected: raise ValueError('Spoken homework source changed; review the ASR interval again')
    review = assignment['review']
    if not isinstance(review, dict) or set(review) != {'status', 'notes'} or review['status'] != 'reviewed':
        raise ValueError('Explicitly review the spoken assignment before accepting')
    text(assignment['instructions']);text(review['notes']);texts(assignment['uncertainties'])


def record(directory):
    folder, path = location(directory)
    if not path.exists(): return None
    value = read(path)
    if (set(value) != {'schema', 'revision', 'assignment', 'sha256'} or value['schema'] != SCHEMA
            or type(value['revision']) is not int or value['revision'] < 1):
        raise ValueError('Invalid spoken homework registry')
    from source_integrity import digest
    digest(value['sha256'])
    return value


def accept(directory, assignment, expected_revision):
    import course
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('accept requires a nonnegative --expected-revision')
    # Same lock order as progress; accepting a new task cannot race a grade.
    with file_lock(directory / '.lesson.lock'):
        data = course.read(directory / 'lesson.json')
        validate(directory, data, assignment)
        folder, path = location(directory)
        folder.mkdir(exist_ok=True)
        ordinary(folder, directory.parent.parent)
        lock = ordinary(folder / (directory.name + '.lock'), directory.parent.parent)
        with file_lock(lock):
            previous = record(directory)
            revision = previous['revision'] if previous else 0
            if expected_revision != revision: raise ValueError('Spoken homework revision conflict')
            value = dict(schema=SCHEMA, revision=revision + 1, assignment=deepcopy(assignment),
                         sha256=hashlib.sha256(assignment['instructions'].encode('utf-8')).hexdigest())
            if len(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()) > MAX_JSON:
                raise ValueError('Spoken homework record exceeds JSON limit')
            text_dir = ordinary(folder / 'text', directory.parent.parent)
            text_dir.mkdir(exist_ok=True)
            target = ordinary(text_dir / (value['sha256'] + '.txt'), directory.parent.parent)
            if target.exists():
                if file_hash(target) != value['sha256']: raise ValueError('Stored spoken homework text changed')
            else:
                from atomic_files import atomic_bytes
                atomic_bytes(target, assignment['instructions'].encode('utf-8'))
            atomic_json(path, value)
    return dict(revision=value['revision'], homework=resolve(directory, data))


def resolve(directory, data):
    """Teacher file has priority. Invalid derived evidence blocks grading."""
    original = deepcopy(data.get('homework', dict(status='not_asked')))
    if original.get('status') == 'provided': return original
    try:
        value = record(directory)
        if value is None: return original
        validate(directory, data, value['assignment'])
        folder, _ = location(directory)
        target = ordinary(folder / 'text' / (value['sha256'] + '.txt'), directory.parent.parent)
        if not target.is_file() or file_hash(target) != value['sha256']:
            raise ValueError('Stored spoken homework text missing or changed')
        assignment = value['assignment']
        if hashlib.sha256(assignment['instructions'].encode('utf-8')).hexdigest() != value['sha256']:
            raise ValueError('Spoken homework text does not match reviewed instructions')
        return dict(status='provided', source_kind='lecture_speech', file=str(target), sha256=value['sha256'],
                    revision='verbal:' + signature(value), registry_revision=value['revision'],
                    origin=assignment['source'], review=assignment['review'], uncertainties=assignment['uncertainties'],
                    assurance='reviewed_ASR_interpretation_not_independently_verified_audio_or_teacher_file')
    except (ValueError, OSError, KeyError, TypeError) as error:
        return dict(status='needs_review', source_kind='lecture_speech', reason=str(error))


def dispatch(args):
    import course
    directory = course.lesson_path(args)
    data = course.read(directory / 'lesson.json')
    if args.action == 'draft': return draft(directory, data, args.time, args.window)
    if args.action == 'accept':
        if args.assignment_file is None: raise ValueError('accept requires --assignment-file')
        return accept(directory, read(args.assignment_file), args.expected_revision)
    if args.action == 'status':
        value = record(directory)
        return dict(revision=value['revision'] if value else 0, homework=resolve(directory, data))
    raise ValueError('Unknown spoken-homework action')
