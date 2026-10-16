"""Structured lesson-backed sources for study units; no inference of video/code equality."""
from copy import deepcopy
import hashlib
from pathlib import Path
from profile_storage import checked

BASE={'kind','role','notes','lesson','start_line','end_line'}


def validate(source,stored=False):
    import course
    from source_integrity import digest
    kind=source['kind']
    fields=BASE.copy()
    if kind=='code':
        fields|={'repository','version','file'}
        if 'stage' in source:
            course.slug(source['stage']);fields.add('stage')
            if stored:fields.add('stage_sha256');digest(source.get('stage_sha256'))
        if stored:
            fields|={'code_sha256','file_sha256'};digest(source.get('code_sha256'));digest(source.get('file_sha256'))
        if source.get('repository')!='default':
            from code_repositories import repository_id
            repository_id(source.get('repository'))
        if source.get('version')!='current':digest(source.get('version'))
        if not isinstance(source.get('file'),str) or not source['file'].strip() or len(source['file'])>4096:
            raise ValueError('Code source requires a file')
    elif kind=='resource':
        fields.add('resource_id');course.slug(source.get('resource_id'))
        if stored:fields|={'revision','review_sha256'};digest(source.get('review_sha256'))
    elif kind=='homework':
        if stored:fields.add('sha256');digest(source.get('sha256'))
    else:raise ValueError('Unknown structured unit source')
    if set(source)!=fields:raise ValueError('Invalid '+kind+' source fields')
    course.slug(source['lesson'])
    if any(type(source[k]) is not int for k in ('start_line','end_line')) or not 1<=source['start_line']<=source['end_line']:
        raise ValueError('Source requires 1 <= start_line <= end_line')
    if stored and kind=='resource':
        revision=source['revision']
        if not isinstance(revision,str) or not revision.startswith('sha256:'):raise ValueError('Invalid resource revision')
        digest(revision[7:])


def excerpt(path,source,expected):
    checked(path)
    if not path.is_file() or path.stat().st_size>8*1024*1024:raise ValueError('Source file missing or exceeds 8 MiB')
    with path.open('rb') as stream:raw=stream.read(8*1024*1024+1)
    if len(raw)>8*1024*1024 or hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Source file changed')
    if b'\0' in raw:raise ValueError('Source must be UTF-8 text')
    lines=raw.decode('utf-8-sig').splitlines()
    if source['end_line']>len(lines):raise ValueError('Source lines exceed content')
    text='\n'.join(lines[source['start_line']-1:source['end_line']])
    if len(text)>100_000:raise ValueError('Select at most 100000 source characters')
    return text


def evidence(root,source,writing=False):
    import course
    from study_units import digest
    kind=source['kind'];directory=root/'lessons'/source['lesson'];data=course.read(directory/'lesson.json')
    value=deepcopy(source)
    context={k:source[k] for k in BASE}
    if kind=='code':
        from code_repositories import current,resolve
        from source_integrity import verify_snapshot
        code=resolve(data,source['repository']) if source['version']=='current' else None
        if code is None:
            repo=current(data).get(source['repository'],{})
            code=next((item for item in [repo]+repo.get('history',[]) if item.get('snapshot') and item.get('content_sha256')==source['version']),None)
            if code is None:raise ValueError('Pinned repository snapshot unavailable')
            if verify_snapshot(code)!='snapshot_hashes_match':raise ValueError('Pinned snapshot is not verifiable')
        entry=code['inventory'].get(source['file'])
        if entry is None:raise ValueError('Code source file not found in selected repository')
        # Inventory is created by the snapshot importer; require a bounded relative file too.
        relative=Path(source['file'])
        if relative.is_absolute() or '..' in relative.parts or '\\' in source['file']:raise ValueError('Unsafe code source file')
        file_hash=entry['sha256']
        context.update(repository=source['repository'],version=source['version'],code_sha256=code['content_sha256'],
                       historical=code is not current(data).get(source['repository']),
                       file=source['file'],text=excerpt(Path(code['snapshot'])/relative,source,file_hash),
                       lecture_match='unverified',usable_as_screen_match=False)
        value.update(code_sha256=code['content_sha256'],file_sha256=file_hash)
        if 'stage' in source:
            from code_stages import checked as stages
            rows=[row for row in stages(directory,data) if row.get('id')==source['stage']]
            if len(rows)!=1 or rows[0]['stale']:raise ValueError('Code stage missing or stale')
            stage=rows[0]
            if stage['code_sha256']!=code['content_sha256'] or stage['file']!=source['file']:
                raise ValueError('Code stage references a different snapshot or file')
            stored=data['code_stages'][source['stage']]
            signature=digest(dict(annotation={k:v for k,v in stored.items() if k!='updated'},overlaps=sorted(stage['overlapping_stage_ids'])))
            value['stage_sha256']=signature
            context.update(stage={k:stage[k] for k in ('id','start','end','screen_file','match','notes','frames','overlapping_stage_ids')},
                           lecture_match=stage['match'],usable_as_screen_match=stage['usable_as_screen_match'])
    elif kind=='resource':
        from material_sources import describe,resource_revision
        matches=[item for item in data.get('resources',{}).get('items',[]) if item.get('id')==source['resource_id']]
        if len(matches)!=1:raise ValueError('Resource source missing or ambiguous')
        item=describe(dict(items=matches))['items'][0]
        if item.get('status')!='reviewed' or not item.get('review_valid'):raise ValueError('Resource requires extraction and review')
        signature=digest({k:v for k,v in item['review'].items() if k!='updated'})
        value.update(revision=resource_revision(item),review_sha256=signature)
        context.update(resource_id=source['resource_id'],revision=value['revision'],review=item['review'],
                       origin=item['source'],text=excerpt(Path(item['extracted_file']),source,item['text_sha256']))
    else:
        homework=data.get('homework',{})
        if course.homework_integrity(homework)!='verified':raise ValueError('Homework missing or changed')
        value['sha256']=homework['sha256']
        context.update(sha256=homework['sha256'],text=excerpt(Path(homework['file']),source,homework['sha256']))
    if not writing:
        generated={'code':('code_sha256','file_sha256','stage_sha256') if 'stage' in source else ('code_sha256','file_sha256'),
                   'resource':('revision','review_sha256'),'homework':('sha256',)}[kind]
        if any(value[key]!=source[key] for key in generated):raise ValueError(kind+' source revision changed')
    return value,context
