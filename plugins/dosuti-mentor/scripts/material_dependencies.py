"""Explicit, portable checkpoint dependencies on reviewed lesson materials."""
import hashlib
import json
from pathlib import Path

LISTS = {'code_stages', 'term_notes', 'resources', 'visual_notes', 'ocr_reviews'}


def selection(value):
    import course
    if not isinstance(value, dict) or not value or set(value) - (LISTS | {'homework'}):
        raise ValueError('Invalid dependency_materials fields')
    for kind, names in value.items():
        if kind == 'homework':
            if names is not True:
                raise ValueError('homework dependency must be true; omit it otherwise')
            continue
        if not isinstance(names, list) or not 1 <= len(names) <= 50:
            raise ValueError(kind + ' requires 1..50 unique IDs')
        for name in names:
            course.slug(name)
        if len(set(names)) != len(names):
            raise ValueError('Duplicate material IDs')
    return value


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate(value, chosen):
    from source_integrity import digest
    selection(chosen)
    if not isinstance(value, dict) or set(value) != {'schema', 'materials'} or value['schema'] != 'material_dependencies_v1':
        raise ValueError('Unknown material dependency schema')
    rows = value['materials']
    if not isinstance(rows, dict) or set(rows) != set(chosen):
        raise ValueError('Material dependency fields differ from selection')
    for kind, names in chosen.items():
        hashes = rows[kind]
        expected = {'task'} if kind == 'homework' else set(names)
        if not isinstance(hashes, dict) or set(hashes) != expected:
            raise ValueError('Material dependency IDs differ from selection')
        for sha in hashes.values(): digest(sha)
    return value


def collect(directory, data, chosen):
    import course
    from source_integrity import check_local_video
    selection(chosen)
    check_local_video(data['video'])
    result = {}
    for kind, names in chosen.items():
        values = {}
        if kind in ('code_stages', 'term_notes'):
            if kind == 'code_stages':
                from code_stages import checked
            else:
                from term_notes import checked
            records = data.get(kind, {})
            if any(name not in records for name in names):
                raise ValueError('Missing dependency ' + kind)
            # Keep all stages for overlap detection, but bind only selected records.
            scoped = data if kind == 'code_stages' else dict(data, term_notes={name:records[name] for name in names})
            rows = {r.get('id'): r for r in checked(directory, scoped) if r.get('id') in names}
            for name in names:
                row = rows[name]
                if row['stale']:
                    raise ValueError('Stale material dependency: ' + kind + '/' + name)
                semantic = {k: v for k, v in records[name].items() if k != 'updated'}
                if kind == 'code_stages':
                    semantic = dict(annotation=semantic, historical=row['historical'],
                                    overlaps=sorted(row['overlapping_stage_ids']))
                values[name] = checksum(semantic)
        elif kind == 'resources':
            from material_sources import describe, resource_revision
            items = data.get('resources', {}).get('items', [])
            for name in names:
                matches = [i for i in items if i.get('id') == name]
                if len(matches) != 1:
                    raise ValueError('Missing or ambiguous resource dependency: ' + name)
                item = describe(dict(items=matches))['items'][0]
                if not item.get('review_valid') or item.get('status') != 'reviewed':
                    raise ValueError('Resource dependency needs extraction and review: ' + name)
                values[name] = checksum(dict(revision=resource_revision(item),
                                            source=item['source'] if item['source'].startswith(('https://','http://')) else 'local', kind=item['kind'],
                                            review_notes=item['review']['notes']))
        elif kind == 'ocr_reviews':
            from ocr_review import dependency
            for name in names:values[name]=dependency(directory,name)
        elif kind == 'homework':
            from spoken_homework import resolve
            homework = resolve(directory, data)
            if course.homework_integrity(homework) != 'verified':
                raise ValueError('Homework dependency is missing or changed')
            values['task'] = checksum(dict(revision=course.homework_revision(homework)))
        else:
            from lecture_context import load_fragment, assemble
            fragments = data.get('prepared_fragments', [])
            for name in names:
                matches = [f for f in fragments if Path(f['folder']).name == name]
                if len(matches) != 1:
                    raise ValueError('Missing or ambiguous active visual-notes packet: ' + name)
                packet, source = load_fragment(matches[0], directory, data['video']['sha256'])
                path = Path(source['notes_file'])
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Visual notes are missing or linked')
                before = course.file_hash(path)
                bounds = packet['coverage']
                scoped = dict(data, prepared_fragments=matches)
                context = assemble(directory, scoped, bounds['start'], bounds['end'] - bounds['start'],
                                   include_code_stages=False, include_term_notes=False)
                for rows in (context['visual_notes']['observations'], context['visual_notes']['uncertainties']):
                    if any(r['provenance']['fragment_id'] == source['fragment_id'] and r.get('stale') for r in rows):
                        raise ValueError('Visual notes have stale code references')
                if course.file_hash(path) != before:
                    raise ValueError('Visual notes changed during dependency check')
                values[name] = before
        result[kind] = values
    return validate(dict(schema='material_dependencies_v1', materials=result), chosen)
