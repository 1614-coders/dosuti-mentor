"""One local pilot report, including partial diagnostics and explicit evidence limits."""
import json
from pathlib import Path
from atomic_files import atomic_text
from profile_storage import checked


def sections(root):
    from pilot import literal
    lines=['## Время и операции', '',
           'monotonic — измерено процессом; wall_clock — интервал между командами чата, включая паузы.',
           'Вложенные и параллельные интервалы нельзя складывать в общее время. Неизмеренное не равно нулю.', '']
    try:
        from pilot_activity import read
        values=read(root)['activities']
        if not values: lines+=['Замеры не записаны (в том числе для старых прогонов).', '']
        for item in values:
            lines += [literal(json.dumps(item,ensure_ascii=False)), '']
    except (ValueError,OSError,KeyError,TypeError) as error:
        lines += ['Журнал времени недоступен; результаты не подтверждены:',literal(str(error)), '']
    lines += ['## Теория и практика', '',
              'Оценки ниже — записанная проверка ответов ассистентом/преподавателем, не автоматическое доказательство усвоения.',
              'Пустые, пропущенные и устаревшие ответы не засчитываются. Основной учебный прогресс ведётся отдельно.', '']
    try:
        import pilot_assessment as assessment
        data=assessment.read(root)
        lessons={e['value']['lesson'] for e in data['events'] if e['type']=='plan'}
        if not lessons: lines+=['План и ответы ещё не записаны.', '']
        for lesson in sorted(lessons):
            plan=assessment.latest(data,lesson);changed=assessment.stale(root,plan)
            lines += ['### Урок', literal(lesson), f"План: ревизия {plan['revision']}; состояние источников: {'ИЗМЕНЕНЫ/НЕДОСТУПНЫ' if changed else 'SHA256 совпадают'}",literal(changed),'']
            answers=[e for e in data['events'] if e['type']=='answer' and e['value']['lesson']==lesson and e['value']['plan_revision']==plan['revision']]
            for kind in ('theory','practice'):
                qs=[q for q in plan['value']['questions'] if q['kind']==kind]
                latest={e['value']['question']:e['value'] for e in answers}
                responded=sum(q['id'] in latest and latest[q['id']]['verdict']!='skipped' for q in qs)
                correct=sum(q['id'] in latest and latest[q['id']]['verdict']=='correct' for q in qs) if not changed else 0
                lines += [f'{kind}: заданий {len(qs)}, отвечено {responded}, текущих оценок correct {correct}.', '']
        # Retain all plan/answer revisions, hints, provenance, pin hashes, session and actual wording.
        for event in data['events']:
            lines += [literal(json.dumps(event,ensure_ascii=False)), '']
    except (ValueError,OSError,KeyError,TypeError) as error:
        lines+=['История проверки недоступна; успешность не установлена:',literal(str(error)), '']
    lines += ['## Подготовка среды, ошибки и повторы', '']
    setup=root.parent/'setup.json'
    if root.name=='run' and setup.exists():
        try:
            from pilot_setup import read
            data=read(root.parent)
            for attempt in data['attempts']:
                # Only include attempts explicitly linked here, plus earlier failed bootstrap attempts.
                if attempt.get('pilot') and Path(attempt['pilot']).resolve()!=root: continue
                lines += [literal(json.dumps(attempt,ensure_ascii=False)), '']
        except (ValueError,OSError,KeyError,TypeError) as error:
            lines+=['Диагностика установки недоступна:',literal(str(error)), '']
    else: lines+=['Журнал установки не найден; установка не подтверждена.', '']
    lines += ['## Материалы и сохранённое обучение', '',
              'Снимки локальных метаданных и журналов. Наличие ссылки не доказывает сверку с видео.', '']
    # Known course paths only, bounded reads; no recursive scan of videos or arbitrary home folders.
    course=root/'course';checked(course,True)
    paths=[course/'learners'/'pilot'/name for name in ('preferences.json','course-preferences.json','learning-intent.json','cursor.json','unit-learning.json')]
    lessons=course/'lessons';checked(lessons,True)
    if lessons.is_dir():
        entries=sorted(lessons.iterdir())
        if len(entries)>100: lines+=['Показаны только первые 100 уроков.', '']
        for folder in entries[:100]:
            checked(folder,True)
            if folder.is_dir(): paths.append(folder/'lesson.json')
    used=0
    for path in paths:
        for parent in path.parents:
            if parent==root: break
            checked(parent,True)
        checked(path)
        if not path.is_file(): continue
        size=path.stat().st_size
        if size>300_000 or used+size>1_000_000:
            lines += [literal(str(path.relative_to(root))), 'Слишком большой файл: не включён. Полная история остаётся в курсе.', ''];continue
        used+=size
        try: value=json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,OSError) as error:
            lines += [literal(str(path.relative_to(root))),literal(str(error)), ''];continue
        if path.name=='unit-learning.json':
            try:
                from learning_segments import event_stream
                from itertools import islice
                schema,revision,events=event_stream(path,value)
                captured=list(islice(events,501))
                value=dict(schema=schema,revision=revision,events=captured[:500],truncated=len(captured)>500)
            except (ValueError,OSError,KeyError,TypeError) as error:
                lines += ['Учебная история не проверена:',literal(str(error)), ''];continue
        lines += [literal(str(path.relative_to(root))),literal(json.dumps(value,ensure_ascii=False)), '']
    database=course/'learners'/'pilot'/'progress.sqlite3'
    for parent in (database.parent.parent,database.parent): checked(parent,True)
    checked(database)
    if database.is_file():
        try:
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=.1)) as connection:
                connection.row_factory=sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                rows=[dict(row) for row in connection.execute('SELECT * FROM events ORDER BY id DESC LIMIT 501')]
            lines += ['Обычный журнал прогресса (исторические события; актуальность домашки сверяется учебными командами):',
                      literal(json.dumps(dict(events=rows[:500],truncated=len(rows)>500),ensure_ascii=False)), '']
        except (OSError,sqlite3.Error) as error:
            lines += ['Журнал прогресса недоступен:',literal(str(error)), '']
    lines += ['## Завершение и удаление', '',
              'Перед передачей просмотрите личные данные, ответы и пути. Отчёт отправляется только вручную.',
              'cleanup-plan/apply удаляют только собственную тестовую папку после экспорта наружу.',
              'Плагин, источник marketplace и история чатов Codex удаляются отдельно; этот отчёт не подтверждает их удаление.',
              'Общие Python/Git, внешние модели/runtime, чужие курсы и экспортированный отчёт сохраняются.', '']
    return lines


def output_path(root, output):
    from pilot import checked_root
    path=checked_root(output) if output is not None else root/'TEST_REPORT.md'
    if path.suffix.lower()!='.md' or (path.is_relative_to(root) and path!=root/'TEST_REPORT.md'):
        raise ValueError('Choose the default report or an external Markdown file')
    if root.name=='run' and path==root.parent/'SETUP_REPORT.md': raise ValueError('Preserve the separate setup diagnostics')
    if path.exists() and (not path.is_file() or path.stat().st_nlink!=1): raise ValueError('Report target must be an unlinked regular file')
    return path
