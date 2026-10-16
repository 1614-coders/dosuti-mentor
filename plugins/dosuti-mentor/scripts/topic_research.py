"""Bounded, local research ledger. Network/search execution belongs to the assistant."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from atomic_files import atomic_json, atomic_text
from profile_storage import checked
from storage_lock import file_lock

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
        fields(value,'action source_id verdict version_fit observed_version notes')
        if not isinstance(value['source_id'],str) or value['source_id'] not in state['sources']:raise ValueError('Unknown research source')
        if state['sources'][value['source_id']]['status']!='captured':raise ValueError('Cannot review an unavailable source')
        if value['verdict'] not in ('accepted','excluded') or value['version_fit'] not in ('compatible','conflict','unknown'):raise ValueError('Invalid research review')
        text(value['observed_version'],'observed version',True);text(value['notes'],'review notes')
        if value['verdict']=='accepted' and (value['version_fit']!='compatible' or value['observed_version'] is None):
            raise ValueError('Accepted source requires a manually checked compatible version')
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


def snapshot(folder,value):
    p=folder/value['snapshot'];checked(folder/'snapshots',True);checked(p)
    if not p.is_file() or p.stat().st_size>800000:raise ValueError('Research snapshot missing or too large')
    raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=value['sha256']:raise ValueError('Research snapshot changed')
    content=raw.decode('utf-8')
    if len(content)!=value['characters']:raise ValueError('Research snapshot character count changed')
    return content



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
            if value['action']=='review':snapshot(folder,state['sources'][value['source_id']])
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
                # Enforce budgets before writing any snapshot.
                apply(deepcopy(state),value,True)
                for p in (parent,folder,folder/'snapshots'):p.mkdir(exist_ok=True);checked(p,True)
                target=folder/value['snapshot'];checked(target)
                if target.exists():snapshot(folder,value)
                else:atomic_text(target,content);target.chmod(target.stat().st_mode & ~0o222)
            event=dict(revision=expected+1,at=course.now(),value=value);event['sha256']=digest(event)
            data=dict(schema=SCHEMA,revision=expected+1,events=data['events']+[event]);replay(data)
            if len(json.dumps(data,ensure_ascii=False,indent=2).encode())>900000:raise ValueError('Research history limit reached; preserve/export')
            for p in (parent,folder):p.mkdir(exist_ok=True);checked(p,True)
            atomic_json(path,data)
    data,state=load();sources=[]
    for identity,source in state['sources'].items():
        row=deepcopy(source);review=state['reviews'].get(identity);row.update(review=review,eligible=False,text=None)
        if source['status']=='captured':
            try:
                content=snapshot(folder,source);row['integrity']='verified'
                row['eligible']=bool(review and review['verdict']=='accepted')
                if row['eligible']:row.update(text=content,text_file=str((folder/source['snapshot']).resolve()))
            except (OSError,ValueError) as error:row.update(integrity='stale',reason=str(error))
        sources.append(row)
    budget=state['request']['budget'] if state['request'] else None
    return dict(schema=SCHEMA,revision=data['revision'],request=state['request'],queries=list(state['queries'].values()),sources=sources,
                used=dict(queries=len(state['queries']),pages=len(sources),characters=state['characters']),budget=budget,
                verification='assistant_recorded_search_and_review_not_automatic_proof',
                limitations=['Queries are reservations, not proof a search tool was called','URL and captured text association recorded by assistant, not network-attested','No automatic search, unit creation, grading or token metering'])
