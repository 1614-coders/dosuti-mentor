"""On-demand bounded PDF page images; stored in the existing resource import transaction."""
import hashlib
import io
import json
import math
from pathlib import Path
import sys
from profile_storage import checked
from source_integrity import file_hash,safe_asset,digest
from visual_materials import limits


def verify(bundle,raw,expected):
    required={'schema','source_sha256','folder','images','limits','requested_pages','omitted_pages','stored_bytes'}
    if not isinstance(bundle,dict) or not required<=set(bundle):raise ValueError('Incomplete PDF visual index')
    if bundle['schema']!='pdf_visuals_v1':raise ValueError('Unsupported PDF visual index')
    if bundle['source_sha256']!=expected:raise ValueError('PDF visual source changed')
    if not isinstance(bundle['folder'],str) or not isinstance(bundle['limits'],dict):raise ValueError('Invalid visual folder/limits')
    folder=Path(bundle['folder']);checked(folder,True)
    if folder.parent!=Path(raw).parent or not (folder.name=='visuals' or folder.name.startswith('.visual-staging-')):raise ValueError('Visual storage escapes resource')
    budget=limits(bundle['limits'])
    if not isinstance(bundle['images'],list) or len(bundle['images'])>budget['max_candidates']:raise ValueError('Invalid PDF image count')
    for field in ('requested_pages','omitted_pages'):
        values=bundle[field]
        if (not isinstance(values,list) or len(values)>200 or any(type(v) is not int or not 1<=v<=200 for v in values)
                or len(set(values))!=len(values)):
            raise ValueError('Invalid PDF visual page index')
    if any(not isinstance(r,dict) or set(r) not in ({'page','file','sha256'},{'page','file','sha256','path'}) or type(r['page']) is not int for r in bundle['images']):
        raise ValueError('Invalid PDF visual image row')
    if sorted([r['page'] for r in bundle['images']]+bundle['omitted_pages'])!=sorted(bundle['requested_pages']):
        raise ValueError('Incomplete PDF visual receipt')
    pages=set();total=0;files=set()
    for row in bundle['images']:
        if row['page'] in pages or not 1<=row['page']<=200:raise ValueError('Invalid visual page')
        pages.add(row['page'])
        digest(row['sha256'])
        if row['file']!=f"image-{row['sha256']}.png":raise ValueError('Invalid PDF visual file name')
        path=safe_asset(folder,row['file']);checked(path)
        if 'path' in row and (not isinstance(row['path'],str) or Path(row['path'])!=path):raise ValueError('PDF visual path binding changed')
        if row['file'] not in files:total+=path.stat().st_size;files.add(row['file'])
        if total>budget['max_disk_bytes'] or file_hash(path)!=row['sha256']:raise ValueError('PDF page image changed or exceeds storage limit')
    if type(bundle['stored_bytes']) is not int or total!=bundle['stored_bytes']:raise ValueError('Visual size receipt changed')
    return bundle


def render(raw,folder,pages,expected,budget):
    from preparation_control import run
    import subprocess
    try:
        result=run([sys.executable,'-B',str(Path(__file__).resolve()),str(raw),str(folder),expected,
                    json.dumps(pages),json.dumps(budget)],capture_output=True,encoding='utf-8',timeout=budget['timeout_seconds'])
    except subprocess.TimeoutExpired as error:raise ValueError('PDF visuals timed out; no partial images registered') from error
    if result.returncode:raise ValueError('PDF visual rendering failed or exceeded limits; no partial images registered')
    try:
        value=json.loads(result.stdout)
        if value['source_sha256']!=expected or value['requested_pages']!=pages or value['limits']!=budget:raise ValueError('PDF visual binding changed')
        value['folder']=str(folder)
        return verify(value,raw,expected)
    except (ValueError,KeyError,TypeError) as error:raise ValueError('Invalid PDF visual worker response') from error


def validate_pages(pages):
    if (not isinstance(pages,list) or not 1<=len(pages)<=200 or
            any(type(p) is not int or not 1<=p<=200 for p in pages) or len(set(pages))!=len(pages)):
        raise ValueError('Choose distinct PDF pages from 1 to 200')


