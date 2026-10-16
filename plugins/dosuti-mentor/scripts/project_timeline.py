"""Explicit project series across lessons; observed content changes are not video history."""
from copy import deepcopy
import json
import hashlib
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock

SCHEMA='project_timelines_v1'
FIELDS={'id','title','tracks','uncertainties'}


def validate(value,stored=False):
    import course
    from study_units import strings,digest
    from source_integrity import digest as sha
    if not isinstance(value,dict) or set(value)!=FIELDS|({'content_sha256','updated'} if stored else set()):raise ValueError('Invalid project timeline fields')
    course.slug(value['id'])
    if not isinstance(value['title'],str) or not value['title'].strip() or len(value['title'])>4000:raise ValueError('Invalid timeline title')
    strings(value['uncertainties'],'uncertainties')
    if not isinstance(value['tracks'],list) or not 1<=len(value['tracks'])<=10:raise ValueError('Timeline requires 1..10 tracks')
    tracks=set();total=0
    for track in value['tracks']:
        if not isinstance(track,dict) or set(track)!={'id','title','steps'}:raise ValueError('Invalid project track')
        course.slug(track['id'])
        if track['id'] in tracks:raise ValueError('Duplicate track ID')
        tracks.add(track['id'])
        if not isinstance(track['title'],str) or not track['title'].strip() or len(track['title'])>4000:raise ValueError('Invalid track title')
        if not isinstance(track['steps'],list) or not 2<=len(track['steps'])<=50:raise ValueError('Track requires 2..50 explicit steps')
        steps=set();total+=len(track['steps'])
        for step in track['steps']:
            fields={'id','lesson','repository','version','gap_before','notes'}|({'code_sha256'} if stored else set())
            if not isinstance(step,dict) or set(step)!=fields:raise ValueError('Invalid timeline step fields')
            course.slug(step['id']);course.slug(step['lesson'])
            if step['id'] in steps:raise ValueError('Duplicate step ID within track')
            steps.add(step['id'])
            if step['repository']!='default':
                from code_repositories import repository_id
                repository_id(step['repository'])
            if step['version']!='current':sha(step['version'])
            if type(step['gap_before']) is not bool:raise ValueError('gap_before must be boolean')
            if not isinstance(step['notes'],str) or not step['notes'].strip() or len(step['notes'])>4000:raise ValueError('Step needs provenance/uncertainty notes')
            if stored:sha(step['code_sha256'])
    if total>100:raise ValueError('Timeline supports at most 100 steps')
    if stored:
        sha(value['content_sha256'])
        if not isinstance(value['updated'],str) or not value['updated'] or digest({k:value[k] for k in FIELDS})!=value['content_sha256']:
            raise ValueError('Timeline annotation checksum changed')


def load(root):
    from learning_tools import read_json
    path=root/'project-timelines.json';checked(path)
    data=read_json(path,dict(schema=SCHEMA,revision=0,timelines={},history=[]))
    if (not isinstance(data,dict) or set(data)!={'schema','revision','timelines','history'} or data['schema']!=SCHEMA
        or type(data['revision']) is not int or not isinstance(data['timelines'],dict) or not isinstance(data['history'],list)
        or len(data['timelines'])>100 or data['revision']!=len(data['timelines'])+len(data['history'])):raise ValueError('Invalid project timelines registry')
    for name,row in data['timelines'].items():
        validate(row,True)
        if name!=row['id']:raise ValueError('Timeline identity mismatch')
    for event in data['history']:
        if not isinstance(event,dict) or set(event)!={'record','at'}:raise ValueError('Invalid timeline history')
        validate(event['record'],True)
        if not isinstance(event['at'],str) or not event['at']:raise ValueError('Invalid timeline history timestamp')
    return data


def selected(root,step):
    import course
    from code_repositories import current,resolve
    from source_integrity import verify_snapshot
    data=course.read(root/'lessons'/step['lesson']/'lesson.json');repo=current(data).get(step['repository'],{})
    if step['version']=='current':code=resolve(data,step['repository'])
    else:
        code=next((x for x in [repo]+repo.get('history',[]) if x.get('content_sha256')==step['version'] and x.get('snapshot')),None)
        if code is None:raise ValueError('Pinned snapshot missing in selected lesson/repository')
        if verify_snapshot(code)!='snapshot_hashes_match':raise ValueError('Unverified pinned snapshot')
    if code.get('content_sha256')!=hashlib.sha256(json.dumps(code['inventory'],sort_keys=True).encode()).hexdigest():
        raise ValueError('Snapshot inventory identity changed')
    return code,code is not repo


