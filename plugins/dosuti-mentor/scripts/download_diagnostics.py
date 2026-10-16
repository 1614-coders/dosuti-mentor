"""Read bounded, advisory download status without fetching or blessing media."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
from atomic_files import atomic_json

CATEGORIES = {
 'timeout': 'Increase --site-timeout or provide a local video file.',
 'access_required': 'Provide an accessible public link or a local video file.',
 'network': 'Check the connection and retry, or provide a local video file.',
 'unsupported': 'Provide a direct video URL or a local video file.',
 'site_failed': 'Check source availability or provide a local video file.',
 'validation_or_download_failed': 'Inspect the download error; do not bypass media or hash checks.',
 'interrupted': 'Check whether the process stopped before retrying.'}


def record(assets, source, **values):
    atomic_json(Path(assets)/'download-status.json',dict(values,schema_version=1,
        source_url_sha256=hashlib.sha256(source.encode()).hexdigest(),
        updated=datetime.now(timezone.utc).isoformat()))


def describe(directory, video):
    path=Path(directory)/'video'/'download-status.json'
    base=dict(advisory_only=True,media_integrity='not_checked')
    try:
        if path.parent.is_symlink() or path.is_symlink():raise ValueError('linked_status')
        if not path.exists():return dict(base,status='not_recorded',next_step='No site-download attempt is recorded; use prepare when needed.')
        st=path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size>65536:raise ValueError('invalid_status_file')
        with path.open('rb') as stream:raw=stream.read(65537)
        if len(raw)>65536:raise ValueError('oversized_status')
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('invalid_status_object')
        state=value.get('status');category=value.get('category')
        if state not in ('running','ready','failed','interrupted') or value.get('adapter')!='yt-dlp':
            raise ValueError('invalid_status_fields')
        for key,low,high in [('fragments',1,8),('timeout',120,7200)]:
            if type(value.get(key)) is not int or not low<=value[key]<=high:raise ValueError('invalid_status_options')
        bound='legacy_unbound'
        if 'schema_version' in value:
            if type(value['schema_version']) is not int or value['schema_version']!=1:raise ValueError('unknown_status_schema')
            if value.get('source_url_sha256')!=hashlib.sha256(video['source'].encode()).hexdigest():
                return dict(base,status='source_mismatch',next_step='Ignore this report: it belongs to a different source.')
            stamp=value.get('updated')
            if not isinstance(stamp,str) or len(stamp)>64:raise ValueError('invalid_status_time')
            parsed=datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:raise ValueError('invalid_status_time')
            bound='source_url_matches'
        if category is not None and category not in CATEGORIES:raise ValueError('unknown_status_category')
        result=dict(base,status=state,adapter='yt-dlp',fragments=value['fragments'],timeout=value['timeout'],source_binding=bound)
        if bound=='source_url_matches':result['updated']=value['updated']
        if category is not None:result['category']=category
        result['next_step']=(CATEGORIES.get(category,CATEGORIES['site_failed']) if state=='failed' else
            'Check whether a download is still running before retrying; this report does not prove a live process.' if state=='running' else
            CATEGORIES['interrupted'] if state=='interrupted' else
            'The last attempt completed. Use normal prepare/context checks before teaching.')
        return result
    except (ValueError,OSError,TypeError,KeyError,RecursionError):
        return dict(base,status='unreadable',next_step='Preserve the diagnostic file and inspect it; learner progress remains available.')
