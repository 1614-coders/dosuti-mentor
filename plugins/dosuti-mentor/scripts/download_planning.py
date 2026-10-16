"""Explicit metadata-only planning and bounded quality selection."""
import json,math,shutil,subprocess,sys
from pathlib import Path
from urllib.parse import urlsplit
from storage_paths import source_kind,local_path
from source_integrity import file_hash


def quality_format(quality):
    if quality=='best':return 'bv*+ba/b'
    if type(quality) is not str or quality not in ('360','480','720','1080'):
        raise ValueError('Video quality must be best, 360, 480, 720 or 1080')
    # No silent higher-resolution fallback when the requested ceiling is unavailable.
    return f'bv*[height<={quality}]+ba/b[height<={quality}]'


def estimate(info):
    rows=info.get('requested_formats') or info.get('requested_downloads') or [info]
    if not isinstance(rows,list) or not rows:return None,'unknown'
    total=0;approx=False
    for row in rows:
        if not isinstance(row,dict):return None,'unknown'
        size=row.get('filesize');kind='exact_metadata'
        if type(size) not in (int,float) or not math.isfinite(size) or size<=0:
            size=row.get('filesize_approx');kind='approximate_metadata'
        if type(size) not in (int,float) or not math.isfinite(size) or size<=0:
            duration=info.get('duration');rate=row.get('tbr')
            if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (duration,rate)):return None,'unknown'
            size=duration*rate*1000/8;kind='bitrate_estimate'
        total+=int(size);approx|=kind!='exact_metadata'
    return total,'estimate' if approx else 'exact_metadata'


def download_plan(args):
    import course
    from video_sources import cached,MEDIA_EXTENSIONS,download_failure
    directory=course.lesson_path(args);data=course.read(directory/'lesson.json');video=data['video'];source=video['source']
    quality=getattr(args,'video_quality','best');fmt=quality_format(quality)
    result=dict(quality=quality,downloads_video=False,metadata_network=False,estimated_download_bytes=None,
                estimate_kind='unknown',space_advisory='Download estimate excludes ASR audio, frames, job copies, merge temporaries and filesystem overhead.')
    result['filesystem_free_bytes']=shutil.disk_usage(directory).free
    if source_kind(source)=='local':
        path=local_path(source)
        if not path.is_file():raise ValueError('Local video is missing')
        result.update(source_state='local_file',local_bytes=path.stat().st_size,quality_applies=False)
        return result
    assets=directory/'video'
    ready=cached(assets,video) if assets.exists() else None
    if ready:
        result.update(source_state='verified_cache',local_bytes=ready.stat().st_size,quality_applies=False,
                      note='Existing pinned cache is reused; quality changes require a separately registered lesson.')
        return result
    parsed=urlsplit(source)
    if parsed.hostname in ('disk.yandex.ru','disk.yandex.com','yadi.sk') or Path(parsed.path).suffix.lower() in MEDIA_EXTENSIONS:
        result.update(source_state='direct_or_yandex',quality_applies=False,note='No format selection for direct/Yandex files; size is unknown without fetching metadata.')
        return result
    cmd=[sys.executable,'-m','yt_dlp','--ignore-config','--no-playlist','--no-plugin-dirs','--socket-timeout','20','--retries','1',
         '--skip-download','--dump-single-json','--playlist-end','2','--format',fmt,'--',source]
    try:
        p=subprocess.run(cmd,capture_output=True,text=True,check=True,timeout=120);info=json.loads(p.stdout)
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
        detail=getattr(error,'stderr','') or ''
        if isinstance(detail,bytes):detail=detail.decode(errors='replace')
        _,message=download_failure(error,detail);raise ValueError(message) from error
    if not isinstance(info,dict) or info.get('_type') in ('playlist','multi_video') or info.get('is_live') or info.get('live_status') in ('is_live','is_upcoming'):
        raise ValueError('Planning requires one recorded video')
    size,kind=estimate(info)
    result.update(source_state='site_metadata',quality_applies=True,metadata_network=True,estimated_download_bytes=size,estimate_kind=kind)
    # Return only a small allowlist, never CDN URLs, credentials or arbitrary metadata.
    result['selected_formats']=[{k:r.get(k) for k in ('format_id','height','ext') if type(r.get(k)) in (str,int,float)} for r in (info.get('requested_formats') or [info]) if isinstance(r,dict)]
    result['download_alone_exceeds_free_space']=None if size is None else size>result['filesystem_free_bytes']
    return result
