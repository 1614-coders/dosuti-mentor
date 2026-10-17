"""Plan/prune obsolete host OCR builds; preserve courses and permanent lock files."""
import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import stat
import sys
import time
import native_cache as nc
from profile_storage import checked
from source_integrity import file_hash,digest
from storage_lock import file_lock

SCHEMA='native_cleanup_plan_v1'
SOURCE=Path(__file__).with_name('native')/'vision.swift'


def signature(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def validate_directory(path):
    checked(path,True);info=path.stat()
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('OCR cleanup requires private user-owned directories')


def current_key():
    _,environment=nc.toolchain()
    return nc.identity_key(dict(source_sha256=file_hash(SOURCE),toolchain=environment))


def inspect(entry):
    digest(entry.name);validate_directory(entry)
    names={p.name for p in itertools.islice(entry.iterdir(),5)}
    if names!={'vision','build.json','build.lock'}:
        raise ValueError('Unknown or incomplete OCR build contents')
    for name in names:nc.regular(entry/name)
    manifest=entry/'build.json';binary=entry/'vision'
    data=nc.build_record(entry,manifest)
    if not binary.is_file() or not 0<binary.stat().st_size<=50*1024*1024 or file_hash(binary)!=data['binary_sha256']:
        raise ValueError('OCR build checksum changed')
    return dict(manifest_sha256=file_hash(manifest),binary_sha256=data['binary_sha256'],
                bytes=manifest.stat().st_size+binary.stat().st_size,
                modified_ns=max(manifest.stat().st_mtime_ns,binary.stat().st_mtime_ns))


def age_days(value):
    if type(value) is not int or not 0<=value<=3650:raise ValueError('min_age_days must be an integer between0 and3650')
    return value


def plan(min_age_days=7,root=None):
    age_days(min_age_days)
    if platform.system()!='Darwin':
        return dict(status='unsupported_platform',read_only=True,entries=[],reclaimable_bytes=0)
    root=Path(root) if root is not None else nc.host_root()
    checked(root,True)
    if not root.exists():return dict(status='absent',read_only=True,entries=[],reclaimable_bytes=0)
    validate_directory(root);active=current_key();now_ns=time.time_ns();entries=[]
    children=list(itertools.islice(root.iterdir(),129));complete=len(children)<=128
    for entry in sorted(children[:128]):
        row=dict(key=entry.name,decision='preserve')
        try:
            if entry.name==active:row['reason']='current_build'
            else:
                snapshot=inspect(entry)
                if now_ns-snapshot['modified_ns']<min_age_days*86400*10**9:row['reason']='recent_or_future_build'
                else:row.update(decision='eligible',snapshot=snapshot)
        except (ValueError,OSError) as error:row['reason']=str(error)
        entries.append(row)
    data=dict(schema=SCHEMA,status='planned',read_only=True,root=str(root.resolve()),
              min_age_days=min_age_days,current_key=active,entries=entries,scan_complete=complete,
              reclaimable_bytes=sum(r['snapshot']['bytes'] for r in entries if r['decision']=='eligible'))
    data['sha256']=signature(data)
    return data


def apply(value,root=None):
    if platform.system()!='Darwin':raise ValueError('Native OCR cleanup is supported only on macOS')
    if (not isinstance(value,dict) or set(value)!={'schema','status','read_only','root','min_age_days','current_key','entries','scan_complete','reclaimable_bytes','sha256'}
            or value['schema']!=SCHEMA or value['status']!='planned' or value['read_only'] is not True
            or not isinstance(value['entries'],list) or len(value['entries'])>128
            or signature({k:v for k,v in value.items() if k!='sha256'})!=value['sha256']):
        raise ValueError('Invalid cleanup plan; run plan again')
    age_days(value['min_age_days']);root=Path(root) if root is not None else nc.host_root()
    if value['root']!=str(root.resolve()):raise ValueError('Cleanup plan belongs to another host cache')
    validate_directory(root);active=current_key();rows=[];seen=set()
    # Validate all requested paths before any mutation, never use paths supplied by plan.
    for row in value['entries']:
        if not isinstance(row,dict) or not isinstance(row.get('key'),str):raise ValueError('Invalid cleanup entry')
        if row.get('decision')!='eligible':continue
        digest(row['key'])
        if row['key'] in seen:raise ValueError('Duplicate cleanup entry')
        seen.add(row['key'])
        snapshot=row.get('snapshot')
        if not isinstance(snapshot,dict) or set(snapshot)!={'manifest_sha256','binary_sha256','bytes','modified_ns'}:
            raise ValueError('Invalid cleanup snapshot')
        digest(snapshot['manifest_sha256']);digest(snapshot['binary_sha256'])
        if type(snapshot['bytes']) is not int or snapshot['bytes']<=0 or type(snapshot['modified_ns']) is not int:
            raise ValueError('Invalid cleanup snapshot size/time')
    for row in value['entries']:
        if row.get('decision')!='eligible':continue
        key=row['key'];entry=root/key;result=dict(key=key,status='preserved')
        try:
            if key==active:raise ValueError('Current OCR build must be preserved')
            validate_directory(root);validate_directory(entry)
            lock=entry/'build.lock';nc.regular(lock)
            if not lock.is_file():raise ValueError('Missing permanent OCR build lock')
            with file_lock(lock,timeout=0):
                nc.regular(lock);validate_directory(root);validate_directory(entry)
                marker=entry/'evicted.json';nc.regular(marker)
                if marker.exists():
                    names={p.name for p in itertools.islice(entry.iterdir(),5)}
                    if names not in ({'evicted.json','vision','build.lock'},{'evicted.json','build.lock'}):
                        raise ValueError('Unknown interrupted eviction contents')
                    data=nc.build_record(entry,marker)
                    if file_hash(marker)!=row['snapshot']['manifest_sha256'] or data['binary_sha256']!=row['snapshot']['binary_sha256']:
                        raise ValueError('Eviction changed since plan')
                    if current_key()==key:raise ValueError('Build became current; preserve it')
                    reclaimed=marker.stat().st_size
                    binary=entry/'vision';nc.regular(binary)
                    if binary.exists():reclaimed+=binary.stat().st_size
                    nc.finish_eviction(entry)
                    result.update(status='removed',reclaimed_bytes=reclaimed,resumed=True)
                    rows.append(result);continue
                snapshot=inspect(entry)
                if snapshot!=row['snapshot']:raise ValueError('Build changed since plan')
                if time.time_ns()-snapshot['modified_ns']<value['min_age_days']*86400*10**9:
                    raise ValueError('Build is too recent for this plan')
                if current_key()==key:raise ValueError('Build became current; preserve it')
                # Keep entry/build.lock stable for old and new waiting runtimes.
                # A failed unlink leaves a verifiable tombstone; new runtime resumes it.
                (entry/'build.json').replace(entry/'evicted.json')
                nc.finish_eviction(entry)
                result.update(status='removed',reclaimed_bytes=snapshot['bytes'])
        except (ValueError,OSError,RuntimeError,TimeoutError) as error:
            result['reason']=str(error)
            result['incomplete_eviction']=(entry/'evicted.json').exists()
        rows.append(result)
    return dict(entries=rows,reclaimed_bytes=sum(r.get('reclaimed_bytes',0) for r in rows),
                courses_touched=False,lock_files_preserved=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan','apply'))
    parser.add_argument('--min-age-days',type=int,default=7)
    parser.add_argument('--plan-file',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='plan':value=plan(args.min_age_days)
        else:
            if args.plan_file is None:raise ValueError('apply requires --plan-file')
            checked(args.plan_file)
            from learning_tools import read_json
            value=apply(read_json(args.plan_file))
        print(json.dumps(value,ensure_ascii=False,indent=2))
    except (ValueError,OSError,RuntimeError,TimeoutError,RecursionError) as error:
        print('Error:',error,file=sys.stderr);sys.exit(1)

if __name__=='__main__':main()
