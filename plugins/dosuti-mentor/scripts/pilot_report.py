"""Bounded complete export with machine-readable coverage; partial reports never authorize cleanup."""
import json
from pathlib import Path
from profile_storage import checked
from source_integrity import file_hash

MAX_FILE=2_000_000
MAX_TOTAL=30_000_000
MAX_EVENTS=50_000


def output_path(root, output):
    from pilot import checked_root
    path=checked_root(output) if output is not None else root/'TEST_REPORT.md'
    if path.suffix.lower()!='.md' or (path.is_relative_to(root) and path!=root/'TEST_REPORT.md'):
        raise ValueError('Choose the default report or an external Markdown file')
    if root.name=='run' and path==root.parent/'SETUP_REPORT.md': raise ValueError('Preserve the separate setup diagnostics')
    if path.exists() and (not path.is_file() or path.stat().st_nlink!=1): raise ValueError('Report target must be an unlinked regular file')
    return path


def collect(root):
    from pilot import literal
    lines=[];entries=[];used=0
    link_cache={}
    summary=dict(independent=0,with_hints=0,ungraded=0,skipped=0,incorrect=0,partial=0,stale_plans=0)
    def label(path):
        import os
        return Path(os.path.relpath(path,root)).as_posix()
    def entry(path):
        value=dict(path=label(path),detected=0,exported=0,status='pending')
        entries.append(value);return value
    def check_path(path):
        for parent in path.parents:
            checked(parent,True)
            if parent==root or parent==root.parent: break
        checked(path)
    def error(item,exc):
        item.update(status='error',error=str(exc)[:2000])
        title='Журнал времени недоступен' if item['path']=='activities.json' else 'Данные не полностью доступны:'
        lines.extend([title,literal(item),''])
    def emit(value):
        nonlocal used
        raw=json.dumps(value,ensure_ascii=False)
        used+=len(raw.encode('utf-8'))
        if used>MAX_TOTAL: raise ValueError('Report budget exceeded; preserve original data')
        lines.extend([literal(raw),''])
    def document(path, required=False, transform=None):
        check_path(path)
        if not path.exists() and not required: return None
        item=entry(path)
        try:
            if not path.is_file() or path.stat().st_size>MAX_FILE: raise ValueError('Missing or oversized journal')
            before=file_hash(path);value=json.loads(path.read_text(encoding='utf-8'))
            item['sha256']=before
            item['revision']=value.get('revision') if isinstance(value,dict) else None
            lines.extend([literal(label(path)),''])
            if transform: transform(value,item)
            else:
                item['detected']=len(value.get('events',[])) if isinstance(value,dict) and isinstance(value.get('events'),list) else 1
                emit(value);item['exported']=item['detected']
            if file_hash(path)!=before: raise ValueError('Journal changed during report export')
            item['status']='complete'
            return value
        except (ValueError,OSError,KeyError,TypeError) as exc: error(item,exc);return None
    lines+=['## Материалы и сохранённое обучение','',
            'Локальные снимки и записанные оценки не доказывают достоверность источника или самостоятельное усвоение.','']
    document(root/'pilot.json',True)
    document(root/'course/course.json',True)
    lines+=['## Время и операции','',
            'monotonic — измерено процессом; wall_clock — интервал между командами, включая паузы.',
            'Вложенные и параллельные интервалы нельзя складывать. Неизвестное время не равно нулю.','']
    activities=[]
    def timings(value,item):
        from pilot_activity import read
        data=read(root);activities.extend(data['activities'])
        item['detected']=len(activities);emit(data);item['exported']=item['detected']
    document(root/'activities.json',transform=timings)
    if not activities: lines+=['Замеры не записаны либо журнал недоступен.','']
    lines+=['## Теория и практика','','Оценки записаны проверяющим; основной учебный прогресс ведётся отдельно.','']
    def assessments(value,item):
        import pilot_assessment as assessment
        data=assessment.read(root);item['detected']=len(data['events'])
        emit({k:data[k] for k in ('schema','pilot_id','revision')})
        for lesson in sorted({e['value']['lesson'] for e in data['events'] if e['type']=='plan'}):
            plan=assessment.latest(data,lesson);changed=assessment.stale(root,plan)
            summary['stale_plans']+=bool(changed)
            lines.extend(['### Урок',literal(lesson),f"План {plan['revision']}; источники: {'ИЗМЕНЕНЫ/НЕДОСТУПНЫ' if changed else 'SHA256 совпадают'}",literal(changed),''])
            latest={e['value']['question']:e['value'] for e in data['events'] if e['type']=='answer' and e['value']['lesson']==lesson and e['value']['plan_revision']==plan['revision']}
            for kind in ('theory','practice'):
                qs=[q for q in plan['value']['questions'] if q['kind']==kind]
                answered=sum(q['id'] in latest and latest[q['id']]['verdict']!='skipped' for q in qs)
                correct=sum(q['id'] in latest and latest[q['id']]['verdict']=='correct' for q in qs) if not changed else 0
                lines.extend([f'{kind}: заданий {len(qs)}, отвечено {answered}, текущих оценок correct {correct}.',''])
            if not changed:
                for answer in latest.values():
                    verdict=answer['verdict']
                    key=('with_hints' if answer['hints'] else 'independent') if verdict=='correct' else verdict
                    if key in summary: summary[key]+=1
        for event in data['events']:
            emit(event);item['exported']+=1
            if event['type']=='answer':
                from pilot_links import reconcile
                emit(dict(answer_revision=event['revision'],learning_link=reconcile(root,event['value'],link_cache)))
    document(root/'assessment.json',transform=assessments)
    lines+=['## Подготовка среды, ошибки и повторы','']
    setup=[]
    def attempts(value,item):
        from pilot_setup import read
        data=read(root.parent);setup.extend(data['attempts'])
        item['detected']=len(setup)
        for attempt in setup: emit(attempt);item['exported']+=1
    if root.name=='run': document(root.parent/'setup.json',transform=attempts)
    course=root/'course'
    for name in ('preferences.json','course-preferences.json','learning-intent.json','cursor.json','unit-learning.json'):
        path=course/'learners/pilot'/name
        def unit(value,item):
            from learning_segments import event_stream
            schema,revision,events=event_stream(path,value)
            from unit_learning import SCHEMA, LEGACY_SCHEMA
            if schema not in (SCHEMA,LEGACY_SCHEMA) or type(revision) is not int or revision<0: raise ValueError('Invalid learning schema/revision')
            item.update(detected=revision,revision=revision)
            emit(dict(schema=value['schema'],revision=revision,segments=value.get('segments',[])))
            if value.get('segments'):
                for segment in value['segments']:
                    part=path.parent/'unit-learning-segments'/(segment['sha256']+'.json')
                    sub=entry(part);check_path(part);sub.update(sha256=file_hash(part),detected=segment['count'],exported=segment['count'],status='complete')
            count=0
            for event in events:
                count+=1
                if count>MAX_EVENTS: raise ValueError('Learning history exceeds report event budget')
                emit(event);item['exported']+=1
            item.update(detected=revision,revision=revision)
            if count!=revision: raise ValueError('Learning event count/revision mismatch')
        try: document(path,transform=unit if name=='unit-learning.json' else None)
        except (ValueError,OSError) as exc:
            item=entry(path);error(item,exc)
    lessons=course/'lessons'
    try:
        check_path_dir(lessons)
        if lessons.exists():
            folders=sorted(lessons.iterdir())
            if len(folders)>1000: raise ValueError('Lesson metadata budget exceeded')
            for folder in folders:
                check_path_dir(folder)
                if folder.is_dir(): document(folder/'lesson.json',True)
    except (ValueError,OSError) as exc:
        item=entry(lessons);error(item,exc)
    database=course/'learners/pilot/progress.sqlite3'
    try:
        check_path(database)
        if database.exists():
            item=entry(database)
            try:
                import sqlite3
                from contextlib import closing
                before=file_hash(database);item['sha256']=before
                sidecars={}
                for suffix in ('-wal','-shm','-journal'):
                    side=Path(str(database)+suffix);check_path(side)
                    if side.exists(): sidecars[str(side)]=file_hash(side)
                with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
                    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'events' not in tables: raise ValueError('Progress events table missing')
                    for table in ('events','progress','bindings'):
                        if table not in tables: continue
                        count=db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0];item['detected']+=count
                        if count>MAX_EVENTS: raise ValueError('Progress exceeds report event budget')
                        for row in db.execute(f'SELECT * FROM {table}'):
                            emit(dict(table=table,row=dict(row)));item['exported']+=1
                if file_hash(database)!=before: raise ValueError('Progress database changed during export')
                for name,expected in sidecars.items():
                    if file_hash(Path(name))!=expected: raise ValueError('SQLite sidecar changed during export')
                    entries.append(dict(path=label(Path(name)),sha256=expected,status='complete',detected=0,exported=0))
                item['status']='complete'
            except (ValueError,OSError,sqlite3.Error) as exc: error(item,exc)
    except (ValueError,OSError) as exc:
        item=entry(database);error(item,exc)
    coverage=dict(schema='pilot_export_coverage_v1',complete=all(e['status']=='complete' and e['detected']==e['exported'] for e in entries),entries=entries)
    return lines,coverage,dict(assessment=summary,activities=activities,setup=setup)


