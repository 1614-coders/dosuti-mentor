"""Explicit, revision-checked activation of preserved preparation variants."""
from pathlib import Path
from copy import deepcopy
from lecture_context import load_fragment, coverage
from source_integrity import check_local_video


def protected_ranges(data):
    """Explicit ranges survive late prepares; legacy selections pin current coverage."""
    from lecture_context import interval
    guard = data.get('fragment_selection_guard')
    if guard is None:
        if not data.get('fragment_selections'):
            return []
        # Do not reconstruct an older choice that a pre-0.4.7 prepare already replaced.
        return [interval(f['coverage'], 'Legacy selection coverage') for f in data.get('prepared_fragments', [])]
    if (not isinstance(guard, dict) or set(guard) != {'schema', 'source_sha256', 'ranges'}
            or guard['schema'] != 'explicit_fragment_selection_v1'
            or guard['source_sha256'] != data['video'].get('sha256')
            or not isinstance(guard['ranges'], list) or not guard['ranges']):
        raise ValueError('Invalid fragment selection guard; preserve metadata for diagnosis')
    return [interval(bounds, 'Protected selection range') for bounds in guard['ranges']]


def guard(data, ranges):
    return dict(schema='explicit_fragment_selection_v1', source_sha256=data['video']['sha256'],
                ranges=[dict(start=s, end=e) for s,e in ranges])


def defer_to_selection(data, fragment):
    """Return candidate/active when protected; None retains normal publication rules."""
    from lecture_context import interval
    ranges = protected_ranges(data)
    if not ranges:
        return None
    data['fragment_selection_guard'] = guard(data, ranges)
    start, end = interval(fragment['coverage'], 'Prepared fragment coverage')
    if not any(start < right and end > left for left,right in ranges):
        return None
    if any(f['folder'] == fragment['folder'] for f in data.get('prepared_fragments', [])):
        return 'active'
    candidate = dict(fragment, activation_reason='explicit_selection_overlap')
    retired = [f for f in data.get('retired_fragments', []) if f['folder'] != fragment['folder']]
    data['retired_fragments'] = retired + [candidate]
    return 'candidate'


def registry(data):
    rows = {}
    for active, key in [(True, 'prepared_fragments'), (False, 'retired_fragments')]:
        for fragment in data.get(key, []):
            name = Path(fragment['folder']).name
            if name in rows:
                raise ValueError('Ambiguous fragment ID; preserve metadata for diagnosis')
            rows[name] = (active, fragment)
    return rows


def fragments(args):
    import course as c
    directory = c.lesson_path(args)
    keep = getattr(args, 'keep', None)
    if keep:
        if len(keep) > 500 or len(set(keep)) != len(keep):
            raise ValueError('Select 1..500 unique fragment IDs')
        if getattr(args, 'expected_revision', None) is None:
            raise ValueError('Selection requires --expected-revision from fragments')
        def select(data):
            if data.get('revision', 0) != args.expected_revision:
                raise ValueError('Lesson revision changed; read fragments again')
            rows = registry(data)
            if any(name not in rows for name in keep):
                raise ValueError('Unknown fragment ID')
            check_local_video(data['video'])
            chosen = [rows[name][1] for name in keep]
            ranges = []
            for fragment in chosen:
                load_fragment(fragment, directory, data['video']['sha256'])
                bounds = fragment['coverage']; ranges.append((bounds['start'], bounds['end']))
            # A switch must not discard any currently available interval.
            for old in data.get('prepared_fragments', []):
                bounds = old['coverage']
                if not coverage(ranges, bounds['start'], bounds['end'])[0]['complete']:
                    raise ValueError('Selection would create an unprepared interval')
            previous = [Path(f['folder']).name for f in data.get('prepared_fragments', [])]
            data['prepared_fragments'] = deepcopy(chosen)
            for fragment in data['prepared_fragments']:
                fragment.pop('activation_reason',None)
            data['retired_fragments'] = [deepcopy(f) for name, (_, f) in rows.items() if name not in keep]
            data.setdefault('fragment_selections', []).append(dict(previous=previous, selected=list(keep), at=c.now()))
            data['fragment_selection_guard'] = guard(data, ranges)
        c.update_lesson(directory, select)
    data = c.read(directory/'lesson.json')
    protected=protected_ranges(data)
    return dict(lesson=args.lesson, revision=data.get('revision',0),
        selection_protection=dict(enabled=bool(protected), ranges=[dict(start=s,end=e) for s,e in protected]), fragments=[
        dict(id=name, active=active, coverage=f['coverage'], packet_sha256=f.get('packet_sha256'),
             job_key=f.get('job_key'), activation_reason=f.get('activation_reason')) for name,(active,f) in registry(data).items()],
        limitations=['Selection verifies integrity and coverage, not ASR accuracy.',
                     'Overlapping prepares remain candidates after explicit selection; disjoint ranges may activate.',
                     'Packets are selected whole; mixed protected/new ranges require explicit selection.'])
