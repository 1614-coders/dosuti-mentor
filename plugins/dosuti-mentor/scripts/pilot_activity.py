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
        unknown=item['status']=='interrupted' and item.get('duration_state')=='unknown'
        if not unknown and (item['status']=='running') != (item.get('seconds') is None): raise ValueError('Invalid activity completion')
        if unknown and item.get('seconds') is not None: raise ValueError('Recovered duration must stay unknown')
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


def start(root, stage, session='', detail='', method='wall_clock', parent=None, lease=None):
    from pilot import checked_root, load
    root = checked_root(root);load(root)
    if stage not in STAGES or method not in ('monotonic','wall_clock'): raise ValueError('Unknown activity stage/method')
    if any(not isinstance(s,str) or len(s)>2000 for s in (session,detail)): raise ValueError('Activity text too large')
    with file_lock(root / '.activities.lock'):
        value = read(root)
        if len(value['activities']) >= 2000: raise ValueError('Activity limit reached')
        if parent is not None and not any(a['id']==parent and a['status']=='running' for a in value['activities']):
            raise ValueError('Activity parent must be running')
        item = dict(id=lease or uuid.uuid4().hex, stage=stage, session=session, detail=detail,
                    method=method, parent=parent, started=timestamp(), status='running', seconds=None)
        if lease is not None: item['lease']=lease
        value['activities'].append(item);value['revision']+=1;write(root,value)
        return item


def finish(root, activity, status='passed', detail='', seconds=None, session='', allow_unknown_owner=False):
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
        if item['method']=='monotonic' and seconds is None:
            if status!='interrupted' or not detail.strip(): raise ValueError('Recovery requires interrupted and a reason; duration is unknown')
            if not isinstance(session,str) or len(session)>160: raise ValueError('Invalid recovery session')
            from contextlib import nullcontext
            lease=item.get('lease')
            if lease is None and not allow_unknown_owner:
                raise ValueError('Legacy activity owner unknown; verify no worker is running, then use --allow-unknown-owner explicitly')
            if lease is not None:
                if lease!=item['id'] or len(lease)!=32 or any(c not in '0123456789abcdef' for c in lease): raise ValueError('Invalid activity lease')
                lease_path=root/'activity-leases'/(lease+'.lock')
                checked(lease_path.parent,True);checked(lease_path)
                if not lease_path.is_file(): raise ValueError('Activity lease missing; preserve the journal')
                lease_guard=file_lock(lease_path,timeout=.1,create_parent=False)
            else: lease_guard=nullcontext()
            with lease_guard:
                item.update(finished=ended,status='interrupted',seconds=None,duration_state='unknown',
                            recovered_at=ended,recovered_session=session,result=detail,
                            owner_verification='lease_released' if lease else 'legacy_owner_unverifiable')
        else:
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
    from contextlib import nullcontext
    from pilot_lifecycle import guard
    # A processing child inherits its parent operation; its .processing.lock and
    # the parent's lifecycle lock cover it without deadlocking the parent process.
    operation_guard=nullcontext() if scope is not None and os.environ.get('DOSUTI_QA_PARENT') else guard(selected)
    with operation_guard:
        leases=selected/'activity-leases';checked(leases,True);leases.mkdir(exist_ok=True)
        lease=uuid.uuid4().hex
        with file_lock(leases/(lease+'.lock'),create_parent=False):
            item = start(selected, stage, method='monotonic', parent=os.environ.get('DOSUTI_QA_PARENT'),lease=lease)
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
    if folder is not None and folder.name=='course':
        from pilot_lifecycle import active
        active(root)
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
        from pilot_lifecycle import guard
        with guard(root,timeout=.1):
            if command in stages:
                with span(stages[command],root): yield
            else: yield
    finally:
        if old is None: os.environ.pop(ENV,None)
        else: os.environ[ENV]=old
        if parent is not None: os.environ['DOSUTI_QA_PARENT']=parent
