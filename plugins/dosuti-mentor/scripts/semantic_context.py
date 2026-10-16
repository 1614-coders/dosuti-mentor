"""Retrieve assistant-reviewed teaching units without guessing semantic boundaries."""
from copy import deepcopy


def validate_boundaries(value):
    from learning_tools import text
    if not isinstance(value, dict) or set(value) != {'start', 'end', 'notes'}:
        raise ValueError('boundary_review requires start, end, notes')
    for edge in ('start', 'end'):
        if value[edge] not in ('complete', 'continuation', 'unknown'):
            raise ValueError('Boundary must be complete, continuation or unknown')
    text(value['notes'], 'boundary review notes', 4000)
    if not value['notes'].strip():
        raise ValueError('Boundary review needs an explanation')


def select(directory, data, topic_id):
    from learning_tools import checked_topics
    from lecture_context import assemble
    import course
    course.slug(topic_id)
    row = next((r for r in checked_topics(directory, deepcopy(data)) if r['id'] == topic_id), None)
    if row is None:
        raise ValueError('Unknown topic ID')
    if row['stale']:
        raise ValueError('Topic is stale; review and save it again')
    review = row.get('boundary_review')
    validate_boundaries(review)
    if review['start'] != 'complete' or review['end'] != 'complete':
        raise ValueError('Topic boundaries are not complete; inspect adjacent context first')
    result = assemble(directory, data, row['start'], row['end']-row['start'])
    if not result['coverage']['complete']:
        raise ValueError('Topic has unprepared intervals')
    result['teaching_unit'] = dict(id=row['id'], title=row['title'], summary=row['summary'],
        boundary_review=review, verification='assistant_review_not_automatic_semantic_proof')
    return result


def boundary_speech(speech, start, stop):
    """Expose segments crossing a navigation window; their text is already preserved."""
    return [dict(start=row['start'], end=row['end'], text=row['text'],
                 crosses_start=row['start'] < start, crosses_end=row['end'] > stop)
            for row in speech if row['start'] < start or row['end'] > stop]
