"""Immutable journal segments, committed by one atomic manifest replacement."""
import hashlib
import json
from pathlib import Path
from atomic_files import atomic_json
from profile_storage import checked

SCHEMA='unit_learning_v3'
LIMIT=900_000
MAX_SEGMENTS=256


def encoded(value):return json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')


def read_manifest(path):
    from learning_tools import read_json
    checked(path)
    return read_json(path)


def event_stream(path,data):
    """Validate manifest and stream one bounded segment at a time."""
    if not isinstance(data,dict):raise ValueError('Invalid learning journal')
    if data.get('schema')!=SCHEMA:
        if set(data)!={'schema','revision','events'}:raise ValueError('Invalid learning journal fields')
        return data['schema'],data['revision'],iter(data['events']) if isinstance(data['events'],list) else iter_invalid()
    if (set(data)!={'schema','revision','segments','events'} or type(data['revision']) is not int
            or not isinstance(data['segments'],list) or not 1<=len(data['segments'])<=MAX_SEGMENTS
            or not isinstance(data['events'],list)):
        raise ValueError('Invalid segmented learning manifest')
    def events():
        folder=path.parent/'unit-learning-segments';checked(folder,True)
        for entry in data['segments']:
            if not isinstance(entry,dict) or set(entry)!={'sha256','count'} or type(entry['count']) is not int or entry['count']<1:
                raise ValueError('Invalid learning segment reference')
            from source_integrity import digest
            digest(entry['sha256']);p=folder/(entry['sha256']+'.json');checked(p)
            if not p.is_file() or p.stat().st_size>LIMIT:raise ValueError('Learning segment missing or too large')
            raw=p.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise ValueError('Learning segment checksum changed')
            segment=json.loads(raw)
            if not isinstance(segment,list) or len(segment)!=entry['count']:raise ValueError('Invalid learning segment count')
            yield from segment
        yield from data['events']
    return 'unit_learning_v2',data['revision'],events()


def iter_invalid():raise ValueError('Invalid learning events')


def expand(path,data):
    schema,revision,events=event_stream(path,data)
    return dict(schema=schema,revision=revision,events=list(events))


def write(path,journal):
    """Caller holds profile lock. Unreferenced segments after failure are harmless."""
    previous=read_manifest(path)
    if (previous is None or previous.get('schema')!=SCHEMA) and len(encoded(journal))<=LIMIT:
        atomic_json(path,journal);return
    if previous and previous.get('schema')==SCHEMA:
        segments=previous['segments'][:]
        tail=previous['events']+[journal['events'][-1]]
    else:segments=[];tail=journal['events'][:]
    folder=path.parent/'unit-learning-segments'
    checked(folder,True);folder.mkdir(exist_ok=True);checked(folder,True)
    # Leave room in the small manifest for references and the active tail.
    while len(encoded(dict(schema=SCHEMA,revision=journal['revision'],segments=segments,events=tail)))>LIMIT:
        if len(segments)>=MAX_SEGMENTS:raise ValueError('Learning segment limit reached; preserve/export course')
        chunk=[];size=0
        for event in tail:
            cost=len(encoded(event))+10
            if chunk and size+cost>600_000:break
            chunk.append(event);size+=cost
        if not chunk:raise ValueError('Learning event too large')
        raw=encoded(chunk);checksum=hashlib.sha256(raw).hexdigest();target=folder/(checksum+'.json');checked(target)
        if target.exists():
            if target.read_bytes()!=raw:raise ValueError('Existing learning segment changed')
        else:atomic_json(target,chunk)
        segments.append(dict(sha256=checksum,count=len(chunk)));tail=tail[len(chunk):]
    atomic_json(path,dict(schema=SCHEMA,revision=journal['revision'],segments=segments,events=tail))


def append(path,event,expected_revision):
    """Caller validated the full stream and holds the profile lock."""
    previous=read_manifest(path)
    revision=previous['revision'] if previous is not None else 0
    if revision!=expected_revision or event['revision']!=revision+1:
        raise ValueError('Unit learning changed in another chat; read before writing')
    if previous and previous['schema']==SCHEMA:
        # write() only needs the new event for a segmented manifest.
        events=[event]
    else:events=(previous['events'] if previous else [])+[event]
    write(path,dict(schema='unit_learning_v2',revision=revision+1,events=events))
