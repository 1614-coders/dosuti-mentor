"""Bounded content-addressed documents; legacy JSON is read without migration.

The caller holds its existing write lock. Parts are published before the atomic
manifest. Default reads decode full logical documents; an opt-in projector consumes verified
parts without assembling the full text. This is not an index.
"""
import hashlib
import json
import codecs
from pathlib import Path
from atomic_files import atomic_json
from profile_storage import checked

SCHEMA='document_parts_v1'
INLINE_BYTES=900_000
MAX_BYTES=64*1024*1024
PART_BYTES=256*1024


def encoded(value):return json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')


def folder(path):return path.with_name(path.stem+'-objects')


def read(path,default,kind,project=None):
    from learning_tools import read_json
    checked(path);value=read_json(path,default)
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA:
        # Inline documents are already bounded by read_json. Projection mainly
        # saves memory for large segmented registries; no on-disk migration.
        return project(iter([encoded(value).decode('utf-8')])) if project else value
    if (set(value)!={'schema','kind','bytes','sha256','parts'} or value['kind']!=kind
            or type(value['bytes']) is not int or not 0<value['bytes']<=MAX_BYTES
            or not isinstance(value['parts'],list) or not 1<=len(value['parts'])<=256):
        raise ValueError('Invalid document manifest')
    from source_integrity import digest
    digest(value['sha256']);directory=folder(path);checked(directory,True)
    # Validate the declared total before decoding any bounded parts.
    total=0
    for item in value['parts']:
        if not isinstance(item,dict) or set(item)!={'sha256','bytes'} or type(item['bytes']) is not int or not 0<item['bytes']<=PART_BYTES:
            raise ValueError('Invalid document part')
        digest(item['sha256']);total+=item['bytes']
    if total!=value['bytes']:raise ValueError('Document size changed')
    decoder=codecs.getincrementaldecoder('utf-8')();chunks=[];checksum=hashlib.sha256()
    for item in value['parts']:
        p=directory/(item['sha256']+'.part');checked(p)
        if not p.is_file() or p.stat().st_size!=item['bytes']:raise ValueError('Document part missing or size changed')
        with p.open('rb') as stream:raw=stream.read(PART_BYTES+1)
        if len(raw)!=item['bytes'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Document part checksum changed')
        checksum.update(raw);chunks.append(raw if project else decoder.decode(raw))
    if checksum.hexdigest()!=value['sha256']:raise ValueError('Document checksum changed')
    if project:
        # Keep verified bytes only. Consume/release them while projecting; no
        # full logical text or historical object graph is assembled.
        from collections import deque
        pending=deque(chunks);del chunks,raw
        def texts():
            while pending:yield decoder.decode(pending.popleft())
            yield decoder.decode(b'',final=True)
        result=project(texts())
        if not isinstance(result,dict) or result.get('schema')==SCHEMA:raise ValueError('Invalid logical document')
        return result
    chunks.append(decoder.decode(b'',final=True))
    del raw,decoder
    text=''.join(chunks)
    # json.loads constructs a second object graph; release chunks before that peak.
    del chunks
    result=json.loads(text)
    if not isinstance(result,dict) or result.get('schema')==SCHEMA:raise ValueError('Invalid logical document')
    return result


def check_size(value,limit=MAX_BYTES):
    raw=encoded(value)
    if len(raw)>limit:raise ValueError('Document storage limit reached; preserve/export history')
    return raw


def write(path,value,kind,writer=atomic_json,limit=MAX_BYTES):
    from atomic_files import atomic_bytes
    raw=check_size(value,limit);checked(path)
    if len(raw)<=INLINE_BYTES:writer(path,value);return
    directory=folder(path);checked(directory,True);directory.mkdir(exist_ok=True);checked(directory,True)
    parts=[];created=[]
    manifest=dict(schema=SCHEMA,kind=kind,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),parts=parts)
    try:
        for start in range(0,len(raw),PART_BYTES):
            chunk=raw[start:start+PART_BYTES];sha=hashlib.sha256(chunk).hexdigest();p=directory/(sha+'.part');checked(p)
            if p.exists():
                if p.read_bytes()!=chunk:raise ValueError('Existing document part changed')
            else:atomic_bytes(p,chunk);created.append(p)
            parts.append(dict(sha256=sha,bytes=len(chunk)))
        writer(path,manifest)
    except BaseException:
        # A writer can fail after replace: never delete a possibly committed part.
        from learning_tools import read_json
        try:committed=read_json(path)==manifest
        except (ValueError,OSError):committed=True
        if not committed:
            for p in created:p.unlink(missing_ok=True)
        raise
