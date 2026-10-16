"""Decode timestamped screen samples and keep visual-change candidates for review."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import uuid
from atomic_files import atomic_json
from source_integrity import file_hash, safe_asset, source_matches
from storage_lock import file_lock


def bounds(start, duration, sample_every):
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start,duration,sample_every)):
        raise ValueError('Screen times must be finite numbers')
    if start < 0 or duration <= 0 or sample_every <= 0:
        raise ValueError('Screen start must be nonnegative; duration/interval positive')


def extract_frames(video, folder, start, duration, sample_every, ffmpeg):
    bounds(start,duration,sample_every)
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    # Limit the input, so a filter with no later output does not decode the rest of a long lecture.
    selection=f"select='lt(t,{duration})*(isnan(prev_selected_t)+gte(t-prev_selected_t,{sample_every}))',showinfo"
    result=subprocess.run([ffmpeg,'-hide_banner','-loglevel','info','-y',
        '-ss',str(start),'-t',str(duration),'-i',str(video),'-an','-vf',selection,
        '-fps_mode','vfr','-q:v','2',str(folder/'%05d.jpg')],
        check=False,capture_output=True,text=True)
    stamps=[(int(n),float(t)) for n,t in re.findall(r'n:\s*(\d+)\s+pts:.*?pts_time:\s*([\d.eE+-]+)',result.stderr)]
    files=sorted(folder.glob('*.jpg'))
    if not files:
        raise ValueError('No video frames in the requested interval')
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode,result.args,output=result.stdout,stderr=result.stderr)
    from media_input import decoding_errors
    errors = decoding_errors(result.stderr)
    if errors:
        raise ValueError('Damaged video: ' + '; '.join(errors)[:600])
    if len(stamps)!=len(files) or [n for n,t in stamps]!=list(range(len(files))):
        raise ValueError('Decoded frame timestamps do not match output images')
    records=[]
    previous=-1
    for path,(_,seconds) in zip(files,stamps):
        if not math.isfinite(seconds) or seconds < 0 or seconds >= duration or seconds <= previous:
            raise ValueError('Decoded frame timestamp is outside interval or not increasing')
        previous=seconds
        records.append(dict(approx_seconds=start+seconds,source_seconds=start+seconds,
            decoded_offset=seconds,file=path.name,timestamp_accuracy='decoded_video_timestamp'))
    return records


def visual_difference(previous,current):
    from PIL import ImageChops, ImageStat
    difference=ImageChops.difference(previous,current)
    pixels=difference.histogram()
    fraction=sum(pixels[20:])/sum(pixels)
    tile=max(ImageStat.Stat(difference.crop((x,y,min(x+16,current.width),min(y+16,current.height)))).mean[0]
        for y in range(0,current.height,16) for x in range(0,current.width,16))
    return dict(changed_fraction=round(fraction,6),max_tile_mean=round(tile,3))


def scan_settings(checksum, start, duration, sample_every):
    settings=dict(source_sha256=checksum,start=float(start),duration=float(duration),
                  sample_every=float(sample_every),algorithm='screen-pixels-v5')
    return settings, hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()


def local_difference(previous, current):
    """Count strong local pixel changes before thumbnail reduction; not OCR or semantic proof."""
    import numpy as np
    delta = np.max(np.abs(np.asarray(current, dtype=np.int16) - np.asarray(previous, dtype=np.int16)), axis=2)
    changed = delta >= 48
    # Preserve glyphs at source resolution. Small tiles avoid dilution by a large editor background.
    h,w = changed.shape
    padded = np.pad(changed, ((0, (-h)%16), (0, (-w)%16)))
    tiles = padded.reshape((padded.shape[0]//16,16,padded.shape[1]//16,16)).sum(axis=(1,3))
    ys,xs=np.where(changed)
    box=[int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1] if len(xs) else None
    return dict(strong_pixels=int(changed.sum()), max_tile_pixels=int(tiles.max()), bounds=box)


class RepeatedCaret:
    """Compact only repeated, reversible thin-bar changes; retain first occurrences and anchors."""
    def __init__(self):
        self.box=None
        self.old_patch=None
        self.reversals=0

    def observe(self, previous, current, metrics):
        import numpy as np
        box=metrics.get('bounds') if metrics else None
        if box is None:
            self.box=None; self.old_patch=None; self.reversals=0
            return False
        x0,y0,x1,y1=box
        area=(x1-x0)*(y1-y0)
        thin=(1<=x1-x0<=3 and 8<=y1-y0<=48 and metrics['strong_pixels']>=.85*area)
        if not thin:
            self.box=None; self.old_patch=None; self.reversals=0
            return False
        before=np.asarray(previous.crop(box),dtype=np.int16)
        after=np.asarray(current.crop(box),dtype=np.int16)
        reversible=(box==self.box and self.old_patch is not None and
                    np.max(np.abs(after-self.old_patch))<24)
        self.reversals=self.reversals+1 if reversible else 0
        self.box=box; self.old_patch=before
        return self.reversals>=2


def build_overviews(folder, rows):
    """Navigation only: retain every candidate and its full-resolution source."""
    from PIL import Image, ImageDraw
    candidates = [row for row in rows if row['review_candidate']]
    target = Path(folder) / 'overviews'
    target.mkdir(exist_ok=True)
    groups = []
    for offset in range(0, len(candidates), 12):
        batch = candidates[offset:offset+12]
        sheet = Image.new('RGB', (960, 205*((len(batch)+3)//4)), 'white')
        draw = ImageDraw.Draw(sheet)
        for index, row in enumerate(batch):
            with Image.open(safe_asset(folder, row['file'])) as original:
                thumbnail = original.convert('RGB')
                thumbnail.thumbnail((240, 180))
            x, y = index%4*240, index//4*205
            sheet.paste(thumbnail, (x, y))
            draw.text((x+3, y+183), f"{row['id']}  {row['approx_seconds']:.3f}s", fill='black')
        path = target / f'{len(groups)+1:04d}.jpg'
        sheet.save(path, quality=90)
        groups.append(dict(file=path.relative_to(folder).as_posix(), sha256=file_hash(path),
                           frame_ids=[row['id'] for row in batch], purpose='navigation_only'))
    return groups


def scan(video, output, start, duration, sample_every, checksum, rebuild=False,preferred=None,generation=None):
    import imageio_ffmpeg
    from PIL import Image
    bounds(start,duration,sample_every)
    if duration>300 or duration/sample_every>1200:
        raise ValueError('Scan at most 300 seconds and 1200 sample intervals; use smaller lecture sections')
    output=Path(output)
    settings,key=scan_settings(checksum,start,duration,sample_every)
    output.mkdir(parents=True,exist_ok=True)
    target=output/('scan-'+key[:24])
    with file_lock(output/('.'+key+'.lock')):
        if generation:
            if not re.fullmatch('[a-f0-9]{12}', generation):
                raise ValueError('Invalid screen generation')
            target=target.with_name(target.name+'-'+generation)
        elif rebuild:
            target=target.with_name(target.name+'-'+uuid.uuid4().hex[:8])
        elif preferred:
            target=Path(preferred).resolve()
            if not target.is_relative_to(output.resolve()):
                raise ValueError('Screen scan path must stay within its storage')
        if target.exists():
            marker=json.loads((target/'completion.json').read_text())
            if marker.get('key')!=key or marker.get('receipt_sha256')!=file_hash(target/'screen.json'):
                raise ValueError('Screen scan metadata integrity changed; use a fresh output')
            receipt=json.loads((target/'screen.json').read_text())
            source_matches(receipt,checksum)
            if receipt.get('settings')!=settings:
                raise ValueError('Screen scan settings changed')
            verify_scan(target,receipt)
            return target
        staging=output/('.pending-'+uuid.uuid4().hex)
        staging.mkdir()
        try:
            if file_hash(video)!=checksum:
                raise ValueError('Video changed before screen scan')
            rows=extract_frames(video,staging/'frames',start,duration,sample_every,imageio_ffmpeg.get_ffmpeg_exe())
            previous=None;last_selected=None;previous_rgb=None;last_anchor=-math.inf
            caret=RepeatedCaret()
            for index,row in enumerate(rows,1):
                row['id']=f'f{index:05d}'
                row['file']='frames/'+row['file']
                with Image.open(staging/row['file']) as image:
                    rgb=image.convert('RGB')
                    current=rgb.convert('L').resize((320,180))
                metrics=visual_difference(previous,current) if previous else None
                local=local_difference(previous_rgb,rgb) if previous_rgb is not None else None
                repeated_caret=caret.observe(previous_rgb,rgb,local) if previous_rgb is not None else False
                accumulated=visual_difference(last_selected,current) if last_selected is not None else None
                changed=bool(not repeated_caret and metrics and (metrics['changed_fraction']>=.0005 or metrics['max_tile_mean']>=4 or
                    local['max_tile_pixels']>=4 or accumulated['max_tile_mean']>=4))
                anchor=row['approx_seconds']-last_anchor>=10
                row.update(sha256=file_hash(staging/row['file']),visual_change_metrics=metrics,
                    review_candidate=previous is None or changed or anchor,
                    candidate_reason='initial' if previous is None else ('pixel_change' if changed else ('periodic_anchor' if anchor else ('repeated_caret_like_change' if repeated_caret else 'not_selected'))),
                    local_change_metrics=local, repeated_caret_like_change=repeated_caret,
                    verification='not_inspected',screen_state_confirmed=False)
                if changed and index > 1 and not rows[index-2].get('review_candidate'):
                    rows[index-2].update(review_candidate=True,candidate_reason='before_pixel_change')
                if anchor:last_anchor=row['approx_seconds']
                if row['review_candidate']:last_selected=current
                previous=current
                previous_rgb=rgb
            if file_hash(video)!=checksum:
                raise ValueError('Video changed during screen scan')
            receipt=dict(schema_version=1,source_sha256=checksum,settings=settings,
                requested_interval=[start,start+duration],sample_count=len(rows),frames=rows,
                overviews=build_overviews(staging, rows),
                limitations=['Changes between samples may be missed.','Pixel changes do not identify code or validate meaning.',
                    'Overview thumbnails are navigation only; read code from original frames.',
                    'Repeated reversible thin-bar changes may be compacted as caret-like; all originals remain available.',
                    'All samples need visual review.'])
            atomic_json(staging/'screen.json',receipt)
            atomic_json(staging/'completion.json',dict(key=key,receipt_sha256=file_hash(staging/'screen.json')))
            staging.replace(target)
            return target
        finally:
            if staging.exists():shutil.rmtree(staging)


def verify_scan(folder,receipt,selected=None):
    if not isinstance(receipt,dict):
        raise ValueError('Invalid screen scan')
    frames=receipt.get('frames')
    if receipt.get('schema_version')!=1 or not isinstance(frames,list) or not frames:
        raise ValueError('Invalid screen scan')
    interval=receipt.get('requested_interval')
    if not isinstance(interval,list) or len(interval)!=2:
        raise ValueError('Invalid screen interval')
    bounds(interval[0],interval[1]-interval[0],receipt['settings']['sample_every'])
    ids=set();previous=-1
    for row in frames:
        seconds=row.get('approx_seconds')
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or not interval[0]<=seconds<interval[1] or seconds<=previous:
            raise ValueError('Screen timestamp is outside interval or not increasing')
        if not isinstance(row.get('id'),str) or row['id'] in ids:
            raise ValueError('Screen frame ID must be unique')
        ids.add(row['id']);previous=seconds
    for row in frames if selected is None else selected:
        if file_hash(safe_asset(folder,row.get('file')))!=row.get('sha256'):
            raise ValueError('Screen frame integrity changed; scan again in a fresh output')
    if receipt.get('settings', {}).get('algorithm') == 'screen-pixels-v5':
        groups = receipt.get('overviews')
        if not isinstance(groups, list) or not groups:
            raise ValueError('Screen overview index is missing')
        members = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('frame_ids'), list) or not 1 <= len(group['frame_ids']) <= 12:
                raise ValueError('Invalid screen overview group')
            members.extend(group['frame_ids'])
            if file_hash(safe_asset(folder, group.get('file'))) != group.get('sha256'):
                raise ValueError('Screen overview integrity changed')
        if members != [row['id'] for row in frames if row.get('review_candidate')]:
            raise ValueError('Screen overviews do not match ordered candidates')


def context_scans(directory,data,start,stop):
    rows,sources=[],[]
    seen=set()
    for entry in data.get('screen_scans',[]):
        left,right=entry['requested_interval']
        if left>=stop or right<=start:continue
        folder=Path(entry['folder']).resolve()
        if str(folder) in seen:continue
        seen.add(str(folder))
        receipt_file=folder/'screen.json'
        if file_hash(receipt_file)!=entry.get('receipt_sha256'):
            raise ValueError('Screen scan metadata integrity changed')
        receipt=json.loads(receipt_file.read_text())
        source_matches(receipt,data['video']['sha256'])
        verify_scan(folder,receipt,selected=[])
        selected=[f for f in receipt['frames'] if f.get('review_candidate') and start<=f['approx_seconds']<stop]
        in_window=[f for f in receipt['frames'] if start<=f['approx_seconds']<stop]
        fallback=None
        if in_window and in_window[0] not in selected:
            fallback=in_window[0]; selected.insert(0,fallback)
        elif not in_window:
            prior=[f for f in receipt['frames'] if f['approx_seconds']<start and
                   start-f['approx_seconds'] <= receipt['settings']['sample_every']*2]
            if prior:
                fallback=prior[-1]; selected.insert(0,fallback)
        verify_scan(folder,receipt,selected)
        relative=folder.relative_to(Path(directory).resolve()).as_posix()
        identity='v'+hashlib.sha256(relative.encode()).hexdigest()[:16]
        sources.append(dict(fragment_id=identity,folder=str(folder),receipt_file=str(receipt_file),
            requested_interval=receipt['requested_interval'],sample_every=receipt['settings']['sample_every'],
            sample_count=receipt['sample_count'],selected_count=len(selected),limitations=receipt['limitations']))
        chosen = {frame['id'] for frame in selected}
        sources[-1]['overviews'] = [dict(absolute_path=str(safe_asset(folder, group['file'])),
            sha256=group['sha256'], frame_ids=[identity+':'+item for item in group['frame_ids']],
            selected_frame_ids=[identity+':'+item for item in group['frame_ids'] if item in chosen],
            purpose='navigation_only', may_include_frames_outside_window=True)
            for group in receipt.get('overviews', []) if chosen.intersection(group['frame_ids'])]
        for frame in selected:
            row=deepcopy(frame)
            row.update(id=identity+':'+frame['id'],local_id=frame['id'],selection='screen_scan_candidate',
                       absolute_path=str(safe_asset(folder,frame['file'])),
                       provenance=[dict(fragment_id=identity,local_id=frame['id'],packet_file=str(receipt_file))])
            if frame is fallback:
                row['selection']='screen_window_sample' if frame['approx_seconds']>=start else 'preceding_sample'
                row['age_seconds']=max(0,start-frame['approx_seconds'])
            rows.append(row)
    return rows,sources
