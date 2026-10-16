"""Dated, bounded career samples; no automatic learner scoring or plan changes."""
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
import json
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock

SCHEMA='career_evidence_v1'
FIELDS={'id','title','scope','as_of','selection','limitations','postings','requirements'}


def fields(value,names):
    if not isinstance(value,dict) or set(value)!=set(names.split()):raise ValueError('Invalid career evidence fields')


def day(value,nullable=False):
    if nullable and value is None:return None
    if not isinstance(value,str):raise ValueError('Expected ISO date')
    try:result=date.fromisoformat(value)
    except ValueError:raise ValueError('Expected ISO date YYYY-MM-DD')
    if result.isoformat()!=value:raise ValueError('Expected ISO date YYYY-MM-DD')
    return result


def validate(card,stored=False):
    import course,research_source
    from topic_research import text
    from study_units import strings,digest
    extra={'content_sha256','saved_at'} if stored else set()
    if not isinstance(card,dict) or set(card)!=FIELDS|extra:raise ValueError('Invalid career sample fields')
    course.slug(card['id']);text(card['title'],'sample title');text(card['selection'],'selection method')
    fields(card['scope'],'role region level goal basis')
    for key in ('role','region','level','goal'):text(card['scope'][key],key)
    if card['scope']['basis'] not in ('user_agreed','provisional','qa_assumption'):raise ValueError('Unknown scope basis')
    as_of=day(card['as_of']);strings(card['limitations'],'limitations',1)
    if not isinstance(card['postings'],list) or not 1<=len(card['postings'])<=40:raise ValueError('Use 1..40 postings')
    identities=set()
    for row in card['postings']:
        fields(row,'id employer employer_job_id role region level kind status inclusion inclusion_reason observed_on published_on source')
        course.slug(row['id'])
        if row['id'] in identities:raise ValueError('Duplicate posting ID')
        identities.add(row['id'])
        for key in ('employer','role','region','level','inclusion_reason'):text(row[key],key)
        text(row['employer_job_id'],'employer job ID',True)
        if row['kind'] not in ('vacancy','program') or row['status'] not in ('active','archived','unknown') or row['inclusion'] not in ('included','excluded'):raise ValueError('Invalid posting classification')
        observed=day(row['observed_on']);published=day(row['published_on'],True)
        if observed>as_of or published and published>observed:raise ValueError('Invalid posting observation/publication order')
        source=row['source']
        if not isinstance(source,dict) or source.get('kind')!='research' or source.get('role')!='external':raise ValueError('Career posting requires external research source')
        text(source.get('notes'),'source notes');research_source.validate(source,stored)
    if not isinstance(card['requirements'],list) or len(card['requirements'])>60:raise ValueError('Use at most60 requirements')
    seen=set();postings={p['id']:p for p in card['postings']}
    for requirement in card['requirements']:
        fields(requirement,'id title interpretation evidence');course.slug(requirement['id'])
        if requirement['id'] in seen:raise ValueError('Duplicate requirement ID')
        seen.add(requirement['id']);text(requirement['title'],'requirement title');text(requirement['interpretation'],'assistant interpretation')
        if not isinstance(requirement['evidence'],list) or not 1<=len(requirement['evidence'])<=40:raise ValueError('Requirement needs1..40 evidence links')
        pairs=set()
        for link in requirement['evidence']:
            fields(link,'posting classification start_line end_line explanation')
            if not isinstance(link['posting'],str) or link['posting'] not in postings:raise ValueError('Unknown posting evidence')
            if link['classification'] not in ('required','preferred','mentioned','unclear'):raise ValueError('Invalid requirement classification')
            source=postings[link['posting']]['source']
            if any(type(link[k]) is not int for k in ('start_line','end_line')) or not source['start_line']<=link['start_line']<=link['end_line']<=source['end_line']:raise ValueError('Requirement lines must be within posting excerpt')
            text(link['explanation'],'evidence interpretation')
            key=(link['posting'],link['start_line'],link['end_line'])
            if key in pairs:raise ValueError('Duplicate requirement evidence')
            pairs.add(key)
    if stored:
        from source_integrity import digest as sha
        sha(card['content_sha256']);text(card['saved_at'],'saved at')
        if digest({k:card[k] for k in FIELDS})!=card['content_sha256']:raise ValueError('Career sample checksum changed')


