"""Durable QA activity timings. Only explicit pilot courses are instrumented."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock

STAGES = ('download', 'preparation', 'audio', 'frames', 'model_load', 'asr', 'code_review',
          'materials', 'web_search', 'source_review', 'onboarding', 'teaching', 'theory',
          'practice', 'learner_wait', 'resume', 'other')
ENV = 'DOSUTI_QA_PILOT'


def timestamp(): return datetime.now(timezone.utc).isoformat()


def read(root):
    from pilot import load
    identity = load(root)['id']
    path = root / 'activities.json';checked(path)
    if not path.exists(): return dict(schema='pilot_activities_v1', pilot_id=identity, revision=0, activities=[])
    if path.stat().st_size > 2_000_000: raise ValueError('Activity journal too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value,dict) or value.get('schema') != 'pilot_activities_v1' or value.get('pilot_id') != identity
            or type(value.get('revision')) is not int or not isinstance(value.get('activities'), list)
            or len(value['activities']) > 2000): raise ValueError('Invalid activity journal')
    ids = set()
    for item in value['activities']:
        if (not isinstance(item, dict) or item.get('stage') not in STAGES or item.get('id') in ids
                or not isinstance(item.get('id'), str) or item.get('status') not in ('running','passed','failed','interrupted')
                or item.get('method') not in ('monotonic','wall_clock')
                or any(not isinstance(item.get(k), str) for k in ('started','session','detail'))):
            raise ValueError('Invalid activity record')
        if item.get('parent') is not None and item['parent'] not in ids: raise ValueError('Invalid parent activity')
        ids.add(item['id'])
        try: datetime.fromisoformat(item['started'])
        except (ValueError,TypeError): raise ValueError('Invalid activity timestamp')
        if (item['status']=='running') != (item.get('seconds') is None): raise ValueError('Invalid activity completion')
        seconds = item.get('seconds')
        if seconds is not None and (type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds < 0):
            raise ValueError('Invalid activity duration')
    if value['revision'] != len(value['activities']) + sum(a['status']!='running' for a in value['activities']):
        raise ValueError('Activity revision mismatch')
    return value


def write(root, value):
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 1_900_000:
        raise ValueError('Activity journal limit reached; export the report')
    atomic_json(root / 'activities.json', value)


def start(root, stage, session='', detail='', method='wall_clock', parent=None):
    from pilot import checked_root, load
    root = checked_root(root);load(root)
    if stage not in STAGES or method not in ('monotonic','wall_clock'): raise ValueError('Unknown activity stage/method')
    if any(not isinstance(s,str) or len(s)>2000 for s in (session,detail)): raise ValueError('Activity text too large')
    with file_lock(root / '.activities.lock'):
        value = read(root)
        if len(value['activities']) >= 2000: raise ValueError('Activity limit reached')
        if parent is not None and not any(a['id']==parent and a['status']=='running' for a in value['activities']):
            raise ValueError('Activity parent must be running')
        item = dict(id=uuid.uuid4().hex, stage=stage, session=session, detail=detail,
                    method=method, parent=parent, started=timestamp(), status='running', seconds=None)
        value['activities'].append(item);value['revision']+=1;write(root,value)
        return item


def finish(root, activity, status='passed', detail='', seconds=None):
    from pilot import checked_root
    root = checked_root(root)
    if status not in ('passed','failed','interrupted'): raise ValueError('Unknown activity result')
    if not isinstance(detail,str) or len(detail)>2000: raise ValueError('Activity detail too large')
    with file_lock(root / '.activities.lock'):
        value = read(root)
        item = next((a for a in value['activities'] if a['id']==activity), None)
        if item is None or item['status']!='running': raise ValueError('Activity missing or already finished')
        ended = timestamp()
        if item['method']=='wall_clock':
            seconds = (datetime.fromisoformat(ended)-datetime.fromisoformat(item['started'])).total_seconds()
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Invalid duration; preserve the open activity')
        item.update(finished=ended, status=status, seconds=round(seconds,6), result=detail)
        value['revision']+=1;write(root,value)
        return item


@contextmanager
def span(stage, root=None, scope=None):
    selected = root or os.environ.get(ENV)
    if not selected:
        yield;return
    from pilot import checked_root
    selected = checked_root(selected)
    if scope is not None and not Path(scope).resolve().is_relative_to(selected):
        yield;return
    item = start(selected, stage, method='monotonic', parent=os.environ.get('DOSUTI_QA_PARENT'))
    before = time.monotonic()
    old = os.environ.get('DOSUTI_QA_PARENT');os.environ['DOSUTI_QA_PARENT']=item['id']
    try:
        yield
    except BaseException as error:
        finish(selected,item['id'],'interrupted' if isinstance(error,(KeyboardInterrupt,SystemExit)) else 'failed',
               type(error).__name__,time.monotonic()-before)
        raise
    else: finish(selected,item['id'],seconds=time.monotonic()-before)
    finally:
        if old is None: os.environ.pop('DOSUTI_QA_PARENT',None)
        else: os.environ['DOSUTI_QA_PARENT']=old


@contextmanager
def course_scope(course, command):
    """No change to ordinary courses; children inherit only a validated QA root."""
    folder = Path(course).expanduser().absolute() if course is not None else None
    root = folder.parent if folder is not None else None
    if folder is None or folder.name!='course' or not (root/'pilot.json').exists():
        # Never leak an inherited QA timer into another course.
        old=os.environ.pop(ENV,None);parent=os.environ.pop('DOSUTI_QA_PARENT',None)
        try: yield
        finally:
            if old is not None: os.environ[ENV]=old
            if parent is not None: os.environ['DOSUTI_QA_PARENT']=parent
        return
    from pilot import checked_root, load
    root=checked_root(root);load(root)
    old=os.environ.get(ENV);parent=os.environ.pop('DOSUTI_QA_PARENT',None)
    os.environ[ENV]=str(root)
    stages={'download-video':'download','prepare':'preparation','research':'materials',
            'code-stage':'code_review','diff-code':'code_review','attach':'materials'}
    try:
        with file_lock(root/'.pilot-use.lock',timeout=.1):
            if command in stages:
                with span(stages[command],root): yield
            else: yield
    finally:
        if old is None: os.environ.pop(ENV,None)
        else: os.environ[ENV]=old
        if parent is not None: os.environ['DOSUTI_QA_PARENT']=parent
