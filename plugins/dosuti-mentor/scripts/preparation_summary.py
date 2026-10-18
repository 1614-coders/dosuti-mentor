"""On-demand lesson summary; no periodic disk scan or invented timing/quality."""
from preparation_status import describe
from processing_status import describe as worker_status, LABELS
from storage_report import inventory


def summarize(directory):
    import course
    view = describe(directory)
    disk = inventory(directory, 10000, prefix=('lessons', directory.name))
    timings = view.get('stage_seconds')
    child = None
    if view.get('worker_key'):
        child = worker_status(directory / '.jobs' / course.slug(view['worker_key']))
        if child.get('started_at', 0) < view.get('started_at', 0): child = None
    result = dict(schema='preparation_summary_v1', preparation=view,
                  automatic_stage_seconds=timings,
                  worker_stage_seconds=child.get('stage_seconds') if child else None,
                  timing_note='Worker timings are nested inside worker time: do not sum them twice. Missing timings are unknown, not zero. Chat/wait time is not CPU time.',
                  storage=dict(scope='lesson_only', logical_bytes=disk['totals']['logical_bytes'],
                      allocated_file_bytes=disk['totals']['allocated_file_bytes'],
                      scan_complete=disk['scan_complete'], filesystem_free_bytes=disk['filesystem_free_bytes'],
                      groups=disk['groups'], skipped=disk['skipped'],
                      excluded='External videos, shared models/runtime and link targets; logical sizes include duplicate file paths.'),
                  cleanup=dict(automatic_deletion=False, safe_to_delete_bytes=None,
                      next_step='Inspect cleanup-job for working duplicates or compact plan for completed lesson media; verify the proposed plan before deletion.'),
                  material_quality=None)
    scope = view.get('review_scope')
    duration = view.get('actual_duration', view.get('requested_duration'))
    if scope is None and duration is not None: scope = [view.get('start', 0), view.get('start', 0) + duration]
    if scope is not None and view.get('status') in ('complete', 'reported'):
        from lecture_context import assemble
        try:
            context = assemble(directory, course.read(directory / 'lesson.json'), scope[0], scope[1] - scope[0])
            result['material_quality'] = context['quality_summary']
        except (OSError, ValueError, KeyError, TypeError) as error:
            result['quality_error'] = str(error)[:300]
    result['stage_labels'] = {key: LABELS[key] for key in set(timings or {}) | set((child or {}).get('stage_seconds', {})) if key in LABELS}
    return result