def check_path_dir(path):
    for parent in (path.parent,path): checked(parent,True)


def headline(data,coverage,info):
    from pilot import literal, STEPS
    latest={e['step']:e for e in data['events']}
    lines=['## Сводка','',f"Полнота экспорта: {'complete' if coverage['complete'] else 'INCOMPLETE — очистка запрещена'}",'',
           '| Этап | Результат |','|---|---|']
    for step in STEPS: lines.append(f"| {step} | {latest.get(step,{}).get('status','not_checked')} |")
    counts=info['assessment']
    lines+=['',f"Текущие записанные оценки: самостоятельно {counts['independent']}; с подсказками {counts['with_hints']}; без оценки {counts['ungraded']}; пропущено {counts['skipped']}; устаревших планов {counts['stale_plans']}.",
            '',f"Попыток установки: {len(info['setup'])}; неуспешных/прерванных: {sum(a['status'] in ('failed','interrupted') for a in info['setup'])}.",
            '', '| Операция | Метод | Статус | Секунды |','|---|---|---|---|']
    # Display each interval, never sum nested/parallel measurements.
    for item in info['activities']:
        seconds='неизвестно' if item['seconds'] is None else str(item['seconds'])
        lines.append(f"| {item['stage']} | {item['method']} | {item['status']} | {seconds} |")
    lines+=['','learner_wait — ожидание ученика. Интервалы нельзя складывать без учёта перекрытий.','', 'Отзыв человека:']
    feedback=[e['tester_feedback'] for e in data['events'] if e['tester_feedback']]
    lines+=[literal(value) for value in feedback] if feedback else ['    Пока не получен.']
    lines+=['','## Покрытие экспорта','',literal(json.dumps(coverage,ensure_ascii=False)),'']
    return lines


def sections(root): return collect(root)[0]
