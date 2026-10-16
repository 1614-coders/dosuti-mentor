"""Bounded content-addressed documents; legacy JSON is read without migration.

The caller holds its existing write lock. Parts are published before the atomic
manifest. Full logical documents are still decoded in memory; this is not an index.
"""
import hashlib
import json
from pathlib import Path
from atomic_files import atomic_json
from profile_storage import checked

SCHEMA='document_parts_v1'
INLINE_BYTES=900_000
MAX_BYTES=64*1024*1024
PART_BYTES=256*1024


def encoded(value):return json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')


def folder(path):return path.with_name(path.stem+'-objects')


def read(path,default,kind):
    from learning_tools import read_json
    checked(path);value=read_json(path,default)
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA:return value
    if (set(value)!={'schema','kind','bytes','sha256','parts'} or value['kind']!=kind
            or type(value['bytes']) is not int or not 0<value['bytes']<=MAX_BYTES
            or not isinstance(value['parts'],list) or not 1<=len(value['parts'])<=256):
        raise ValueError('Invalid document manifest')
    from source_integrity import digest
    digest(value['sha256']);directory=folder(path);checked(directory,True)
    data=bytearray()
    for item in value['parts']:
        if not isinstance(item,dict) or set(item)!={'sha256','bytes'} or type(item['bytes']) is not int or not 0<item['bytes']<=PART_BYTES:
            raise ValueError('Invalid document part')
        digest(item['sha256']);p=directory/(item['sha256']+'.part');checked(p)
        if not p.is_file() or p.stat().st_size!=item['bytes']:raise ValueError('Document part missing or size changed')
        with p.open('rb') as stream:raw=stream.read(PART_BYTES+1)
        if len(raw)!=item['bytes'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Document part checksum changed')
        data.extend(raw)
        if len(data)>value['bytes']:raise ValueError('Document size changed')
    if len(data)!=value['bytes'] or hashlib.sha256(data).hexdigest()!=value['sha256']:raise ValueError('Document checksum changed')
    result=json.loads(data.decode('utf-8'))
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
