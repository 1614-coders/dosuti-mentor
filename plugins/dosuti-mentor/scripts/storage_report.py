"""Read-only bounded course inventory; sizes do not imply safe deletion."""
import heapq
import os
from pathlib import Path
import shutil
import stat

CATEGORIES = {'prepared':'prepared_evidence','evidence':'screen_evidence','.jobs':'processing_work',
              'code':'teacher_code','materials':'attached_materials','homework':'homework'}


def group(parts):
    if parts[0]=='learners':return None,'learner_data'
    if parts[0]=='lessons' and len(parts)>=3:
        lesson=parts[1];kind=parts[2]
        if kind=='video':
            category='download_staging' if len(parts)>3 and parts[3].startswith('.fetch-') else 'video_cache'
        else:category=CATEGORIES.get(kind,'lesson_metadata_or_other')
        return lesson,category
    return None,'course_metadata_or_other'


def inventory(root, max_entries=200000):
    if type(max_entries) is not int or not 1<=max_entries<=1000000:
        raise ValueError('max-entries must be an integer from 1 to 1000000')
    root=Path(root).expanduser().resolve()
    if not root.is_dir():raise ValueError('Course folder is missing')
    stack=[root];groups={};seen=set();largest=[];errors=[]
    count=logical=allocated=unique=links=special=visited=0;limited=False;allocation_available=True
    error_count=0
    def failure(path):
        nonlocal error_count
        error_count+=1
        if len(errors)<50:errors.append(str(path.relative_to(root)))
    while stack and not limited:
        directory=stack.pop()
        try:
            # Avoid following ordinary symbolic links, including queued directories.
            if directory.is_symlink():links+=1;continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    if visited>=max_entries:limited=True;break
                    visited+=1;path=Path(entry.path)
                    try:
                        info=entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):links+=1;continue
                        if stat.S_ISDIR(info.st_mode):stack.append(path);continue
                        if not stat.S_ISREG(info.st_mode):special+=1;continue
                        relative=path.relative_to(root);key=group(relative.parts)
                        row=groups.setdefault(key,dict(lesson=key[0],category=key[1],files=0,logical_bytes=0))
                        size=info.st_size;row['files']+=1;row['logical_bytes']+=size;count+=1;logical+=size
                        identity=(info.st_dev,info.st_ino) if info.st_ino else ('path',str(relative))
                        if identity not in seen:
                            seen.add(identity);unique+=1
                            blocks=getattr(info,'st_blocks',None)
                            if blocks is None:allocation_available=False
                            else:allocated+=blocks*512
                        item=(size,relative.as_posix())
                        if len(largest)<20:heapq.heappush(largest,item)
                        elif item>largest[0]:heapq.heapreplace(largest,item)
                    except OSError:failure(path)
        except OSError:failure(directory)
    try:free=shutil.disk_usage(root).free
    except OSError:free=None
    return dict(schema_version=1,course=str(root),read_only=True,scan_complete=not limited and error_count==0,
        limit_reached=limited,max_entries=max_entries,entries_visited=visited,
        totals=dict(file_paths=count,unique_file_ids=unique,logical_bytes=logical,
                    allocated_file_bytes=allocated if allocation_available else None),
        groups=sorted(groups.values(),key=lambda r:((r['lesson'] or ''),r['category'])),
        largest_files=[dict(path=name,logical_bytes=size) for size,name in sorted(largest,reverse=True)],
        skipped=dict(symbolic_links=links,special_files=special,error_count=error_count,error_paths=errors),
        filesystem_free_bytes=free,reclaimable_bytes=None,
        limitations=['Inventory is not an atomic snapshot; processing may change files during scanning.',
            'Logical bytes count each file path, including hard links. Allocated file bytes count each file ID once when supported; directories, shared APFS extents and quotas are not measured.',
            'External source videos, shared models/runtime and symlink targets are not traversed.',
            'Directory categories do not prove that files are unused or safe to delete.'])


def storage_report(args):
    import course
    root=course.root(args);course.read(root/'course.json')
    return inventory(root,getattr(args,'max_entries',200000))
