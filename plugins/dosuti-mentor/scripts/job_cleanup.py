"""Prune only hash-matched audio/frame work copies, retaining locks and metadata."""
import hashlib,json
from pathlib import Path
from atomic_files import atomic_json
from source_integrity import file_hash,verify_evidence,safe_asset,digest
from storage_lock import file_lock
from publication import valid_publication


def ordinary(path,base):
    path=Path(path)
    if not path.is_relative_to(base):raise ValueError('Cleanup path outside lesson')
    for p in [path,*path.parents]:
        if p==base.parent:break
        if p.is_symlink():raise ValueError('Cleanup does not follow symbolic links')
    return path


def inspect(directory,data,key):
    import course
    course.slug(key)
    directory=Path(directory).resolve();working=ordinary(directory/'.jobs'/key,directory)
    if not working.is_dir():raise ValueError('Working job is unavailable')
    jobs=[j for j in data.get('processing_jobs',{}).values() if j.get('job_key')==key]
    if not jobs or any(j.get('status')!='complete' for j in jobs):raise ValueError('Job must be registered complete; pending or historical jobs are protected')
    if (working/'payload-cleaned.json').exists():raise ValueError('Job payload already cleaned; use prepare to reuse its publication')
    checkpoint=course.read(ordinary(working/'checkpoint.json',directory))
    if checkpoint.get('status')!='complete':raise ValueError('Working checkpoint is incomplete')
    checksum=digest(data['video'].get('sha256'))
    if checkpoint.get('source_sha256')!=checksum:raise ValueError('Job source differs from lesson')
    manifest=course.read(ordinary(working/'manifest.json',directory));verify_evidence(working,manifest,audio=True)
    candidates=[f for f in data.get('prepared_fragments',[]) if f.get('job_key')==key]
    published=None
    for f in candidates:
        folder=ordinary(Path(f['folder']),directory)
        if folder.is_relative_to(directory/'prepared') and valid_publication(folder,key,checksum):
            pub_manifest=course.read(folder/'manifest.json')
            if (pub_manifest.get('evidence_sha256')==manifest.get('evidence_sha256') and
                pub_manifest.get('audio_sha256')==manifest.get('audio_sha256')):
                published=folder;break
    if published is None:raise ValueError('No intact matching registered publication; preserve recovery data')
    names=['audio.wav',*[f['file'] for f in manifest['frames']]]
    if len(names)!=len(set(names)):raise ValueError('Duplicate cleanup paths')
    refs_data=[data]
    course_root=directory.parents[1]
    for index,meta in enumerate((course_root/'lessons').glob('*/lesson.json')):
        if index>=2000:raise ValueError('Too many lessons to establish cleanup dependencies')
        if meta.parent==directory:continue
        ordinary(meta,course_root)
        if meta.stat().st_size>4*1024*1024:raise ValueError('Lesson metadata exceeds cleanup scan limit')
        refs_data.append(course.read(meta))
    files=[]
    for name in names:
        ordinary(working/name,directory)
        path=ordinary(safe_asset(working,name),directory)
        if name!='audio.wav' and (not name.startswith('frames/') or len(Path(name).parts)!=2):raise ValueError('Unexpected frame layout')
        counterpart=ordinary(safe_asset(published,name),directory)
        actual=file_hash(path)
        if actual!=file_hash(counterpart):raise ValueError('Work payload differs from published payload')
        # Conservative scan for extra explicit references, excluding job bookkeeping.
        def strings(value):
            if isinstance(value,str):yield value
            elif isinstance(value,dict):
                for v in value.values():yield from strings(v)
            elif isinstance(value,list):
                for v in value:yield from strings(v)
        check=[{k:v for k,v in item.items() if k not in ('processing_jobs','storage_paths')} for item in refs_data]
        for v in strings(check):
            if Path(v).is_absolute():
                ref=Path(v).resolve()
                if ref==working or ref==path or (ref.is_relative_to(working) and path.is_relative_to(ref)):
                    raise ValueError('Lesson metadata explicitly references working payload')
        files.append(dict(file=name,sha256=actual,size=path.stat().st_size))
    identity=dict(job_key=key,source_sha256=checksum,publication=published.relative_to(directory).as_posix(),files=files,
                  lesson_sha256=file_hash(directory/'lesson.json'),checkpoint_sha256=file_hash(working/'checkpoint.json'))
    revision=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    return dict(identity,revision=revision,logical_bytes=sum(f['size'] for f in files),deletes_only_work_payload=True,
                retains=['checkpoint and processing lock','all published evidence','teacher materials','learner progress','work notes/transcript'],
                note='Explicit paths in lesson metadata checked; external references cannot be discovered.')


def cleanup_job(args):
    import course
    directory=course.lesson_path(args);key=args.job_key;course.slug(key)
    working=ordinary(directory/'.jobs'/key,directory)
    # Lock files persist; never remove/replace them during cleanup.
    if not working.is_dir():raise ValueError('Working job unavailable')
    ordinary(directory/'.lesson.lock',directory)
    ordinary(working/'.processing.lock',directory)
    with file_lock(directory/'.lesson.lock',timeout=.1):
        with file_lock(working/'.processing.lock',timeout=.1):
            data=course.read(directory/'lesson.json');plan=inspect(directory,data,key)
            if not getattr(args,'apply',False):return dict(applied=False,plan=plan)
            if args.expected_revision!=plan['revision']:raise ValueError('Cleanup plan changed; inspect the fresh plan before applying')
            # Write receipt first: even an interrupted partial prune requires verified
            # publication reuse, never attempts resume against missing payload.
            atomic_json(working/'payload-cleaned.json',dict(schema_version=1,job_key=key,source_sha256=plan['source_sha256'],publication=plan['publication']))
            removed=0
            for row in plan['files']:
                path=ordinary(working/row['file'],directory)
                if file_hash(path)!=row['sha256']:raise ValueError('Payload changed during cleanup')
                path.unlink();removed+=row['size']
            return dict(applied=True,removed_logical_bytes=removed,plan=plan,
                        note='Logical bytes are not a guarantee of filesystem space reclaimed.')


def cleaned_publication(working,directory,data,key):
    """Only consulted if receipt exists: fail closed instead of a broken resume."""
    import course
    marker=ordinary(working/'payload-cleaned.json',directory)
    receipt=course.read(marker)
    if (not isinstance(receipt,dict) or receipt.get('schema_version')!=1 or receipt.get('job_key')!=key or
        receipt.get('source_sha256')!=data['video'].get('sha256')):raise ValueError('Invalid cleanup receipt; use prepare --rebuild')
    # Match trusted active registry, not a caller-supplied path from the receipt.
    for fragment in data.get('prepared_fragments',[]):
        if fragment.get('job_key')==key:
            folder=ordinary(Path(fragment['folder']),directory)
            if folder.is_relative_to(directory/'prepared') and valid_publication(folder,key,receipt['source_sha256']):return folder
    raise ValueError('Cleaned job publication missing or damaged; restore it or use prepare --rebuild')
