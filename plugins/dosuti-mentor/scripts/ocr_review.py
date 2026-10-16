"""Versioned manual frame transcriptions; preserve OCR and uncertainty separately."""
from pathlib import Path
from types import SimpleNamespace as NS
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock

SCHEMA='ocr_review_v1'


def validate(value):
    if not isinstance(value,dict) or set(value)!={'text','notes','uncertainties'}:raise ValueError('OCR review requires text, notes and uncertainties')
    for name,limit in [('text',20000),('notes',4000)]:
        if not isinstance(value[name],str) or not value[name].strip() or len(value[name])>limit:raise ValueError('Invalid OCR review '+name)
    items=value['uncertainties']
    if not isinstance(items,list) or len(items)>50 or any(not isinstance(x,str) or not x.strip() or len(x)>1000 for x in items):raise ValueError('Invalid OCR uncertainties')


def candidate(directory,key):
    from learning_tools import read_json
    from source_integrity import digest
    from screen_ocr import signature,observations,region,select,SCHEMA as OCR_SCHEMA
    digest(key);folder=directory/'evidence/ocr'
    for p in (directory/'evidence',folder):checked(p,True)
    path=folder/(key+'.json');checked(path);value=read_json(path)
    if not isinstance(value,dict) or set(value)!={'schema','binding','region','engine','lines','verification','sha256'}:raise ValueError('OCR candidate is unavailable or invalid')
    body={k:v for k,v in value.items() if k!='sha256'}
    if value['schema']!=OCR_SCHEMA or value['verification']!='ocr_candidate_only' or signature(body)!=value['sha256']:raise ValueError('OCR candidate checksum changed')
    if signature(dict(binding=value['binding'],region=value['region'],engine=value['engine']))!=key:raise ValueError('OCR candidate key changed')
    observations(value['lines']);region(value['region']);b=value['binding']
    if not isinstance(b,dict) or set(b)!={'lesson','video_sha256','frame_id','frame_sha256','frame_seconds'}:raise ValueError('Invalid OCR binding')
    from lecture_context import number
    number(b['frame_seconds'],'OCR frame time')
    if b['frame_seconds']<0:raise ValueError('Invalid OCR frame time')
    _,_,current=select(NS(course=directory.parent.parent,lesson=directory.name,frame_id=b['frame_id'],start=max(0,b['frame_seconds']-.001),window=.01))
    if current!=b:raise ValueError('OCR review source changed')
    return value


def load(directory,key,raw=None):
    from document_store import read
    from screen_ocr import signature
    from source_integrity import digest
    digest(key);path=directory/'evidence/ocr'/(key+'.review.json');checked(path)
    value=read(path,dict(schema=SCHEMA,revision=0,events=[]),'ocr_review')
    if not isinstance(value,dict) or set(value)!={'schema','revision','events'} or value['schema']!=SCHEMA or type(value['revision']) is not int or not 0<=value['revision']<=100 or not isinstance(value['events'],list) or len(value['events'])!=value['revision']:
        raise ValueError('Invalid OCR review history; preserve before repair')
    raw=raw if raw is not None else candidate(directory,key)
    for i,row in enumerate(value['events'],1):
        if not isinstance(row,dict) or set(row)!={'revision','at','candidate_sha256','value','sha256'}:raise ValueError('Invalid OCR review event')
        validate(row['value'])
        if type(row['revision']) is not int or row['revision']!=i or not isinstance(row['at'],str) or not row['at'] or row['candidate_sha256']!=raw['sha256'] or signature({k:v for k,v in row.items() if k!='sha256'})!=row['sha256']:
            raise ValueError('OCR review changed or belongs to another candidate')
    return path,value


def view(directory,key,raw):
    _,data=load(directory,key,raw)
    return dict(revision=data['revision'],latest=data['events'][-1] if data['events'] else None,
                verification='assistant_manual_transcription_not_independent_verification')


def save(args,directory,key):
    import course
    from screen_ocr import signature,course_root_inside_plugin
    from learning_tools import read_json
    from document_store import write
    if course_root_inside_plugin(args):raise ValueError('OCR review must stay outside plugin')
    value=read_json(args.review_file);validate(value);expected=getattr(args,'expected_revision',None)
    if type(expected) is not int or expected<0:raise ValueError('OCR review requires expected revision')
    lesson_lock=directory/'.lesson.lock';checked(lesson_lock)
    with file_lock(lesson_lock):
        checked(lesson_lock);raw=candidate(directory,key);lock=directory/'evidence/ocr'/(key+'.review.lock');checked(lock)
        with file_lock(lock):
            checked(lock);path,data=load(directory,key,raw)
            if expected!=data['revision']:raise ValueError('OCR review changed in another chat; read before writing')
            if data['events'] and data['events'][-1]['value']==value:return
            if data['revision']>=100:raise ValueError('OCR review version limit reached; preserve/export history')
            event=dict(revision=expected+1,at=course.now(),candidate_sha256=raw['sha256'],value=value)
            event['sha256']=signature(event);data['events'].append(event);data['revision']+=1
            if candidate(directory,key)['sha256']!=raw['sha256']:raise ValueError('OCR changed during review')
            write(path,data,'ocr_review',writer=atomic_json)


def dependency(directory,key):
    raw=candidate(directory,key);_,data=load(directory,key,raw)
    if not data['events']:raise ValueError('OCR review is missing')
    return data['events'][-1]['sha256']
