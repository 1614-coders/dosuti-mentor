"""Course-level learning units with versioned evidence, independent of video lessons."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock
from read_snapshot import cached, observed

SCHEMA='study_units_v1'
MAX_UNITS=500
FIELDS={'id','title','summary','objectives','criteria','prerequisites','sources','uncertainties'}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def strings(value,name,minimum=0):
    if not isinstance(value,list) or not minimum<=len(value)<=50:raise ValueError(name+' requires '+str(minimum)+'..50 strings')
    for item in value:
        if not isinstance(item,str) or not item.strip() or len(item)>4000:raise ValueError('Invalid '+name)


def validate(value,stored=False):
    import course
    extra={'prerequisite_hashes','content_sha256','updated'} if stored else set()
    if not isinstance(value,dict) or set(value)!=FIELDS|extra:raise ValueError('Invalid study unit fields')
    course.slug(value['id'])
    for key in ('title','summary'):
        if not isinstance(value[key],str) or not value[key].strip() or len(value[key])>4000:raise ValueError('Invalid unit '+key)
    for key in ('objectives','criteria'):strings(value[key],key,1)
    strings(value['uncertainties'],'uncertainties');strings(value['prerequisites'],'prerequisites')
    for name in value['prerequisites']:course.slug(name)
    if len(set(value['prerequisites']))!=len(value['prerequisites']):raise ValueError('Duplicate prerequisites')
    if not isinstance(value['sources'],list) or not 1<=len(value['sources'])<=20:raise ValueError('Unit needs 1..20 sources')
    for source in value['sources']:
        if not isinstance(source,dict) or source.get('role') not in ('teacher','external','assistant'):
            raise ValueError('Source role must be teacher, external or assistant')
        if not isinstance(source.get('notes'),str) or not source['notes'].strip() or len(source['notes'])>4000:
            raise ValueError('Source review notes required')
        if 'visuals' in source:
            if source.get('kind') not in ('video','resource'):raise ValueError('Visuals require video or PDF resource')
            from visual_materials import validate_annotations
            validate_annotations(source['visuals'],stored)
            source={k:v for k,v in source.items() if k!='visuals'}
        if source.get('kind')=='text':
            fields={'kind','role','notes','start_line','end_line'}|({'snapshot','sha256','original_path'} if stored else {'path'})
            if set(source)!=fields:raise ValueError('Invalid text source fields')
            for key in (('snapshot','original_path') if stored else ('path',)):
                if not isinstance(source[key],str) or not source[key].strip() or len(source[key])>4096:
                    raise ValueError('Invalid text source path')
            if any(type(source[k]) is not int for k in ('start_line','end_line')) or not 1<=source['start_line']<=source['end_line']:
                raise ValueError('Text source requires 1 <= start_line <= end_line')
        elif source.get('kind')=='video':
            fields={'kind','role','notes','lesson','start','end'}|({'source_sha256','packet_hashes'} if stored else set())
            if set(source)!=fields:raise ValueError('Invalid video source fields')
            course.slug(source['lesson'])
            from lecture_context import interval
            interval(source,'Study source interval')
        elif source.get('kind')=='timeline':
            from timeline_source import validate as validate_timeline
            validate_timeline(source,stored)
        elif source.get('kind')=='research':
            from research_source import validate as validate_research
            validate_research(source,stored)
        elif source.get('kind') in ('code','resource','homework'):
            from unit_sources import validate as validate_source
            validate_source(source,stored)
        else:raise ValueError('Source kind must be text, video, code, resource, homework, research or timeline')


def semantic(record):
    value={key:deepcopy(record[key]) for key in FIELDS}
    for source in value['sources']:
        source.pop('original_path',None)
    value['prerequisite_hashes']=record['prerequisite_hashes']
    return value


def validate_record(row):
    from source_integrity import digest as sha
    validate(row,stored=True)
    if not isinstance(row['updated'],str) or not row['updated'] or not isinstance(row['prerequisite_hashes'],dict) or set(row['prerequisite_hashes'])!=set(row['prerequisites']):
        raise ValueError('Invalid unit prerequisite hashes or timestamp')
    for checksum in row['prerequisite_hashes'].values():sha(checksum)
    sha(row['content_sha256'])
    if digest(semantic(row))!=row['content_sha256']:raise ValueError('Unit annotation checksum changed')


def validate_history(event):
    if not isinstance(event,dict) or set(event)!={'unit','record','at'}:raise ValueError('Invalid unit history')
    validate_record(event['record'])
    if event['unit']!=event['record']['id'] or not isinstance(event['at'],str) or not event['at']:
        raise ValueError('Invalid historical unit identity')


@cached
def load(root,include_history=True):
    path=root/'study-units.json';checked(path)
    from document_store import read
    history_count=0
    def projection(chunks):
        nonlocal history_count
        from json_projection import visit_array_chunks
        result,history_count=visit_array_chunks(chunks,'history',validate_history)
        return result
    data=read(path,dict(schema=SCHEMA,revision=0,units={},history=[]),'study_units',
              project=None if include_history else projection)
    if (set(data)!={'schema','revision','units','history'} or data['schema']!=SCHEMA
            or type(data['revision']) is not int or data['revision']<0 or not isinstance(data['units'],dict)
            or len(data['units'])>MAX_UNITS or not isinstance(data['history'],list)
            or data['revision']!=len(data['units'])+(len(data['history']) if include_history else history_count)):
        raise ValueError('Invalid study units registry')
    for name,row in data['units'].items():
        validate_record(row)
        if name!=row['id']:raise ValueError('Invalid unit identity')
    for event in data['history']:validate_history(event)
    graph(data['units'])
    return data


def graph(units):
    seen=set();pending=set()
    def walk(name):
        if name in pending:raise ValueError('Prerequisites contain a cycle')
        if name in seen:return
        if name not in units:raise ValueError('Missing prerequisite unit: '+name)
        pending.add(name)
        for parent in units[name]['prerequisites']:walk(parent)
        pending.remove(name);seen.add(name)
    for name in units:walk(name)


def text_bytes(path):
    from source_integrity import identity
    before=identity(path)
    with path.open('rb') as stream:raw=stream.read(min(before[2]+1,8*1024*1024+1))
    if identity(path)!=before or len(raw)!=before[2]:raise ValueError('Text source changed during read')
    return raw


@cached(limit=1)
@observed
def _text_lines(path):
    raw=text_bytes(path)
    if len(raw)>8*1024*1024:raise ValueError('Text source exceeds 8 MiB')
    if b'\0' in raw or raw.startswith((b'%PDF',b'PK\x03\x04')):raise ValueError('Provide extracted UTF-8 text, not PDF/ZIP/binary')
    text=raw.decode('utf-8-sig');lines=text.splitlines()
    return hashlib.sha256(raw).hexdigest(),lines


def text_source(root,source,writing=False,pending=None):
    from source_integrity import digest as sha
    if writing:
        path=Path(source['path']).expanduser();checked(path)
        if not path.is_file() or path.stat().st_size>8*1024*1024:raise ValueError('Text source must be a file up to 8 MiB')
        raw=text_bytes(path)
        checksum=hashlib.sha256(raw).hexdigest()
    else:
        sha(source['sha256'])
        expected='study-sources/'+source['sha256']+'.txt'
        if source['snapshot']!=expected:raise ValueError('Invalid managed text snapshot path')
        checked(root/'study-sources',True);path=root/expected;checked(path)
        if not path.is_file() or path.stat().st_size>8*1024*1024:
            raise ValueError('Text source snapshot missing or changed')
        checksum,lines=_text_lines(path)
        if checksum!=source['sha256']:raise ValueError('Text source snapshot changed')
    if writing:
        if len(raw)>8*1024*1024:raise ValueError('Text source exceeds 8 MiB')
        if b'\0' in raw or raw.startswith((b'%PDF',b'PK\x03\x04')):raise ValueError('Provide extracted UTF-8 text, not PDF/ZIP/binary')
        text=raw.decode('utf-8-sig');lines=text.splitlines()
    if source['end_line']>len(lines):raise ValueError('Text source line range exceeds content')
    excerpt='\n'.join(lines[source['start_line']-1:source['end_line']])
    if len(excerpt)>100_000:raise ValueError('Use a smaller source line range (up to 100000 characters)')
    if writing:
        folder=root/'study-sources';checked(folder,True)
        target=folder/(checksum+'.txt');checked(target)
        if target.exists():
            if target.read_bytes()!=raw:raise ValueError('Existing source snapshot changed; preserve for repair')
        elif pending is not None:pending.append((target,raw))
        else:
            from atomic_files import atomic_bytes
            folder.mkdir(exist_ok=True);atomic_bytes(target,raw);target.chmod(target.stat().st_mode & ~0o222)
        value={k:v for k,v in source.items() if k!='path'}
        value.update(snapshot='study-sources/'+checksum+'.txt',sha256=checksum,original_path=str(path.resolve()))
    else:value=deepcopy(source)
    from material_facts import extraction_context
    return value,dict(kind='text',role=source['role'],notes=source['notes'],start_line=source['start_line'],
                      end_line=source['end_line'],sha256=checksum,text=excerpt,
                      extraction_quality=extraction_context(lines,source['start_line'],source['end_line']))


def video_source(root,source,writing=False):
    import course
    from lecture_context import assemble
    from learning_tools import topic_signature
    directory=root/'lessons'/source['lesson'];data=course.read(directory/'lesson.json')
    context=assemble(directory,data,source['start'],source['end']-source['start'])
    if not context['coverage']['complete']:raise ValueError('Video source interval is not fully prepared')
    checksum=data['video']['sha256'];packets=topic_signature(directory,data,source['start'],source['end'])
    if not writing and (source['source_sha256']!=checksum or source['packet_hashes']!=packets):
        raise ValueError('Video source evidence changed')
    value=dict(source,source_sha256=checksum,packet_hashes=packets)
    return value,dict(kind='video',role=source['role'],notes=source['notes'],lesson=source['lesson'],
                      start=source['start'],end=source['end'],context=context)


def _base_evidence(root,source,writing=False,pending=None):
    if source['kind']=='timeline':
        from timeline_source import evidence as timeline_evidence
        return timeline_evidence(root,source,writing)
    if source['kind']=='research':
        from research_source import evidence as research_evidence
        return research_evidence(root,source,writing)
    if source['kind']=='text':return text_source(root,source,writing,pending)
    if source['kind']=='video':return video_source(root,source,writing)
    from unit_sources import evidence as structured_evidence
    return structured_evidence(root,source,writing)


def evidence(root,source,writing=False,pending=None):
    base={k:v for k,v in source.items() if k!='visuals'}
    value,context=_base_evidence(root,base,writing,pending)
    from visual_materials import bind
    selected,context=bind(source,context,writing)
    if 'visuals' in source:value['visuals']=selected
    return value,context


from source_integrity import verification_session

@verification_session
def checked_units(root,data,names=None,include_contexts=True):
    result={}; units=data['units']
    def check(name):
        if name in result:return result[name]
        row=deepcopy(units[name]);problems=[];sources=[];assurances=[]
        from material_facts import source_assurance,summarize
        for source in row['sources']:
            try:
                context=evidence(root,source)[1]
                context['fact_assurance']=source_assurance(context);assurances.append(context['fact_assurance'])
                if include_contexts:sources.append(context)
            except (ValueError,OSError,KeyError,TypeError) as error:problems.append(str(error))
        for parent in row['prerequisites']:
            previous=check(parent)
            if previous['stale'] or row['prerequisite_hashes'][parent]!=units[parent]['content_sha256']:
                problems.append('Prerequisite changed or needs review: '+parent)
        row.update(stale=bool(problems),reasons=problems,source_contexts=sources,
                   verification='assistant_annotation_not_automatic_proof',mastery='not_assessed',
                   fact_assurance=summarize(assurances))
        from material_status import unit_state
        row['material_status']=unit_state(row)
        result[name]=row;return row
    for name in (units if names is None else names):check(name)
    return result


def unit(args):
    import course
    from learning_tools import read_json
    root=course.root(args);course.read(root/'course.json')
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Study units must stay outside the plugin')
    path=root/'study-units.json';incoming=getattr(args,'unit_file',None)
    if incoming and getattr(args,'unit_id',None):raise ValueError('Use either --unit-file or --unit-id')
    if incoming:
        value=read_json(incoming);validate(value)
        expected=getattr(args,'expected_revision',None)
        if type(expected) is not int or expected<0:raise ValueError('Unit write requires --expected-revision')
        lock=root/'.study-units.lock';checked(lock)
        with file_lock(lock):
            checked(lock);data=load(root)
            if expected!=data['revision']:raise ValueError('Study units changed; read unit before writing')
            if value['id'] not in data['units'] and len(data['units'])>=MAX_UNITS:
                raise ValueError('Study units count limit reached; preserve/export history')
            pending=[]
            draft=deepcopy(value);draft['sources']=[]
            candidate=dict(data['units']);candidate[value['id']]=value;graph(candidate)
            previous=checked_units(root,data,value['prerequisites'])
            if any(previous[name]['stale'] for name in value['prerequisites']):raise ValueError('Prerequisite needs review')
            for source in value['sources']:draft['sources'].append(evidence(root,source,writing=True,pending=pending)[0])
            draft['prerequisite_hashes']={name:data['units'][name]['content_sha256'] for name in value['prerequisites']}
            draft['content_sha256']=digest(semantic(draft));draft['updated']=course.now()
            if value['id'] in data['units'] and draft['content_sha256']==data['units'][value['id']]['content_sha256']:
                return dict(schema=SCHEMA,revision=data['revision'],unit=checked_units(root,data,[value['id']])[value['id']],unchanged=True)
            if value['id'] in data['units']:
                data['history'].append(dict(unit=value['id'],record=data['units'][value['id']],at=course.now()))
            data['units'][value['id']]=draft;data['revision']+=1
            from document_store import check_size,write
            check_size(data)  # Reject before any source publication.
            created=[]
            try:
                from atomic_files import atomic_bytes
                for target,raw in pending:
                    if target.exists():
                        checked(target)
                        if target.read_bytes()!=raw:raise ValueError('Existing source snapshot changed')
                        continue
                    target.parent.mkdir(exist_ok=True);checked(target.parent,True)
                    atomic_bytes(target,raw);created.append(target)
                    target.chmod(target.stat().st_mode & ~0o222)
                write(path,data,'study_units',writer=atomic_json)
            except BaseException:
                # Do not remove snapshots if an unusual writer failed after commit.
                try:committed=load(root)['revision']==data['revision']
                except (ValueError,OSError):committed=True
                if not committed:
                    for target in created:target.unlink(missing_ok=True)
                raise

    data=load(root)
    name=getattr(args,'unit_id',None)
    if name:
        course.slug(name)
        if name not in data['units']:raise ValueError('Unknown study unit')
    rows=checked_units(root,data,[name] if name else None)
    if name:
        selected=rows[name]
        if selected['stale']:selected['source_contexts']=[]
        return dict(schema=SCHEMA,revision=data['revision'],unit=selected)
    return dict(schema=SCHEMA,revision=data['revision'],units=[{k:v for k,v in row.items() if k!='source_contexts'} for row in rows.values()])
