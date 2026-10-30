"""Bounded PDF text-layer extraction in an owned worker; never perform OCR or execute PDF actions."""
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys

MAX_INPUT=32*1024*1024
MAX_OUTPUT=8*1024*1024
MAX_PAGES=200
WALL_SECONDS=45


def checksum(stream):
    stream.seek(0);value=hashlib.sha256()
    for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    stream.seek(0);return value.hexdigest()


def extract(path):
    from preparation_control import run
    from profile_storage import checked
    from source_integrity import file_hash
    path=Path(path).absolute()
    for parent in reversed(path.parents):checked(parent,True)
    checked(path)
    if not path.is_file() or path.stat().st_size>MAX_INPUT:raise ValueError('PDF must be an ordinary file up to 32 MiB')
    identity=file_hash(path)
    try:
        result=run([sys.executable,'-B',str(Path(__file__).resolve()),str(path),identity],
                   capture_output=True,timeout=WALL_SECONDS,encoding='utf-8')
    except subprocess.TimeoutExpired as error:raise ValueError('PDF extraction timed out; no partial result accepted') from error
    except RuntimeError as error:raise ValueError('PDF worker output/termination failed; no partial result accepted') from error
    if result.returncode:
        if 'ModuleNotFoundError' in (result.stderr or ''):raise ValueError('PDF dependency unavailable; update the managed runtime dependencies')
        raise ValueError('PDF extraction failed: invalid, encrypted, unsupported or over resource limits; no partial result accepted')
    try:
        value=json.loads(result.stdout)
        if (set(value)!={'source_sha256','text'} or value['source_sha256']!=identity
                or not isinstance(value['text'],str) or not value['text'].strip()
                or len(value['text'].encode('utf-8'))>MAX_OUTPUT):raise ValueError('Invalid PDF worker result')
    except (ValueError,TypeError) as error:raise ValueError('Invalid PDF worker result; no partial result accepted') from error
    if file_hash(path)!=identity:raise ValueError('PDF source changed during extraction')
    return value['text'],'pdf_text_layer_v1'


def read_pdf(path,expected):
    import pdfplumber
    from pdfminer.pdfpage import PDFPage
    output=[];size=0
    def emit(value):
        nonlocal size
        size+=len(value.encode('utf-8'))
        if size>MAX_OUTPUT:raise ValueError('PDF extracted output exceeds 8 MiB')
        output.append(value)
    with path.open('rb') as stream:
        import os
        if os.fstat(stream.fileno()).st_size>MAX_INPUT:raise ValueError('PDF input exceeds 32 MiB')
        if checksum(stream)!=expected:raise ValueError('PDF source changed before parsing')
        pdf=pdfplumber.open(stream)
        try:
            if pdf.doc.encryption:raise ValueError('Encrypted PDF is unsupported')
            emit('[PDF source sha256: '+expected+'; pdfplumber '+pdfplumber.__version__+'; text layer only; verify layout against original]\n')
            count=0
            # Avoid pdf.pages: it eagerly constructs all pages before the limit is checked.
            for number,raw_page in enumerate(PDFPage.create_pages(pdf.doc),1):
                if number>MAX_PAGES:raise ValueError('PDF page limit exceeded (200)')
                page=pdfplumber.page.Page(pdf,raw_page,page_number=number)
                try:
                    count=number
                    if not 0<page.width<=2000 or not 0<page.height<=4000:raise ValueError('PDF page dimensions exceed layout limit')
                    chars=page.chars
                    if len(chars)>50000 or sum(len(page.objects.get(key,[])) for key in ('line','rect','curve'))>2000:
                        raise ValueError('PDF page complexity exceeds limit')
                    emit(f'\n[PDF page {number}; spatial layout; reading order requires review]\n')
                    if not chars:
                        state='requires_ocr' if page.images else 'no_text_layer; blank or graphics; visual review required'
                        emit('['+state+']\n')
                    else:
                        mono=[c['x1']-c['x0'] for c in chars if any(s in c.get('fontname','').lower() for s in ('courier','mono')) and c['x1']>c['x0']]
                        density=statistics.median(mono) if mono else 7.25
                        text=page.extract_text(layout=True,x_density=max(1,density)) or ''
                        emit('\n'.join(line.rstrip() for line in text.splitlines()).strip('\n')+'\n')
                        if '(cid:' in text or '\ufffd' in text:emit('[unmapped_glyphs: review original; text may be incomplete]\n')
                        if page.images:emit('[images not interpreted; review original]\n')
                        tables=page.find_tables()
                        if len(tables)>30:raise ValueError('PDF table count exceeds limit')
                        for index,table in enumerate(tables,1):
                            rows=table.extract()
                            if len(rows)>500 or any(len(row)>50 for row in rows):raise ValueError('PDF table size exceeds limit')
                            emit(f'[PDF page {number} table {index}; bbox={table.bbox}; headers unverified; supplemental to layout text]\n')
                            for row in rows:
                                emit('\t'.join('[missing/merged: review]' if cell is None else json.dumps(cell,ensure_ascii=False)[1:-1] for cell in row)+'\n')
                            emit('[/table]\n')
                    emit(f'[end PDF page {number}]\n')
                finally:page.close()
            if not count:emit('[PDF has no pages; review required]\n')
        finally:
            # pdf.close() materializes pdf.pages. We own the stream and already close each page.
            pdf.flush_cache()
        if checksum(stream)!=expected:raise ValueError('PDF source changed during parsing')
    return dict(source_sha256=expected,text=''.join(output))


if __name__=='__main__':
    try:
        from pdf_limits import apply,check_memory,limit_streams
        job=apply()
        limit_streams()
        if len(sys.argv)!=3:raise ValueError('Invalid PDF worker arguments')
        result=read_pdf(Path(sys.argv[1]),sys.argv[2])
        check_memory()  # Reject a fast peak even if the RSS watchdog has not ticked yet.
        sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf-8'))
    except Exception as error:
        print('PDF worker rejected input: '+type(error).__name__,file=sys.stderr)
        sys.exit(1)
