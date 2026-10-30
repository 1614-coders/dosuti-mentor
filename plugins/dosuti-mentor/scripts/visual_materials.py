"""Small common video/PDF image view; semantic selection remains an explicit unit annotation."""
from copy import deepcopy
import json
import os
from pathlib import Path
from profile_storage import checked
from source_integrity import file_hash

DEFAULTS=dict(max_candidates=8,max_display=3,max_disk_bytes=16*1024*1024,
              timeout_seconds=20,max_pixels=4_000_000,max_context_bytes=24*1024)
MAXIMA=dict(max_candidates=24,max_display=12,max_disk_bytes=64*1024*1024,
            timeout_seconds=45,max_pixels=12_000_000,max_context_bytes=64*1024)


def limits(changes=None):
    try:changes=changes if changes is not None else json.loads(os.environ.get('DOSUTI_VISUAL_LIMITS','{}'))
    except ValueError as error:raise ValueError('Invalid DOSUTI_VISUAL_LIMITS JSON') from error
    if not isinstance(changes,dict) or set(changes)-set(DEFAULTS):raise ValueError('Unknown visual limit')
    result=DEFAULTS|changes
    if any(type(v) is not int or not 1<=v<=MAXIMA[k] for k,v in result.items()):raise ValueError('Visual limits require bounded positive integers')
    if result['max_context_bytes']<1024:raise ValueError('Visual context limit must be at least1024 bytes')
    return result


def validate_annotations(rows,stored=False):
    if not isinstance(rows,list) or not 1<=len(rows)<=12:raise ValueError('Select 1..12 reviewed visuals')
    ids=set()
    for row in rows:
        fields={'id','caption','notes'}|({'sha256','source_sha256'} if stored else set())
        if not isinstance(row,dict) or set(row)!=fields:raise ValueError('Invalid visual annotation fields')
        for key,maximum in [('id',200),('caption',1000),('notes',4000)]:
            if not isinstance(row[key],str) or not row[key].strip() or len(row[key])>maximum or any(c in row[key] for c in '\n\r\0'):raise ValueError('Invalid visual '+key)
        if row['id'] in ids:raise ValueError('Duplicate selected visual')
        ids.add(row['id'])
        if stored:
            from source_integrity import digest
            digest(row['sha256']);digest(row['source_sha256'])


def video_images(context):
    media=context['context'];source=media['registered_source']['sha256'];rows=[]
    linked={}
    for speech in media['speech']:
        for frame_id in speech.get('frame_ids',[]):linked.setdefault(frame_id,[]).append(speech['id'])
    for frame in media['frames']:
        seconds=frame['approx_seconds']
        if not context['start']<=seconds<context['end']:continue
        rows.append(dict(id=frame['id'],source_id=context['lesson'],source_sha256=source,
            kind='video_frame',path=frame['absolute_path'],sha256=frame['sha256'],
            locator=dict(seconds=seconds),links=dict(speech_ids=linked.get(frame['id'],[])[:20],
                speech_links_truncated=len(linked.get(frame['id'],[]))>20),uncertainties=['pixel_candidate_not_semantic_proof']))
    return rows


def pick(rows,budget):
    # Exact image identity only. Keep latest occurrence of each state, including the final board.
    groups={}
    for row in rows:
        old=groups.get(row['sha256'])
        if old:
            old.update(row);old['occurrence_count']+=1
        else:groups[row['sha256']]=deepcopy(row)|dict(first_locator=row['locator'],occurrence_count=1)
    values=sorted(groups.values(),key=lambda r:r['locator'].get('seconds',r['locator'].get('page',0)))
    count=min(len(values),budget['max_candidates'])
    if count==1:chosen=values[-1:]
    elif count:chosen=[values[round(i*(len(values)-1)/(count-1))] for i in range(count)]
    else:chosen=[]
    kept=[];size=0
    for row in reversed(chosen):  # Reserve disk/context budget for the completed state first.
        path=Path(row['path']);checked(path);weight=path.stat().st_size
        if size+weight>budget['max_disk_bytes']:continue
        kept.append(row);size+=weight
    kept.reverse()
    return kept,len(rows)-len(kept),len(rows)-len(groups)


def bind(source,context,writing=False):
    if source['kind'] not in ('video','resource'):return None,context
    budget=limits();annotations=source.get('visuals',[])
    if source['kind']=='video':rows=video_images(context)
    elif source['kind']=='resource':
        from pdf_visuals import images
        rows=images(context)
    else:return None,context
    candidates,omitted,duplicates=pick(rows,budget)
    for row in candidates:row.update(can_display=False,meaning_verified=False)
    index={r['id']:r for r in rows};selected=[];saved=[];display_bytes=0;inspected=set()
    for annotation in annotations:
        row=deepcopy(index.get(annotation['id']))
        if row is None:raise ValueError('Selected visual is outside the bound source scope')
        canonical=annotation|dict(sha256=row['sha256'],source_sha256=row['source_sha256'])
        if not writing and canonical!=annotation:raise ValueError('Selected visual changed; review unit before reuse')
        path=Path(row['path']);checked(path)
        if path.stat().st_size>budget['max_disk_bytes'] or file_hash(path)!=row['sha256']:raise ValueError('Selected visual changed or exceeds image budget')
        if row['sha256'] not in inspected:
            from PIL import Image
            try:
                with Image.open(path) as im:
                    if im.format not in ('PNG','JPEG') or im.width*im.height>budget['max_pixels']:raise ValueError('Selected visual exceeds pixel budget or is not PNG/JPEG')
                    im.verify()
            except Image.DecompressionBombError as error:raise ValueError('Selected image exceeds safe decoder bounds') from error
            inspected.add(row['sha256'])
        saved.append(canonical)
        if len(selected)<budget['max_display'] and display_bytes+path.stat().st_size<=budget['max_disk_bytes']:
            display_bytes+=path.stat().st_size
            caption=annotation['caption'].replace('\\','\\\\').replace('[','\\[').replace(']','\\]')
            if any(c in str(path) for c in '\n\r<>'):raise ValueError('Unsafe Markdown image path')
            row.update(caption=annotation['caption'],review_notes=annotation['notes'],can_display=True,
                meaning_verified=False,link_verification='assistant_recorded_unit_source_link',
                markdown=f'![{caption}](<{path}>)',link=f'[Открыть изображение](<{path}>)')
            selected.append(row)
    context['visual_materials']=dict(candidates=candidates,selected=selected,omitted_count=omitted,
        duplicate_count=duplicates,selected_omitted_count=len(saved)-len(selected),limits=budget,
        selection='cheap_temporal_candidates_require_inspection',
        limitations=['Candidate sampling can miss important intermediate states.',
                    'Raw audit frames remain available; candidates are references, not extra copies.',
                    'A recorded topic link is not independent verification of image meaning.'])
    result=context['visual_materials']
    result['context_limited']=False
    while len(json.dumps(result,ensure_ascii=False).encode())>budget['max_context_bytes']:
        result['context_limited']=True
        if result['candidates']:
            result['candidates'].pop(0);result['omitted_count']+=1
        elif result['selected']:
            result['selected'].pop();result['selected_omitted_count']+=1
        else:raise ValueError('Visual context metadata exceeds configured limit')
    return saved,context
