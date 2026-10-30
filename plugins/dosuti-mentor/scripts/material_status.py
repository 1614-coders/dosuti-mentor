"""Read-only material lifecycle, projected from existing unit and preparation evidence."""


def unit_state(row):
    status = 'stale' if row['stale'] else ('limited' if row['uncertainties'] or row.get('fact_assurance',{}).get('needs_review') else 'ready')
    return dict(status=status, reuse_preparation=not row['stale'],
                content_sha256=row['content_sha256'],
                reasons=row['reasons'] if row['stale'] else row['uncertainties']+
                    (['recorded_material_fact_concerns'] if row.get('fact_assurance',{}).get('needs_review') else []),
                next_action='review_sources' if row['stale'] else 'open_learn_unit',
                readiness_basis='reviewed_unit_annotation_not_automatic_proof')


def lesson_state(directory, start, window):
    import course
    from lecture_context import assemble, number
    from preparation_status import describe
    from source_integrity import check_local_video
    start, window = number(start, 'time'), number(window, 'window')
    if start < 0 or window <= 0: raise ValueError('Require nonnegative time and positive window')
    data = course.read(directory / 'lesson.json')
    preparation = describe(directory)
    result = dict(status='selected', reuse_preparation=False, reasons=[],
                  scope=[start, start + window], preparation_status=preparation,
                  next_action='prepare_missing_scope')
    if preparation.get('status') == 'running':
        return dict(result, status='preparing', next_action='observe_preparation')
    try:
        if data['video'].get('sha256'): check_local_video(data['video'])
        if preparation.get('reason') == 'source_changed' or preparation.get('review_stale'):
            raise ValueError('Materials changed after preparation review')
        if not data['prepared_fragments']:
            if preparation.get('status') in ('failed', 'interrupted'):
                result.update(status='limited', reasons=[preparation.get('message', preparation['status'])])
            return result
        context = assemble(directory, data, start, window)
        result.update(coverage=context['coverage'], gaps=context['gaps'], warnings=context['warnings'])
        stale = [w for w in context['warnings'] if 'stale' in w]
        if stale: raise ValueError('; '.join(stale))
        complete = context['coverage']['complete']
        reviewed = preparation.get('review_scope', [])
        ready = (complete and preparation.get('teaching_ready') and len(reviewed) == 2
                 and reviewed[0] <= start and reviewed[1] >= start + window)
        result.update(status='ready' if ready else 'limited', reuse_preparation=complete,
                      next_action='open_context' if ready else ('review_context' if complete else 'prepare_missing_scope'),
                      reasons=[] if ready else ['scope_needs_review' if complete else 'unprepared_intervals'])
    except (ValueError, OSError, KeyError, TypeError) as error:
        result.update(status='stale', reuse_preparation=False, reasons=[str(error)], next_action='review_sources')
    return result


def view(args):
    import course
    from study_units import load, checked_units
    root = course.root(args)
    course.read(root / 'course.json')
    name, lesson = getattr(args, 'unit_id', None), getattr(args, 'lesson', None)
    if name and lesson: raise ValueError('Select either unit-id or lesson')
    if name:
        course.slug(name)
        data = load(root, include_history=False)
        if name not in data['units']: raise ValueError('Unknown study unit')
        row = checked_units(root, data, [name], include_contexts=False)[name]
        return dict(schema='material_status_v1', unit_id=name, material=unit_state(row))
    if lesson:
        return dict(schema='material_status_v1', lesson=lesson,
                    material=lesson_state(course.lesson_path(args), args.time, args.window))
    data = load(root, include_history=False)
    rows = checked_units(root, data, include_contexts=False)
    units = [dict(id=name, title=row['title'], material=unit_state(row)) for name, row in rows.items()]
    lessons = [dict(id=course.slug(path.parent.name), title=course.read(path)['title'])
               for path in sorted((root / 'lessons').glob('*/lesson.json'))]
    return dict(schema='material_status_v1', status='selected' if units or lessons else 'materials_missing',
                units=units, lessons=lessons, next_action='select_material' if units or lessons else 'find_material')
