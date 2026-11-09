"""Bounded local/public material ingestion; extraction is not semantic review."""
from copy import deepcopy
from html.parser import HTMLParser
import hashlib,json
from pathlib import Path,PurePosixPath,PureWindowsPath
import shutil,subprocess,sys,uuid,zipfile
from urllib.parse import urlsplit
from source_integrity import file_hash
from storage_paths import source_kind
from material_import import importing, reserve, remove_owned

MAX_BYTES=32*1024*1024

class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript','template'):self.hidden+=1
        if tag in ('p','div','br','li','h1','h2','h3','pre','section','tr'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript','template'):self.hidden=max(0,self.hidden-1)
        if tag in ('p','div','li','pre','section','tr'):self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def text_content(path,media_type=''):
    path=Path(path)
    if path.stat().st_size>MAX_BYTES:raise ValueError('Text material exceeds 32 MiB')
    with path.open('rb') as stream:signature=stream.read(5)
    if signature==b'%PDF-' or 'pdf' in media_type.lower() or path.suffix.lower()=='.pdf':
        from pdf_content import extract
        return extract(path)
    raw=path.read_bytes()
    if raw.startswith((b'%PDF',b'PK\x03\x04')):raise ValueError('ZIP/binary requires a separate text extraction workflow')
    try:value=raw.decode('utf-8-sig')
    except UnicodeDecodeError:raise ValueError('Only UTF-8 text/HTML is supported; provide an extracted text file for PDF or other formats')
    if '\x00' in value:raise ValueError('Binary material cannot be treated as text')
    html=('html' in media_type.lower() or path.suffix.lower() in ('.html','.htm') or value.lstrip().lower().startswith(('<!doctype html','<html')))
    method='utf8_text'
    if html:
        from html_content import extract
        value,method=extract(value)
    if not value.strip():raise ValueError('No readable text extracted; login/JavaScript pages may require a local file')
    if not html:
        # Only normalize the final line ending; surrounding whitespace may be code.
        return value.removesuffix('\n').removesuffix('\r')+'\n', method
    return value.strip()+'\n', method


def acquire(directory,source):
    from safe_material_import import current
    attempt=current(directory)
    folder,fd=attempt.fresh('materials')
    remote=source_kind(source)=='remote'
    name=Path(urlsplit(source).path).name if remote else Path(source).name
    name=name if name and name not in ('.','..') else 'material'
    raw=folder/name
    if remote:
        raw,info=attempt.download(source,folder,fd,name)
        effective=info.get('url_effective',source);media=info.get('content_type') or ''
        size=raw.stat().st_size;checksum=attempt.files[raw.absolute()][3]
    else:
        copied=attempt.copy(source,fd,name,destination=raw)
        size=copied['size'];checksum=copied['sha256'];effective=None;media=''
    return raw,dict(source=source,effective_url=effective,media_type=media,raw_file=str(raw),sha256=checksum,size=size)


def validate_archive(archive):
    entries=archive.infolist()
    if len(entries)>5000 or sum(x.file_size for x in entries)>256*1024*1024:raise ValueError('Code archive exceeds extraction limits')
    for e in entries:
        p=PurePosixPath(e.filename)
        if p.is_absolute() or '..' in p.parts or '\\' in e.filename or PureWindowsPath(e.filename).drive or (e.external_attr>>16)&0o170000==0o120000:
            raise ValueError('Archive contains unsafe paths or symbolic links')


@importing
def attach_material(args):
    import course as c
    directory=c.lesson_path(args)
    source_kind(args.source)  # Validate before acquire creates temporary files.
    if (sys.platform=='win32' and args.kind=='homework' and source_kind(args.source)=='local'
            and Path(args.source).suffix.lower() not in ('.pdf','.html','.htm','.zip')):
        from windows_homework import attach_homework
        return attach_homework(args,directory)
    raw,origin=acquire(directory,args.source)
    target=raw
    if args.kind=='homework':
        value,method=text_content(raw,origin['media_type']);origin['extraction']=method
    elif not zipfile.is_zipfile(raw):
        with raw.open('rb') as stream:signature=stream.read(5)
        if signature==b'%PDF-' or 'pdf' in origin['media_type'].lower() or raw.suffix.lower()=='.pdf':
            raise ValueError('PDF is teaching material, not a code file')
        # Reject HTML error/login bodies presented as downloadable code.
        if 'html' in origin['media_type'].lower() or raw.read_bytes()[:1024].lstrip().lower().startswith((b'<!doctype html',b'<html')):raise ValueError('Code URL returned HTML; provide a raw code file or ZIP')
        text_content(raw,origin['media_type'])
    clone=deepcopy(args);clone.source=str(target);clone.material_origin=origin
    if args.kind=='homework':clone.material_text=value
    c.attach_local(clone)


def resource_revision(item):
    return 'sha256:'+hashlib.sha256((item['origin']['sha256']+':'+item['text_sha256']).encode()).hexdigest()


def integrity(path,expected):
    if not isinstance(path,str) or not isinstance(expected,str):return 'unverifiable'
    p=Path(path)
    if p.is_symlink() or not p.is_file():return 'missing_or_linked'
    return 'verified' if file_hash(p)==expected else 'changed'


def describe(resources):
    result=deepcopy(resources)
    for item in result.get('items',[]):
        origin=item.get('origin')
        if origin:
            item['source_integrity']=integrity(origin.get('raw_file'),origin.get('sha256'))
        if item.get('extracted_file'):
            item['text_integrity']=integrity(item.get('extracted_file'),item.get('text_sha256'))
        if item.get('review') and (item.get('source_integrity')!='verified' or item.get('text_integrity')!='verified'):
            item['status']='needs_review';item['review_valid']=False
        elif item.get('review'):
            item['review_valid']=item['review'].get('revision')==resource_revision(item)
            if not item['review_valid']:item['status']='needs_review'
    return result


def resource(args):
    # Hold a per-resource lock across import cleanup as well as registration.
    # This protects the shared visual cache from a losing request's rollback.
    if getattr(args,'visual_pages',None) is not None:
        import course
        from storage_lock import file_lock
        if args.action!='extract':raise ValueError('--visual-pages is only valid for resource extract')
        from pdf_visuals import validate_pages
        validate_pages(args.visual_pages)
        directory=course.lesson_path(args);identity=course.slug(args.resource_id)
        course.read(directory/'lesson.json')
        with file_lock(directory/('.visual-'+identity+'.lock'),timeout=120):return _resource(args)
    return _resource(args)


@importing
def _resource(args):
    import course as c
    if getattr(args,'visual_pages',None) is not None:
        if args.action!='extract':raise ValueError('--visual-pages is only valid for resource extract')
        from pdf_visuals import validate_pages
        validate_pages(args.visual_pages)
    directory=c.lesson_path(args);data=c.read(directory/'lesson.json')
    if args.action=='list':return describe(data['resources'])
    items=data['resources'].get('items',[])
    original=next((i for i in items if i['id']==args.resource_id),None)
    if original is None:raise ValueError('Unknown resource ID')
    if original.get('kind')=='video':raise ValueError('Prepare video resources as videos; text extraction does not process video')
    if sys.platform=='win32':
        if args.action=='fetch':
            from windows_pdf_intake import registered_pdf,fetch_pdf
            if registered_pdf(original):return fetch_pdf(args,directory,original)
        from windows_resource_text import eligible,resource_text
        if eligible(original,args):return resource_text(args,directory,original)
    item=deepcopy(original)
    if args.action in ('fetch','extract'):
        if not item.get('origin'):
            raw,origin=acquire(directory,item['source']);item.update(origin=origin,status='downloaded')
        else:
            origin=item['origin'];raw=Path(origin['raw_file'])
            if integrity(str(raw),origin['sha256'])!='verified':raise ValueError('Resource cache changed; attach a new trusted version')
        if args.action=='extract':
            if item.get('extracted_file'):
                if integrity(item['extracted_file'],item['text_sha256'])!='verified':raise ValueError('Extracted text changed; attach a new trusted version')
            else:
                value,method=text_content(raw,origin.get('media_type',''))
                from safe_material_import import current
                attempt=current(directory);text_folder,parent=attempt.fresh('materials')
                target,checksum=attempt.text(text_folder,parent,'text-'+uuid.uuid4().hex+'.txt',value)
                item.update(extracted_file=str(target),text_sha256=checksum,extraction=method,status='extracted_not_reviewed')
            if getattr(args,'visual_pages',None) is not None:
                if item.get('extraction')!='pdf_text_layer_v1':raise ValueError('Page images require a PDF resource')
                from pdf_visuals import prepare
                item['visuals']=prepare(raw,item,args.visual_pages)
            item['revision']=resource_revision(item)
    elif args.action=='review':
        if not item.get('extracted_file'):raise ValueError('Extract and read the resource text before review')
        if args.expected_revision!=resource_revision(item):raise ValueError('Resource revision changed; read the current extracted text')
        if not isinstance(args.notes,str) or not args.notes.strip() or len(args.notes)>4000:raise ValueError('Review requires notes within 4000 characters')
        item.update(status='reviewed',review=dict(revision=args.expected_revision,notes=args.notes,updated=c.now(),kind='assistant_recorded_review'))
    else:raise ValueError('Unknown resource action')
    def update(latest):
        current=next((i for i in latest['resources'].get('items',[]) if i['id']==args.resource_id),None)
        if current!=original:raise ValueError('Resource changed concurrently; read its current state')
        if integrity(item['origin']['raw_file'],item['origin']['sha256'])!='verified':raise ValueError('Resource source changed during operation')
        if item.get('extracted_file') and integrity(item['extracted_file'],item['text_sha256'])!='verified':raise ValueError('Resource text changed during operation')
        if item.get('visuals'):
            from pdf_visuals import verify
            verify(item['visuals'],item['origin']['raw_file'],item['origin']['sha256'])
        current.clear();current.update(item)
    from material_import import _CURRENT
    active=_CURRENT.get().get('safe')
    if active is not None:active.publish(args,change=update)
    else:c.update_lesson(directory,update)
    return describe(c.read(directory/'lesson.json')['resources'])