def canonical(url):
    from topic_research import url as valid_url
    valid_url(url);parts=urlsplit(url)
    query=[(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('gclid','fbclid')]
    return urlunsplit((parts.scheme,parts.netloc.lower(),parts.path.rstrip('/') or '/',urlencode(sorted(query)),''))


def load(root,name):
    from learning_tools import read_json
    import course
    folder=root/'career';checked(folder,True);path=folder/(course.slug(name)+'.json');checked(path)
    data=read_json(path,dict(schema=SCHEMA,revision=0,records=[]))
    fields(data,'schema revision records')
    if data['schema']!=SCHEMA or type(data['revision']) is not int or data['revision']<0 or not isinstance(data['records'],list) or len(data['records'])!=data['revision']:raise ValueError('Invalid career sample history')
    for record in data['records']:
        validate(record,True)
        if record['id']!=name:raise ValueError('Career sample identity changed')
    return path,data


def evidence(root,card,writing=False):
    from research_source import evidence as source_evidence
    rows=[];draft=deepcopy(card);all_valid=True
    for posting in draft['postings']:
        row=deepcopy(posting)
        try:
            source,context=source_evidence(root,posting['source'],writing)
            if not context['capture']['url']:raise ValueError('Posting requires a recorded HTTPS URL')
            posting['source']=source
            row.update(source=source,valid=True,url=context['capture']['url'],canonical_url=canonical(context['capture']['url']),text=context['text'])
        except (ValueError,OSError,KeyError,TypeError) as error:
            if writing:raise ValueError('Posting '+posting['id']+': '+str(error)) from error
            all_valid=False;row.update(valid=False,reason=str(error),text=None,url=None,canonical_url=None)
        rows.append(row)
    # Connected groups prevent mirrors/transitive duplicates from inflating counts.
    groups=[]
    for row in rows:
        keys=set()
        if row['valid']:
            keys.add(('url',row['canonical_url']))
            keys.add(('content',row['employer'].strip().casefold(),row['source']['sha256']))
            if row['employer_job_id']:keys.add(('job',row['employer'].strip().casefold(),row['employer_job_id'].strip().casefold()))
        matches=[g for g in groups if keys & g['keys']]
        group=dict(ids=[row['id']],keys=keys)
        for old in matches:group['ids']+=old['ids'];group['keys']|=old['keys'];groups.remove(old)
        groups.append(group)
    by_id={r['id']:r for r in rows};eligible={};dedup=[]
    for group in groups:
        members=[r for r in rows if r['id'] in group['ids']]
        admitted=[r for r in members if r['inclusion']=='included']
        candidates=[r for r in admitted if r['kind']=='vacancy']
        representative=(candidates or members)[0]['id']
        conflict=len({(r['status'],r['kind']) for r in admitted})>1
        current=bool(candidates) and not conflict and all(r['valid'] for r in admitted) and candidates[0]['status']=='active'
        for row in members:eligible[row['id']]=representative if current and row['inclusion']=='included' and row['kind']=='vacancy' else None
        if len(members)>1:dedup.append(dict(postings=[r['id'] for r in members],representative=representative,status_conflict=conflict))
    requirements=[]
    for requirement in card['requirements']:
        links=[];support={key:set() for key in ('required','preferred','mentioned','unclear')}
        for link in requirement['evidence']:
            row=by_id[link['posting']];fragment=None
            if row['valid']:
                start=link['start_line']-row['source']['start_line'];end=link['end_line']-row['source']['start_line']+1
                fragment='\n'.join(row['text'].splitlines()[start:end])
                if eligible[row['id']]:support[link['classification']].add(eligible[row['id']])
            links.append(dict(link,valid=row['valid'],url=row['url'],observed_on=row['observed_on'],status=row['status'],fragment=fragment,current_sample=eligible[row['id']] is not None))
        conflicts=[identity for identity in {v for values in support.values() for v in values} if sum(identity in values for values in support.values())>1]
        requirements.append(dict(requirement,evidence=links,current_counts={k:len(v) for k,v in support.items()} if all_valid else None,classification_conflicts=sorted(conflicts)))
    return draft,dict(valid=all_valid,postings=rows,duplicates=dedup,requirements=requirements,current_vacancy_count=len({v for v in eligible.values() if v}) if all_valid else None,
                      verification='assistant_recorded_classification_not_market_statistics',limitations=['Counts describe this selected sample only; not the labour market or learner readiness','Status, geography and requirement meaning are manually reviewed','Archived, unknown, programs and excluded entries do not establish current vacancy demand'])


FRESHNESS_DAYS=30


def freshness(card,today):
    """Age policy only; never a claim that the employer still accepts applications."""
    from datetime import timedelta
    dates=[day(p['observed_on']) for p in card['postings']
           if p['inclusion']=='included' and p['kind']=='vacancy' and p['status']=='active']
    if not dates:return dict(status='not_applicable',needs_review=False,live_verified=False,review_after_days=FRESHNESS_DAYS)
    oldest=min(dates);newest=max(dates);now=day(today)
    future=newest>now;due=oldest+timedelta(days=FRESHNESS_DAYS) if oldest.year<9999 else oldest
    review=future or now>=due
    return dict(status='future_observation' if future else ('recheck_due' if review else 'within_review_window'),
                needs_review=review,oldest_observation=oldest.isoformat(),next_review_on=due.isoformat(),
                review_after_days=FRESHNESS_DAYS,live_verified=False,
                note='Age of manual observations only; counts are as observed, not live availability')


def career(args):
    import course
    from learning_tools import read_json
    from study_units import digest
    root=course.root(args);course.read(root/'course.json');name=course.slug(args.sample_id)
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Career evidence must stay outside plugin')
    incoming=getattr(args,'sample_file',None)
    if incoming:
        card=read_json(incoming);validate(card)
        if card['id']!=name:raise ValueError('Sample ID mismatch')
        if day(card['as_of'])>day(course.now()[:10]):raise ValueError('Cannot claim future observation')
        expected=getattr(args,'expected_revision',None)
        if type(expected) is not int or expected<0:raise ValueError('Career write requires expected revision')
        lock=root/'.study-units.lock';checked(lock)
        with file_lock(lock):
            checked(lock);path,data=load(root,name)
            if data['revision']!=expected:raise ValueError('Career sample changed; read before writing')
            draft,_=evidence(root,card,True);draft.update(content_sha256=digest({k:draft[k] for k in FIELDS}),saved_at=course.now())
            if not data['records'] or data['records'][-1]['content_sha256']!=draft['content_sha256']:
                data['records'].append(draft);data['revision']+=1
                if len(json.dumps(data,ensure_ascii=False,indent=2).encode())>900000:raise ValueError('Career sample history limit reached; preserve/export')
                path.parent.mkdir(exist_ok=True);checked(path.parent,True);atomic_json(path,data)
    _,data=load(root,name)
    if not data['records']:return dict(schema=SCHEMA,revision=0,sample=None)
    card=data['records'][-1];_,view=evidence(root,card)
    return dict(schema=SCHEMA,revision=data['revision'],sample={k:v for k,v in card.items() if k not in ('postings','requirements')},history=[dict(revision=i,as_of=r['as_of'],content_sha256=r['content_sha256']) for i,r in enumerate(data['records'],1)],freshness=freshness(card,course.now()[:10]),counts_basis='manual_observations_not_live_market',**view)