def changes(before,after):
    """Hash comparison only; exact-content move candidates require unique pairings."""
    old,new=before['inventory'],after['inventory'];added=set(new)-set(old);removed=set(old)-set(new)
    modified=[name for name in sorted(set(old)&set(new)) if old[name]['sha256']!=new[name]['sha256']]
    by_old={};by_new={}
    for name in sorted(removed):by_old.setdefault(old[name]['sha256'],[]).append(name)
    for name in sorted(added):by_new.setdefault(new[name]['sha256'],[]).append(name)
    moved=[];ambiguous=[]
    for sha in sorted(set(by_old)&set(by_new)):
        left,right=by_old[sha],by_new[sha]
        if len(left)==len(right)==1:
            moved.append(dict(from_file=left[0],to_file=right[0],sha256=sha,status='same_content_move_candidate'))
            removed.remove(left[0]);added.remove(right[0])
        else:ambiguous.append(dict(removed=left[:20],added=right[:20],removed_count=len(left),added_count=len(right),members_truncated=len(left)>20 or len(right)>20,sha256=sha,reason='non_unique_content_no_pairing'))
    rows=([dict(file=n,status='modified',before_sha256=old[n]['sha256'],after_sha256=new[n]['sha256']) for n in modified]
          +[dict(file=n,status='removed',before_sha256=old[n]['sha256']) for n in sorted(removed)]
          +[dict(file=n,status='added',after_sha256=new[n]['sha256']) for n in sorted(added)]+moved)
    return dict(changes=rows[:200],change_count=len(rows),truncated=len(rows)>200,
                ambiguous_moves=ambiguous[:50],ambiguous_move_count=len(ambiguous),ambiguous_moves_truncated=len(ambiguous)>50,
                verification='snapshot_content_only_not_proof_of_rename_or_cause')


def context(root,record):
    tracks=[];problems=[]
    for track in record['tracks']:
        rows=[];previous=None
        for step in track['steps']:
            row=deepcopy(step);row.update(stale=False,lecture_match='unverified',comparison=None)
            try:
                code,historical=selected(root,step)
                if step['code_sha256']!=code['content_sha256']:raise ValueError('Selected current snapshot changed')
                row.update(historical=historical,integrity='snapshot_verified',file_count=len(code['inventory']))
            except (OSError,ValueError,KeyError,TypeError) as error:
                row.update(stale=True,reason=str(error));code=None;problems.append(track['id']+'/'+step['id']+': '+str(error))
            if rows:
                if step['gap_before']:row['comparison']=dict(status='gap',reason='Explicit recording/project gap; no inferred intermediate evolution')
                elif previous is None or code is None:row['comparison']=dict(status='unavailable',reason='An endpoint snapshot is stale or missing')
                else:row['comparison']=dict(status='observed',before_step=rows[-1]['id'],**changes(previous,code))
            rows.append(row);previous=code
        tracks.append(dict(id=track['id'],title=track['title'],steps=rows))
    return dict(record=deepcopy(record),stale=bool(problems),reasons=problems,tracks=tracks,
                verification='explicit_assistant_series_annotation_not_automatic_project_identity',
                limitations=['Order and project/repository identity supplied by assistant, not inferred','Snapshot difference is not video-screen correspondence or explanation of why code changed','No intermediate state inferred across gaps','Only explicit kind=timeline unit sources depend on this series; no automatic learner grading'])


def timeline(args):
    import course
    from pathlib import Path
    from learning_tools import read_json
    from study_units import digest
    root=course.root(args);course.read(root/'course.json')
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Timeline storage must stay outside plugin')
    incoming=getattr(args,'timeline_file',None);identity=getattr(args,'timeline_id',None)
    if incoming and identity:raise ValueError('Choose timeline-file or timeline-id')
    if incoming:
        value=read_json(incoming);validate(value)
        expected=getattr(args,'expected_revision',None)
        if type(expected) is not int or expected<0:raise ValueError('Timeline write requires --expected-revision')
        lock=root/'.project-timelines.lock';checked(lock)
        unit_lock=root/'.study-units.lock';checked(unit_lock)
        with file_lock(unit_lock),file_lock(lock):
            checked(unit_lock);checked(lock);data=load(root)
            if expected!=data['revision']:raise ValueError('Timeline changed; read before writing')
            draft=deepcopy(value)
            for track in draft['tracks']:
                for step in track['steps']:step['code_sha256']=selected(root,step)[0]['content_sha256']
            draft['content_sha256']=digest(draft);draft['updated']=course.now()
            if draft['id'] in data['timelines']:data['history'].append(dict(record=data['timelines'][draft['id']],at=course.now()))
            data['timelines'][draft['id']]=draft;data['revision']+=1
            if len(data['timelines'])>100 or len(json.dumps(data,ensure_ascii=False,indent=2).encode())>900000:raise ValueError('Timeline registry limit reached; preserve/export')
            atomic_json(root/'project-timelines.json',data)
    data=load(root)
    if identity:
        course.slug(identity)
        if identity not in data['timelines']:raise ValueError('Unknown project timeline')
        return dict(schema=SCHEMA,revision=data['revision'],timeline=context(root,data['timelines'][identity]))
    return dict(schema=SCHEMA,revision=data['revision'],timelines=[dict(id=r['id'],title=r['title'],content_sha256=r['content_sha256'],track_count=len(r['tracks'])) for r in data['timelines'].values()],
                verification='listing_only_read_timeline_id_for_source_integrity')