def prepare(raw,item,pages):
    from material_import import reserve
    import shutil
    import uuid
    budget=limits();validate_pages(pages)
    raw=Path(raw);checked(raw)
    if raw.stat().st_size>32*1024*1024:raise ValueError('PDF visuals input exceeds32MiB')
    expected=item['origin']['sha256']
    if file_hash(raw)!=expected:raise ValueError('PDF source changed')
    old=item.get('visuals')
    if old is not None:
        verify(old,raw,expected)
        if old['limits']['max_pixels']!=budget['max_pixels']:raise ValueError('Cached visual resolution is pinned; use a new resource for different pixel limits')
        if len(old['images'])>budget['max_candidates'] or old['stored_bytes']>budget['max_disk_bytes']:
            raise ValueError('Existing visual cache exceeds requested storage limits; preserve it and choose a sufficient budget')
    known={r['page']:r for r in old['images']} if old else {}
    missing=[p for p in pages if p not in known]
    if old and not missing:return old
    requested=list(dict.fromkeys((old['requested_pages'] if old else [])+pages))
    folder=raw.parent/'visuals'
    capacity=budget['max_candidates']-len(known)
    available=budget['max_disk_bytes']-(old['stored_bytes'] if old else 0)
    new=None
    if missing and capacity and available:
        staging=reserve(raw.parent/('.visual-staging-'+uuid.uuid4().hex),directory=True)
        new=render(raw,staging,missing,expected,budget|dict(max_candidates=capacity,max_disk_bytes=available))
        if old is None:reserve(folder,directory=True)
        for row in new['images']:
            target=folder/row['file'];checked(target)
            if target.exists():
                if file_hash(target)!=row['sha256']:raise ValueError('Existing cached image changed')
            else:
                reserve(target)
                shutil.copyfile(staging/row['file'],target)
                target.chmod(target.stat().st_mode & ~0o222)
            known[row['page']]=row|dict(path=str(target.resolve()))
    if old is None and not folder.exists():reserve(folder,directory=True)
    unique={r['file'] for r in known.values()}
    value=dict(schema='pdf_visuals_v1',folder=str(folder),source_sha256=expected,
        requested_pages=requested,images=list(known.values()),omitted_pages=[p for p in requested if p not in known],
        stored_bytes=sum((folder/name).stat().st_size for name in unique),limits=budget,
        renderer=(new or old or {}).get('renderer','not_rendered'))
    if file_hash(raw)!=expected:raise ValueError('PDF source changed during rendering')
    return verify(value,raw,expected)


def images(context):
    bundle=context.get('visual_bundle')
    if not bundle:return []
    verify(bundle,context['raw_file'],context['source_sha256'])
    pages=context.get('extraction_quality',{}).get('pages',[]);rows=[]
    for image in bundle['images']:
        if image['page'] not in pages:continue
        path=safe_asset(Path(bundle['folder']),image['file'])
        rows.append(dict(id='page-'+str(image['page']),kind='pdf_page',
            source_id=context['resource_id'],source_sha256=bundle['source_sha256'],
            sha256=image['sha256'],path=str(path),locator=dict(page=image['page']),
            links=dict(start_line=context['start_line'],end_line=context['end_line']),
            uncertainties=['bounded_page_preview_not_semantic_verification']))
    return rows


def worker(raw,folder,expected,pages,budget):
    import pypdfium2 as pdfium
    raw=Path(raw);folder=Path(folder)
    if raw.stat().st_size>32*1024*1024 or file_hash(raw)!=expected:raise ValueError('PDF input changed')
    rows=[];omitted=[];total=0;images_by_hash={}
    with raw.open('rb') as stream,pdfium.PdfDocument(stream) as doc:
        if len(doc)>200 or pdfium.raw.FPDF_GetSecurityHandlerRevision(doc)>=0:raise ValueError('Oversized or encrypted PDF')
        if any(p>len(doc) for p in pages):raise ValueError('Requested page does not exist')
        for page_number in pages:
            if len(rows)>=budget['max_candidates']:
                omitted.append(page_number);continue
            page=doc[page_number-1]
            try:
                w,h=page.get_size()
                if not all(math.isfinite(x) and 0<x<=4000 for x in (w,h)):raise ValueError('Invalid PDF page dimensions')
                scale=min(1.25,1600/max(w,h),math.sqrt(budget['max_pixels']/(w*h))*.99)
                bitmap=page.render(scale=scale)
                try:
                    image=bitmap.to_pil()
                    try:
                        if image.width*image.height>budget['max_pixels']:raise ValueError('PDF image pixel limit')
                        output=io.BytesIO();image.save(output,format='PNG');content=output.getvalue()
                    finally:image.close()
                finally:bitmap.close()
            finally:page.close()
            checksum=hashlib.sha256(content).hexdigest()
            if checksum not in images_by_hash:
                if total+len(content)>budget['max_disk_bytes']:
                    omitted.append(page_number);continue
                name=f'image-{checksum}.png';target=folder/name
                with target.open('xb') as image_file:image_file.write(content)
                target.chmod(target.stat().st_mode & ~0o222)
                images_by_hash[checksum]=name;total+=len(content)
            rows.append(dict(page=page_number,file=images_by_hash[checksum],sha256=checksum))
    if file_hash(raw)!=expected:raise ValueError('PDF source changed during rendering')
    return dict(schema='pdf_visuals_v1',source_sha256=expected,requested_pages=pages,
                images=rows,omitted_pages=omitted,stored_bytes=total,limits=budget,
                renderer='pypdfium2 '+str(pdfium.PYPDFIUM_INFO))


if __name__=='__main__':
    try:
        from pdf_limits import apply,check_memory
        job=apply()
        value=worker(sys.argv[1],sys.argv[2],sys.argv[3],json.loads(sys.argv[4]),json.loads(sys.argv[5]))
        check_memory();print(json.dumps(value))
    except Exception as error:
        print('PDF visual worker rejected input: '+type(error).__name__,file=sys.stderr);sys.exit(1)
