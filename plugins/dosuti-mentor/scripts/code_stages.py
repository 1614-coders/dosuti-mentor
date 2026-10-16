"""Manual, evidence-bound code stages; never infer screen equality from a ZIP."""
from copy import deepcopy
from pathlib import Path
import json

from source_integrity import digest, verify_snapshot
from code_repositories import current, snapshots


def snapshot(data, checksum):
    digest(checksum, 'Code stage snapshot SHA256')
    for item in snapshots(data):
        if item.get('content_sha256') == checksum and item.get('snapshot'):
            verify_snapshot(item)
            return item
    raise ValueError('Code stage snapshot is unavailable; attach the source first')


def evidence(directory, data, value):
    from lecture_context import assemble
    result = assemble(directory, data, value['start'], value['end'] - value['start'], include_code_stages=False)
    if not result['coverage']['complete']:
        raise ValueError('Code stage interval must be fully prepared')
    available = {(f['approx_seconds'], f['sha256']) for f in result['frames']
                 if value['start'] <= f['approx_seconds'] < value['end']}
    for frame in value['frames']:
        if (frame['time'], frame['sha256']) not in available:
            raise ValueError('Code stage frame is missing or changed')
    return result


def validate(value):
    import course as c
    from learning_tools import text
    from lecture_context import number
    fields = {'id', 'title', 'start', 'end', 'code_sha256', 'file', 'screen_file', 'match', 'notes', 'frames'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Stage requires exactly: ' + ', '.join(sorted(fields)))
    c.slug(value['id'])
    for key in ('title', 'file', 'screen_file', 'notes'):
        text(value[key], key, 4000)
    number(value['start'], 'start'); number(value['end'], 'end')
    if value['start'] < 0 or value['end'] <= value['start']:
        raise ValueError('Require 0 <= start < end')
    digest(value['code_sha256'])
    if value['match'] not in ('matches', 'partial', 'differs', 'unknown'):
        raise ValueError('Invalid code stage match')
    frames = value['frames']
    if not isinstance(frames, list) or not 1 <= len(frames) <= 50:
        raise ValueError('Stage needs 1..50 inspected frames')
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != {'time', 'sha256'}:
            raise ValueError('Frame requires time and sha256')
        number(frame['time'], 'frame time'); digest(frame['sha256'])
        if not value['start'] <= frame['time'] < value['end']:
            raise ValueError('Frame must be inside the stage interval')
    if len({(f['time'], f['sha256']) for f in frames}) != len(frames):
        raise ValueError('Duplicate stage frame')


def checked(directory, data, start=None, stop=None):
    rows = []
    for original in data.get('code_stages', {}).values():
        row = deepcopy(original)
        try:
            # Validate stored annotations too; malformed local data must not be trusted.
            validate({k: row[k] for k in ('id','title','start','end','code_sha256','file','screen_file','match','notes','frames')})
            if start is not None and not (row['start'] < stop and row['end'] > start):
                continue
            if row.get('source_sha256') != data['video'].get('sha256'):
                raise ValueError('Video identity changed')
            code = snapshot(data, row['code_sha256'])
            if row['file'] not in code['inventory']:
                raise ValueError('Stage file is missing from snapshot')
            evidence(directory, data, row)
            row.update(stale=False, snapshot=code['snapshot'], absolute_file=str(Path(code['snapshot']) / row['file']),
                       historical=all(code is not item for item in current(data).values()))
        except (ValueError, OSError, KeyError, TypeError) as error:
            row.update(stale=True, reason=str(error))
        row['usable_as_screen_match'] = not row['stale'] and row.get('match') == 'matches'
        row['verification'] = 'assistant_annotation_not_automatic_proof'
        rows.append(row)
    for row in rows:
        row['overlapping_stage_ids'] = [other['id'] for other in rows if other is not row
            and not row['stale'] and not other['stale'] and row['screen_file'] == other['screen_file']
            and row['start'] < other['end'] and row['end'] > other['start']]
        if row['overlapping_stage_ids']:
            row['usable_as_screen_match'] = False
    return rows


def code_stage(args):
    import course as c
    directory = c.lesson_path(args)
    if getattr(args, 'stage_file', None):
        value = json.loads(Path(args.stage_file).read_text())
        validate(value)
        def update(data):
            records = data.setdefault('code_stages', {})
            if value['id'] in records:
                raise ValueError('Stage ID already exists; remove explicitly before replacing it')
            code = snapshot(data, value['code_sha256'])
            if value['file'] not in code['inventory']:
                raise ValueError('Unknown code snapshot file')
            evidence(directory, data, value)
            records[value['id']] = dict(value, source_sha256=data['video']['sha256'], updated=c.now())
        c.update_lesson(directory, update)
    elif getattr(args, 'remove', None):
        c.slug(args.remove)
        def remove(data):
            if args.remove not in data.get('code_stages', {}):
                raise ValueError('Unknown stage ID')
            del data['code_stages'][args.remove]
        c.update_lesson(directory, remove)
    return {'lesson': args.lesson, 'stages': checked(directory, c.read(directory/'lesson.json'))}
