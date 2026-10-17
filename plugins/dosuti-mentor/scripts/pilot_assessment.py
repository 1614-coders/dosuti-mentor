"""Source-pinned pilot question plans and actual answers; never a mastery grade."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock
from pilot_activity import timestamp
from pilot_lifecycle import protected


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): result.update(block)
    return result.hexdigest()


def source(root, name):
    if not isinstance(name,str) or '\\' in name or not name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
        raise ValueError('Source must be a course-relative file')
    path=root/'course'/name
    for part in (path,*path.parents):
        if part.is_symlink(): raise ValueError('Linked assessment source')
    if not path.is_file() or path.stat().st_size > 20_000_000: raise ValueError('Missing or oversized source')
    return digest(path)


def read(root):
    from pilot import load
    identity=load(root)['id'];path=root/'assessment.json';checked(path)
    if not path.exists(): return dict(schema='pilot_assessment_v1',pilot_id=identity,revision=0,events=[])
    if path.stat().st_size>2_000_000: raise ValueError('Assessment journal too large')
    value=json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value,dict) or value.get('schema')!='pilot_assessment_v1' or value.get('pilot_id')!=identity
            or type(value.get('revision')) is not int or not isinstance(value.get('events'),list)
            or value['revision']!=len(value['events']) or len(value['events'])>500):
        raise ValueError('Invalid assessment history')
    for i,event in enumerate(value['events'],1):
        if (not isinstance(event,dict) or event.get('revision')!=i or event.get('type') not in ('plan','answer')
                or not isinstance(event.get('value'),dict) or not isinstance(event.get('at'),str)):
            raise ValueError('Invalid assessment event')
        v=event['value']
        if not isinstance(v.get('lesson'),str): raise ValueError('Invalid assessment lesson')
        if event['type']=='plan' and (not isinstance(v.get('pins'),dict) or not isinstance(v.get('questions'),list)):
            raise ValueError('Invalid stored assessment plan')
        if event['type']=='answer' and any(k not in v for k in ('plan_revision','question','session','answer','hints','verdict','evaluation')):
            raise ValueError('Invalid stored answer')
    return value


def text(value, limit=8000):
    if not isinstance(value,str) or not value.strip() or len(value)>limit: raise ValueError('Expected nonempty bounded text')
    return value


def validate_plan(root, value):
    if not isinstance(value,dict) or set(value)!={'lesson','sources','questions'}: raise ValueError('Plan needs lesson, sources, questions')
    text(value['lesson'],160)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,160}',value['lesson']): raise ValueError('Invalid lesson ID')
    if not isinstance(value['sources'],list) or not 1<=len(value['sources'])<=30: raise ValueError('Plan needs 1..30 source files')
    if not isinstance(value['questions'],list): raise ValueError('Questions must be a list')
    pins={name:source(root,name) for name in value['sources']}
    extra={}
    metadata_name='lessons/'+value['lesson']+'/lesson.json'
    metadata=root/'course'/metadata_name
    if metadata.exists():
        pins[metadata_name]=source(root,metadata_name)
        lesson=json.loads(metadata.read_text(encoding='utf-8'))
        from spoken_homework import resolve
        homework=resolve(metadata.parent,lesson)
        from course import homework_revision
        extra['homework_revision']=homework_revision(homework)
        if homework.get('status')=='needs_review': raise ValueError('Resolve uncertain homework before planning assessment')
        if homework.get('status')=='provided':
            if not any(q.get('homework') is True for q in value['questions'] if isinstance(q,dict)):
                raise ValueError('Include provided teacher homework in the practice plan')
            from course import homework_integrity
            if homework_integrity(homework)!='verified':
                raise ValueError('Teacher homework integrity is not verified')
            local=homework.get('file')
            if local:
                path=Path(local).expanduser().resolve()
                if not path.is_relative_to(root/'course'): raise ValueError('Copy homework into the managed course first')
                name=path.relative_to(root/'course').as_posix();pins[name]=source(root,name)

    questions=value['questions']
    if not isinstance(questions,list) or not 10<=len(questions)<=20: raise ValueError('Need 5..10 theory and 5..10 practice questions')
    ids=set()
    for q in questions:
        if not isinstance(q,dict) or set(q)!={'id','kind','prompt','origin','source','criteria','homework','provenance'}:
            raise ValueError('Invalid question fields')
        if not isinstance(q['id'],str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',q['id']) or q['id'] in ids:
            raise ValueError('Question IDs must be unique')
        ids.add(q['id']);text(q['prompt']);text(q['criteria']);text(q['provenance'])
        if q['kind'] not in ('theory','practice') or q['origin'] not in ('teacher','assistant','external') or type(q['homework']) is not bool:
            raise ValueError('Invalid question kind/origin/homework')
        if q['source'] not in pins: raise ValueError('Question must reference a pinned course source')
        if q['homework'] and (q['kind']!='practice' or q['origin']!='teacher'):
            raise ValueError('Homework must be teacher practice')
        # External material itself is captured in a pinned course file; provenance records URL/review.
        if q['origin']=='external' and not re.search(r'https?://\S+',q['provenance']):
            raise ValueError('External practice needs its source URL and review notes')
    if any(not 5<=sum(q['kind']==kind for q in questions)<=10 for kind in ('theory','practice')):
        raise ValueError('Need 5..10 theory and 5..10 practice questions')
    return dict(**value,pins=pins,**extra)


def latest(data, lesson):
    return next((e for e in reversed(data['events']) if e['type']=='plan' and e['value']['lesson']==lesson),None)


def stale(root, plan):
    result=[]
    for name,expected in plan['value']['pins'].items():
        try: matches=source(root,name)==expected
        except (OSError,ValueError): matches=False
        if not matches: result.append(name)
    if 'homework_revision' in plan['value']:
        metadata=root/'course/lessons'/plan['value']['lesson']/'lesson.json'
        try:
            from spoken_homework import resolve
            from course import homework_revision, homework_integrity
            current=resolve(metadata.parent,json.loads(metadata.read_text(encoding='utf-8')))
            if (homework_revision(current)!=plan['value']['homework_revision']
                    or current.get('status')=='needs_review'
                    or (current.get('status')=='provided' and homework_integrity(current)!='verified')):
                result.append('homework_revision')
        except (OSError,ValueError,KeyError,TypeError): result.append('homework_revision')
    return result


@protected
def record(root, revision, kind, value):
    from pilot import checked_root
    root=checked_root(root);read(root)
    with file_lock(root/'.assessment.lock'):
        data=read(root)
        if type(revision) is not int or revision!=data['revision']: raise ValueError('Assessment revision conflict')
        if len(data['events'])>=500: raise ValueError('Assessment history limit reached')
        if kind=='plan': saved=validate_plan(root,value)
        elif kind=='answer':
            if not isinstance(value,dict) or set(value)-{'learning_ref'}!={'lesson','plan_revision','question','session','answer','hints','verdict','evaluation'}:
                raise ValueError('Invalid answer fields')
            plan=latest(data,value['lesson'])
            if plan is None or type(value['plan_revision']) is not int or plan['revision']!=value['plan_revision']:
                raise ValueError('Use the current plan revision')
            if stale(root,plan): raise ValueError('Source changed; prepare a new plan before assessment')
            if not any(q['id']==value['question'] for q in plan['value']['questions']): raise ValueError('Unknown question')
            text(value['session'],160);text(value['answer']);text(value['evaluation'])
            if type(value['hints']) is not int or not 0<=value['hints']<=100: raise ValueError('Invalid hint count')
            if value['verdict'] not in ('correct','partial','incorrect','ungraded','skipped'): raise ValueError('Unknown verdict')
            from pilot_links import validate
            validate(value.get('learning_ref'))
            saved=value
        else: raise ValueError('Unknown assessment operation')
        event=dict(revision=data['revision']+1,at=timestamp(),type=kind,value=saved)
        data['events'].append(event);data['revision']+=1
        if len(json.dumps(data,ensure_ascii=False).encode('utf-8'))>1_900_000: raise ValueError('Assessment journal limit')
        atomic_json(root/'assessment.json',data)
        return event
