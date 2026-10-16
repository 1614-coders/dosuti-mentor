"""Explicit, versioned topic dependencies for optional learning checkpoints."""
import hashlib
import json


def topic_ids(value):
    import course
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError('dependency_topics requires 1..50 unique topic IDs')
    for name in value:
        course.slug(name)
    if len(set(value)) != len(value):
        raise ValueError('dependency_topics must be unique')
    return value


def validate_evidence(value, names):
    from source_integrity import digest
    if not isinstance(value, dict) or set(value) != {'schema', 'topics'} or value['schema'] != 'topic_dependencies_v1':
        raise ValueError('Unknown checkpoint dependency schema')
    rows = value['topics']
    if not isinstance(rows, list) or len(rows) != len(names):
        raise ValueError('Invalid checkpoint dependencies')
    for name, row in zip(names, rows):
        fields = {'id', 'semantic_sha256', 'source_sha256', 'packet_hashes', 'code_sha256'}
        if not isinstance(row, dict) or set(row) != fields or row['id'] != name:
            raise ValueError('Invalid checkpoint dependency fields')
        digest(row['semantic_sha256']); digest(row['source_sha256'])
        if row['code_sha256'] is not None:
            digest(row['code_sha256'])
        if not isinstance(row['packet_hashes'], list) or not row['packet_hashes']:
            raise ValueError('Invalid dependency packet hashes')
        for checksum in row['packet_hashes']:
            digest(checksum)
    return value


def packet_hashes(evidence):
    return sorted({checksum for row in evidence['topics'] for checksum in row['packet_hashes']})


def collect(directory, data, names, position):
    from learning_tools import checked_topics, text, strings
    from lecture_context import number
    from semantic_context import validate_boundaries
    from source_integrity import check_local_video
    topic_ids(names)
    check_local_video(data['video'])
    records = data.get('topics', {})
    if not isinstance(records, dict):
        raise ValueError('Invalid lesson topics')
    rows = []
    contains_position = False
    for name in names:
        row = records.get(name)
        if not isinstance(row, dict) or row.get('id') != name:
            raise ValueError('Missing dependency topic: ' + name)
        fields = ('id', 'title', 'summary', 'keywords', 'start', 'end', 'code_files')
        if not set(fields) <= set(row):
            raise ValueError('Invalid dependency topic fields')
        semantic = {key: row[key] for key in fields}
        if 'code_references' in row:
            from code_repositories import references
            semantic['code_references'] = references(row['code_references'], data, stored=True)
        text(semantic['title'], 'title', 200); text(semantic['summary'], 'summary')
        strings(semantic['keywords'], 'keywords'); strings(semantic['code_files'], 'code_files')
        number(row['start'], 'start'); number(row['end'], 'end')
        if row['start'] < 0 or row['end'] <= row['start']:
            raise ValueError('Invalid dependency topic interval')
        if 'boundary_review' in row:
            validate_boundaries(row['boundary_review'])
            semantic['boundary_review'] = row['boundary_review']
        # Validate only selected topics, while retaining all packets for seam/overlap checks.
        selected = dict(data, topics={name: dict(row)})
        if checked_topics(directory, selected)[0]['stale']:
            raise ValueError('Stale dependency topic: ' + name)
        if row['code_files'] and any(item not in data['code']['inventory'] for item in row['code_files']):
            raise ValueError('Dependency references an unknown snapshot file')
        contains_position |= row['start'] <= position <= row['end']
        checksum = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()
        rows.append(dict(id=name, semantic_sha256=checksum, source_sha256=row['source_sha256'],
                         packet_hashes=row['packet_hashes'], code_sha256=row.get('code_sha256') if row['code_files'] else None))
    if not contains_position:
        raise ValueError('Checkpoint time must belong to a dependency topic')
    return validate_evidence(dict(schema='topic_dependencies_v1', topics=rows), names)
