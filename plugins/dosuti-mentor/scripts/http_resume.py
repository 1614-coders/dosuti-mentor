"""Resume HTTP media only with a strong validator and a verified local prefix."""
import json
from pathlib import Path
import re
import subprocess
from atomic_files import atomic_json
from source_integrity import file_hash


def response_headers(path):
    blocks = re.split(r'\r?\n\r?\n', path.read_text(errors='replace'))
    responses = [b for b in blocks if b.startswith('HTTP/')]
    if not responses:
        raise ValueError('Missing HTTP response headers')
    lines = responses[-1].splitlines()
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            key = key.lower()
            # Only these fields participate in validation. Repeated Set-Cookie,
            # Link, etc. are legal and must not hide the actual HTTP status.
            if key not in {'content-length', 'content-range', 'content-encoding', 'etag'}:
                continue
            if key in headers and headers[key] != value.strip():
                raise ValueError('Conflicting HTTP response headers')
            headers[key] = value.strip()
    return status, headers


def download(url, target, duration=1800):
    target = Path(target)
    part = target.with_name(target.name+'.part')
    receipt = target.with_name(target.name+'.resume.json')
    body = target.with_name(target.name+'.response')
    headers = target.with_name(target.name+'.headers')
    for path in (target, part, receipt, body, headers):
        if path.is_symlink():
            raise ValueError('Download paths must not be symbolic links')
    state = None
    if part.exists() and receipt.exists():
        try:
            candidate = json.loads(receipt.read_text())
            if (candidate['url'] == url and candidate['size'] == part.stat().st_size
                    and candidate['sha256'] == file_hash(part)
                    and 0 < candidate['size'] < candidate['total']
                    and re.fullmatch(r'"[^"\r\n]+"', candidate['etag'])):
                state = candidate
        except (ValueError, KeyError, TypeError):
            pass
    if state is None:
        part.unlink(missing_ok=True); receipt.unlink(missing_ok=True)
    offset = state['size'] if state else 0
    cmd = ['curl', '--location', '--fail', '--connect-timeout', '20', '--max-time', str(duration),
           '--proto', '=http,https', '--proto-redir', '=http,https', '--silent', '--show-error',
           '--header', 'Accept-Encoding: identity', '--dump-header', str(headers), '--output', str(body)]
    if state:
        cmd += ['--range', f'{offset}-', '--header', 'If-Range: '+state['etag']]
    try:
        result = subprocess.run(cmd+['--', url], capture_output=True, timeout=duration+30)
        status, values = response_headers(headers)
        if status not in (200, 206):
            raise ValueError(f'HTTP {status}: video was not downloaded; partial download was not appended')
        if values.get('content-encoding', 'identity') != 'identity':
            raise ValueError('Unsupported HTTP response; partial download was not appended')
        etag = values.get('etag', '')
        size = body.stat().st_size
        if status == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', values.get('content-range', ''))
            if not state or not match:
                raise ValueError('Unexpected HTTP range response')
            start, end, total = map(int, match.groups())
            if start != offset or not start <= end < total or total != state['total'] or etag != state['etag']:
                raise ValueError('HTTP range or source validator changed; partial download was not appended')
            expected = end-start+1
            if 'content-length' in values and int(values['content-length']) != expected:
                raise ValueError('HTTP range length mismatch')
        else:
            # Range ignored or If-Range failed: this is a new full representation.
            offset = 0
            expected = int(values['content-length']) if 'content-length' in values else None
            total = expected
        if expected is not None and (size > expected or (result.returncode == 0 and size != expected)):
            raise ValueError('HTTP response length mismatch')
        if result.returncode not in (0, 18, 28, 56):
            raise RuntimeError('Video transfer failed; rerun when connection is available')
        if offset:
            with part.open('ab') as dest, body.open('rb') as source:
                import shutil
                shutil.copyfileobj(source, dest)
        else:
            body.replace(part)
        actual = part.stat().st_size
        if result.returncode == 0 and (total is None or actual == total):
            part.replace(target); receipt.unlink(missing_ok=True)
            return
        if total is not None and 0 < actual < total and re.fullmatch(r'"[^"\r\n]+"', etag):
            atomic_json(receipt, dict(url=url, etag=etag, total=total, size=actual, sha256=file_hash(part)))
        else:
            part.unlink(missing_ok=True); receipt.unlink(missing_ok=True)
        raise RuntimeError('Video transfer incomplete; rerun to resume a verified prefix when supported')
    finally:
        body.unlink(missing_ok=True); headers.unlink(missing_ok=True)
