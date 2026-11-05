"""Resume HTTP media only with a strong validator and a verified local prefix."""
import json
from pathlib import Path
import re
import subprocess
from atomic_files import atomic_json
from source_integrity import file_hash
from preparation_control import run as preparation_run


def preflight(url, folder):
    """HEAD is advisory: unsupported/missing metadata must not block valid sources."""
    import tempfile
    from download_space import ensure
    size = None
    # Anonymous descriptor: no replaceable directory and no pathname cleanup.
    with tempfile.TemporaryFile() as headers:
        try:
            import os
            result = preparation_run(['curl', '--head', '--location', '--fail', '--silent', '--show-error',
                '--connect-timeout', '10', '--max-time', '20', '--proto', '=http,https',
                '--proto-redir', '=http,https', '--dump-header', '-', '--output', os.devnull, '--', url],
                stdout=headers, stderr=subprocess.PIPE, timeout=25)
            if os.fstat(headers.fileno()).st_size > 1024 * 1024:
                raise ValueError('HTTP preflight headers exceed 1 MiB')
            headers.seek(0)
            status, values = response_headers(headers)
            if result.returncode == 0 and status == 200:
                size = int(values.get('content-length', '0')) or None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    ensure(folder, size)


def response_headers(path):
    raw = path.read().decode('utf-8', errors='replace') if hasattr(path, 'read') else path.read_text(errors='replace')
    blocks = re.split(r'\r?\n\r?\n', raw)
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


def download(url, target, duration=1800, *, known_size=None):
    import os
    import tempfile
    import hashlib
    from http_download_io import DownloadIO
    from storage_lock import file_lock
    io = DownloadIO(target)
    try:
        key = hashlib.sha256(io.target.name.encode('utf-8')).hexdigest()[:24]
        with file_lock(io.target.parent / ('.http-' + key + '.lock'), directory_fd=io.fd):
            io.absent_target()
            state = io.state(url)
            offset = state['size'] if state else 0
            if state:
                from download_space import ensure
                ensure(io.target.parent, state['total'], merge=True)
            elif known_size is not None:
                from download_space import ensure
                ensure(io.target.parent, known_size, merge=True)
            else:
                preflight(url, io.target.parent)
            with tempfile.TemporaryFile() as body, tempfile.TemporaryFile() as headers:
                cmd = ['curl', '--location', '--fail', '--connect-timeout', '20', '--max-time', str(duration),
                       '--proto', '=http,https', '--proto-redir', '=http,https', '--silent', '--show-error',
                       '--header', 'Accept-Encoding: identity', '--dump-header', '/dev/fd/' + str(headers.fileno()), '--output', '-']
                if state: cmd += ['--range', f'{offset}-', '--header', 'If-Range: ' + state['etag']]
                limit = known_size
                if type(limit) is int and limit > 0: cmd += ['--max-filesize', str(limit)]
                result = preparation_run(cmd + ['--', url], stdout=body, stderr=subprocess.PIPE,
                                         pass_fds=(headers.fileno(),), timeout=duration+30)
                io.owner.guard.check()
                if os.fstat(headers.fileno()).st_size > 1024 * 1024:
                    raise ValueError('HTTP response headers exceed 1 MiB; no result accepted')
                headers.seek(0)
                status, values = response_headers(headers)
                if status not in (200, 206):
                    raise ValueError(f'HTTP {status}: video was not downloaded; partial download was not appended')
                if values.get('content-encoding', 'identity') != 'identity':
                    raise ValueError('Unsupported HTTP response; partial download was not appended')
                etag = values.get('etag', '')
                size = os.fstat(body.fileno()).st_size
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
                actual = offset + size
                if result.returncode == 0 and (total is None or actual == total):
                    from download_space import ensure
                    ensure(io.target.parent, actual)
                    retained = io.preserve
                    io.preserve = True
                    io.write(io.target.name, io.blocks(body, offset))
                    io.preserve = retained
                    return
                if total is not None and 0 < actual < total and re.fullmatch(r'"[^"\r\n]+"', etag):
                    io.partial(body, offset, url, etag, total)
                raise RuntimeError('Video transfer incomplete; rerun to resume a verified prefix when supported')
    finally:
        io.close()
