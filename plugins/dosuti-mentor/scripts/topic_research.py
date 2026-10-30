"""Bounded, local research ledger. Network/search execution belongs to the assistant."""
from copy import deepcopy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock
from read_snapshot import cached, observed

SCHEMA='topic_research_v1'


def text(value,label,nullable=False):
    if nullable and value is None:return
    if not isinstance(value,str) or not value.strip() or len(value)>4000:raise ValueError('Invalid '+label)


def url(value):
    if not isinstance(value,str) or len(value)>4096 or any(c.isspace() for c in value):raise ValueError('Invalid source URL')
    parsed=urlsplit(value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Use an HTTPS URL without credentials or fragment')
    return value


def fields(value,keys):
    if not isinstance(value,dict) or set(value)!=set(keys.split()):raise ValueError('Invalid research event fields')


def validate_claims(review,state,stored):
    claims=review.get('claims',[])
    if not isinstance(claims,list) or len(claims)>10:raise ValueError('Review permits up to 10 claims')
    ids=set()
    for claim in claims:
        fields(claim,'id statement status start_line end_line notes evidence')
        import course
        course.slug(claim['id'])
        if claim['id'] in ids:raise ValueError('Duplicate claim ID')
        ids.add(claim['id'])
        for key in ('statement','notes'):text(claim[key],'claim '+key)
        if claim['status'] not in ('supported','contradictory','unverified','insufficient'):raise ValueError('Invalid recorded claim status')
        references=claim['evidence']
        if not isinstance(references,list) or len(references)>4:raise ValueError('Claim permits up to 4 references')
        if claim['status']=='supported' and not references:raise ValueError('Supported assessment requires a source reference')
        for item in [claim]+references:
            if not isinstance(item,dict):raise ValueError('Claim reference must be an object')
            if any(type(item.get(k)) is not int for k in ('start_line','end_line')) or not 1<=item['start_line']<=item['end_line']:
                raise ValueError('Claim/reference requires valid source lines')
        for ref in references:
            fields(ref,'source_id start_line end_line quote'+(' source_sha256' if stored else ''))
            capture=state['sources'].get(ref['source_id']) if isinstance(ref['source_id'],str) else None
            if not capture or capture['status']!='captured':raise ValueError('Claim reference source unavailable')
            text(ref['quote'],'reference quote')
            if stored and ref['source_sha256']!=capture['sha256']:raise ValueError('Claim reference hash changed')
        if claim['status']=='contradictory' and len({r['source_id'] for r in references})<2:
            raise ValueError('Recorded contradiction requires two source references; independence is not inferred')


def bind_claims(folder,state,review,stored=False,quality=None):
    """Verify locator/quote binding, never whether the statement is semantically true."""
    validate_claims(review,state,stored)
    result=deepcopy(review);cache={}
    def lines(identity):
        if identity not in cache:
            cache[identity]=snapshot(folder,state['sources'][identity]).splitlines()
            if quality is not None:
                from material_facts import extraction_context
                quality[identity]=extraction_context(cache[identity],1,len(cache[identity]))
                quality[identity]['source_url']=state['sources'][identity]['url']
        return cache[identity]
    for claim in result.get('claims',[]):
        if claim['end_line']>len(lines(review['source_id'])):raise ValueError('Claim anchor exceeds source')
        for ref in claim['evidence']:
            content=lines(ref['source_id'])
            if ref['end_line']>len(content) or ref['quote']!='\n'.join(content[ref['start_line']-1:ref['end_line']]):
                raise ValueError('Claim quote does not match the complete selected source lines')
            ref['source_sha256']=state['sources'][ref['source_id']]['sha256']
    return result


def apply(state,value,stored=False):
    import course
    action=value.get('action') if isinstance(value,dict) else None
    if action=='request':
        fields(value,'action topic goal level technology_version gaps materials budget')
        if state['request'] is not None:raise ValueError('Request is immutable; create a new research ID')
        for key in ('topic','goal','level','technology_version'):text(value[key],key)
        from study_units import strings
        strings(value['gaps'],'gaps');strings(value['materials'],'materials')
        fields(value['budget'],'queries pages characters per_source')
        for key,maximum in dict(queries=20,pages=40,characters=200000,per_source=50000).items():
            if type(value['budget'][key]) is not int or not 1<=value['budget'][key]<=maximum:raise ValueError('Invalid research budget '+key)
        if value['budget']['per_source']>value['budget']['characters']:raise ValueError('per_source exceeds total character budget')
        state['request']=deepcopy(value)
        return
    if state['request'] is None:raise ValueError('Create a research request first')
    budget=state['request']['budget']
    if action=='query':
        fields(value,'action id query provider');course.slug(value['id']);text(value['query'],'query');text(value['provider'],'provider')
        if value['id'] in state['queries']:raise ValueError('Duplicate query ID')
        if len(state['queries'])>=budget['queries']:raise ValueError('Query budget exhausted')
        state['queries'][value['id']]=deepcopy(value)
    elif action=='capture':
        common='action id url query_id origin status notes'
        extra=' snapshot sha256 characters' if stored and value.get('status')=='captured' else ' text_file'
        fields(value,common+extra);course.slug(value['id']);text(value['notes'],'capture notes')
        if value['url'] is not None:url(value['url'])
        elif value['origin']!='user':raise ValueError('Search capture requires a URL')
        if value['origin'] not in ('user','search') or value['status'] not in ('captured','unavailable'):raise ValueError('Invalid capture origin/status')
        if value['origin']=='search':
            if not isinstance(value['query_id'],str) or value['query_id'] not in state['queries']:raise ValueError('Search capture requires a reserved query')
        elif value['query_id'] is not None:raise ValueError('User source cannot claim a search query')
        if value['id'] in state['sources']:raise ValueError('Duplicate source ID; use a new ID for a new capture')
        if len(state['sources'])>=budget['pages']:raise ValueError('Page budget exhausted (including unavailable pages)')
        if value['status']=='unavailable':
            if value['text_file'] is not None:raise ValueError('Unavailable page cannot contain text')
        elif stored:
            from source_integrity import digest
            digest(value['sha256'])
            if value['snapshot']!='snapshots/'+value['sha256']+'.txt':raise ValueError('Invalid research snapshot path')
            count=value['characters']
            if type(count) is not int or not 1<=count<=budget['per_source']:raise ValueError('Per-source character budget exceeded')
            if state['characters']+count>budget['characters']:raise ValueError('Total character budget exceeded')
            state['characters']+=count
        else:text(value['text_file'],'text_file')
        state['sources'][value['id']]=deepcopy(value)
    elif action=='review':
        fields(value,'action source_id verdict version_fit observed_version notes'+(' claims' if 'claims' in value else ''))
        if not isinstance(value['source_id'],str) or value['source_id'] not in state['sources']:raise ValueError('Unknown research source')
        if state['sources'][value['source_id']]['status']!='captured':raise ValueError('Cannot review an unavailable source')
        if value['verdict'] not in ('accepted','excluded') or value['version_fit'] not in ('compatible','conflict','unknown'):raise ValueError('Invalid research review')
        text(value['observed_version'],'observed version',True);text(value['notes'],'review notes')
        if value['verdict']=='accepted' and (value['version_fit']!='compatible' or value['observed_version'] is None):
            raise ValueError('Accepted source requires a manually checked compatible version')
        validate_claims(value,state,stored)
        state['reviews'][value['source_id']]=deepcopy(value)
    else:raise ValueError('Unknown research action')


def replay(data):
    fields(data,'schema revision events')
    if data['schema']!=SCHEMA or type(data['revision']) is not int or not isinstance(data['events'],list) or data['revision']!=len(data['events']):raise ValueError('Invalid research journal')
    state=dict(request=None,queries={},sources={},reviews={},characters=0)
    for i,event in enumerate(data['events'],1):
        fields(event,'revision at value sha256')
        from study_units import digest
        if type(event['revision']) is not int or event['revision']!=i or event['sha256']!=digest({k:v for k,v in event.items() if k!='sha256'}):raise ValueError('Research event checksum changed')
        text(event['at'],'timestamp');apply(state,event['value'],True)
    return state


@cached(limit=40)
@observed
def _snapshot_text(path,expected,count):
    if not path.is_file() or path.stat().st_size>800000:raise ValueError('Research snapshot missing or too large')
    raw=path.read_bytes()
    if len(raw)>800000 or hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Research snapshot changed')
    content=raw.decode('utf-8')
    if len(content)!=count:raise ValueError('Research snapshot character count changed')
    return content


def snapshot(folder,value):
    p=folder/value['snapshot'];checked(folder/'snapshots',True);checked(p)
    return _snapshot_text(p,value['sha256'],value['characters'])


def load_research(root,name):
    """Read one journal without creating storage; reusable for unit evidence."""
    import course
    from learning_tools import read_json
    folder=Path(root)/'research'/course.slug(name)
    for directory in (folder.parent,folder):checked(directory,True)
    path=folder/'research.json';checked(path)
    data=read_json(path,dict(schema=SCHEMA,revision=0,events=[]))
    return folder,data,replay(data)


def research(args):
    import course
    from learning_tools import read_json
    from study_units import digest
    root=course.root(args);course.read(root/'course.json');name=course.slug(args.research_id)
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Research must stay outside plugin')
    parent=root/'research';folder=parent/name;path=folder/'research.json'
    for p in (parent,folder):checked(p,True)
    def load():
        _,data,state=load_research(root,name);return data,state
    incoming=getattr(args,'event_file',None)
    if incoming:
        value=read_json(incoming);expected=getattr(args,'expected_revision',None)
        if type(expected) is not int or expected<0:raise ValueError('Research write requires --expected-revision')
        lock=root/'.topic-research.lock';checked(lock)
        unit_lock=root/'.study-units.lock';checked(unit_lock)
        with file_lock(unit_lock),file_lock(lock):
            checked(unit_lock);checked(lock);data,state=load()
            if expected!=data['revision']:raise ValueError('Research changed; read before writing')
            apply(deepcopy(state),value)
            pending=None
            if value['action']=='review':
                snapshot(folder,state['sources'][value['source_id']])
                if 'claims' in value:value=bind_claims(folder,state,value)
            if value['action']=='capture' and value['status']=='captured':
                source=Path(value['text_file']).expanduser();checked(source)
                if not source.is_file() or source.stat().st_size>800000:raise ValueError('Provide a UTF-8 excerpt within 800000 bytes')
                raw=source.read_bytes()
                if len(raw)>800000 or b'\0' in raw:raise ValueError('Invalid source text')
                content=raw.decode('utf-8')
                if not content.strip():raise ValueError('Source text is empty')
                checksum=hashlib.sha256(raw).hexdigest()
                value={k:v for k,v in value.items() if k!='text_file'}
                value.update(snapshot='snapshots/'+checksum+'.txt',sha256=checksum,characters=len(content))
                apply(deepcopy(state),value,True)
                pending=(folder/value['snapshot'],raw)
            event=dict(revision=expected+1,at=course.now(),value=value);event['sha256']=digest(event)
            data=dict(schema=SCHEMA,revision=expected+1,events=data['events']+[event]);replay(data)
            if len(json.dumps(data,ensure_ascii=False,indent=2).encode())>900000:raise ValueError('Research history limit reached; preserve/export')
            # All validation, including the complete journal size, precedes publication.
            staged=None;owned=None
            try:
                for p in (parent,folder):p.mkdir(exist_ok=True);checked(p,True)
                if pending:
                    target,raw=pending
                    directory=folder/'snapshots';directory.mkdir(exist_ok=True);checked(directory,True)
                    checked(target)
                    if target.exists():snapshot(folder,value)
                    else:
                        # Keep the inode identity across the atomic rename, even if it raises.
                        fd,temporary=tempfile.mkstemp(prefix='.capture-',suffix='.tmp',dir=directory)
                        staged=Path(temporary)
                        with os.fdopen(fd,'wb') as stream:
                            info=os.fstat(stream.fileno());owned=(info.st_dev,info.st_ino)
                            stream.write(raw);stream.flush();os.fsync(stream.fileno())
                        staged.rename(target)
                        target.chmod(target.stat().st_mode & ~0o222)
                atomic_json(path,data)
            except BaseException as error:
                if owned is not None:
                    try:
                        # A writer can raise after replace: durable references win over the exception.
                        _,saved,_=load_research(root,name)
                        registered=any(e['value'].get('snapshot')==value['snapshot'] for e in saved['events'])
                        checked(target.parent,True)
                        if not registered and checked(target)==owned:
                            target.chmod(target.stat().st_mode | 0o200);target.unlink()
                    except Exception as failure:
                        error.add_note('Research snapshot preserved; rollback could not be verified: '+str(failure))
                raise
            finally:
                if staged is not None and checked(staged)==owned:staged.unlink()
    data,state=load();sources=[]
    for identity,source in state['sources'].items():
        row=deepcopy(source);review=state['reviews'].get(identity);row.update(review=review,eligible=False,text=None);quality={};citation_quality={};line_count=0
        if source['status']=='captured':
            try:
                content=snapshot(folder,source);row['integrity']='verified'
                from material_facts import extraction_context
                lines=content.splitlines();line_count=len(lines);quality=extraction_context(lines,1,line_count)
                if review and 'claims' in review:bind_claims(folder,state,review,stored=True,quality=citation_quality)
                row['eligible']=bool(review and review['verdict']=='accepted')
                if row['eligible']:row.update(text=content,text_file=str((folder/source['snapshot']).resolve()))
            except (OSError,ValueError) as error:row.update(integrity='stale',reason=str(error))
        from material_facts import source_assurance
        row['fact_assurance']=source_assurance(dict(kind='research',review=review,capture=source,
            research_id=name,source_id=identity,start_line=1,end_line=line_count,
            extraction_quality=quality,citation_quality=citation_quality,text=row.get('text') or ''))
        sources.append(row)
    budget=state['request']['budget'] if state['request'] else None
    return dict(schema=SCHEMA,revision=data['revision'],request=state['request'],queries=list(state['queries'].values()),sources=sources,
                used=dict(queries=len(state['queries']),pages=len(sources),characters=state['characters']),budget=budget,
                verification='assistant_recorded_search_and_review_not_automatic_proof',
                eligibility_scope='integrity_and_recorded_review_not_factual_truth',
                limitations=['Queries are reservations, not proof a search tool was called','URL and captured text association recorded by assistant, not network-attested','No automatic search, unit creation, grading or token metering'])
