"""Read-only, explicit references to ordinary learning events. Never writes grades."""
import json
from pathlib import Path
import sqlite3
from contextlib import closing
from profile_storage import checked
from source_integrity import digest, file_hash


def validate(ref):
    if ref is None: return
    if isinstance(ref,dict) and set(ref)=={'kind','reason'} and ref['kind']=='not_applicable':
        if not isinstance(ref['reason'],str) or not ref['reason'].strip() or len(ref['reason'])>2000: raise ValueError('Link needs reason')
        return
    fields={'kind','profile','target','event','source_sha256'}
    if not isinstance(ref,dict) or set(ref)!=fields or ref['kind'] not in ('unit','lesson'):
        raise ValueError('Invalid learning reference')
    from course import slug
    slug(ref['profile']);slug(ref['target']);digest(ref['source_sha256'])
    if type(ref['event']) is not int or ref['event']<1: raise ValueError('Invalid linked event ID')


def reconcile(root,answer,cache=None):
    ref=answer.get('learning_ref')
    if ref is None: return dict(status='unlinked',reason='No explicit learning event reference')
    try: validate(ref)
    except (ValueError,TypeError,KeyError) as error: return dict(status='mismatch',reason=str(error))
    if ref['kind']=='not_applicable': return dict(status='not_applicable',reason=ref['reason'])
    if ref['profile']!='pilot': return dict(status='mismatch',reason='Reference belongs to another learner; no other profile was read')
    course=root/'course';folder=course/'learners/pilot'
    for p in (course,folder.parent,folder): checked(p,True)
    cache={} if cache is None else cache
    try:
        if ref['kind']=='unit':
            path=folder/'unit-learning.json';checked(path)
            if not path.exists(): return dict(status='not_recorded',reason='Learning journal not created')
            if 'unit_events' not in cache:
                from unit_learning import load
                cache['unit_events']=load(path)['events']
            event=next((e for e in cache['unit_events'] if e['revision']==ref['event']),None)
            if event is None: return dict(status='not_recorded',reason='Learning event not found')
            if event['unit']!=ref['target'] or event['type']!='assessment': return dict(status='mismatch',reason='Wrong unit or event type')
            if event['unit_sha256']!=ref['source_sha256']: return dict(status='stale',reason='Recorded learning event used another source revision')
            key=('unit',ref['target'])
            if key not in cache:
                from study_units import load, checked_units
                units=load(course,include_history=False)
                cache[key]=checked_units(course,units,[ref['target']])[ref['target']]
            unit=cache[key]
            if unit['stale'] or unit['content_sha256']!=ref['source_sha256']: return dict(status='stale',reason='Current unit/source changed')
            value=event['value']
            if (value.get('answer')!=answer['answer'] or value.get('hints')!=answer['hints']
                    or (event.get('session') is not None and event['session']!=answer['session'])):
                return dict(status='mismatch',reason='Answer, hints or session differ from linked attempt')
            if answer['verdict']=='correct' and value.get('stage') not in ('solved_independently','checked_later','practice_with_help'):
                return dict(status='mismatch',reason='Recorded learning stage does not support the QA verdict')
            return dict(status='linked',kind='unit',event=event['revision'])
        if ref['target']!=answer['lesson']: return dict(status='mismatch',reason='Wrong lesson')
        metadata=course/'lessons'/ref['target']/'lesson.json'
        for parent in (metadata.parent.parent,metadata.parent): checked(parent,True)
        checked(metadata)
        if not metadata.is_file() or file_hash(metadata)!=ref['source_sha256']:
            return dict(status='stale',reason='Lesson metadata changed or is missing')
        path=folder/'progress.sqlite3';checked(path)
        if not path.is_file(): return dict(status='not_recorded',reason='Progress database not created')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
            row=db.execute('SELECT * FROM events WHERE id=?',(ref['event'],)).fetchone()
        if row is None: return dict(status='not_recorded',reason='Progress event not found')
        if row['lesson']!=ref['target']: return dict(status='mismatch',reason='Linked progress event belongs to another lesson')
        return dict(status='linked',kind='lesson',event=row['id'],
                    limitation='Legacy lesson progress has no recorded source hash or per-question answer; only explicit ID and current metadata are verified')
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error) as error:
        return dict(status='unverifiable',reason=str(error)[:1000])
