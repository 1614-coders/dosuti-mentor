"""Verified offline cache with adapters for public video sources."""
import json
import os
import hashlib
from contextlib import nullcontext
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, quote
import uuid
import tempfile
from atomic_files import atomic_json
from download_diagnostics import record as record_download_status
from source_integrity import digest, file_hash, safe_asset
from storage_lock import file_lock
from storage_paths import source_kind, local_path
from preparation_control import run as preparation_run
from download_space import ensure as ensure_download_space

MEDIA_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.mpeg', '.mpg', '.ts'}


def validate_media(path):
    import imageio_ffmpeg
    # A login/error HTML page must never become a successful download.
    result = preparation_run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
        '-i', str(path), '-map', '0:v:0', '-frames:v', '1', '-f', 'null', '-'],
        capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('Source did not provide a readable video; supply a local file or accessible video URL')


def check_hash(path, expected):
    checksum = file_hash(path)
    if expected and checksum != digest(expected, 'Registered video SHA256'):
        raise ValueError('Video changed since registration; register it as a new lesson')
    return checksum


def verification_paths(directory, video):
    # Windows callers require a final full rehash, not just these identities.
    paths = [p.resolve() for p in (Path(directory) / 'video').glob('*') if p.is_file()]
    if source_kind(video['source']) == 'local':
        paths.append(local_path(video['source']))
    if video.get('path'):
        paths.append(local_path(video['path']))
    # New downloads/staging paths are deliberately outside this read-only cache.
    return paths


def cached(assets, video):
    # New cache receipt is tied to exact original URL, not an expiring CDN URL.
    marker = assets / 'download.json'
    if marker.exists():
        try:
            receipt = json.loads(marker.read_text())
        except (OSError, ValueError) as error:
            raise ValueError('Video cache receipt is unreadable; keep it for diagnosis and restore a trusted copy') from error
        if receipt.get('source') != video['source']:
            raise ValueError('Video cache belongs to another source')
        name = receipt.get('file')
        if isinstance(name, str) and not Path(name).is_absolute():
            candidate = (assets / name).resolve()
            if candidate.is_relative_to(assets.resolve()) and not candidate.exists():
                if video.get('sha256') == digest(receipt.get('sha256')):
                    return None  # Re-fetch only the already pinned content.
        path = safe_asset(assets, receipt.get('file'))
        checksum = file_hash(path)
        if path.stat().st_size != receipt.get('size') or checksum != digest(receipt.get('sha256')):
            raise ValueError('Video cache integrity changed; restore a trusted copy or register a new lesson')
        if video.get('sha256') and checksum != digest(video['sha256'], 'Registered video SHA256'):
            raise ValueError('Video changed since registration; register it as a new lesson')
        return path
    # Adopt v0.1.4 caches only when the lesson already pinned their content.
    if video.get('sha256'):
        if video.get('path'):
            path = local_path(video['path'])
            if path.is_file():
                check_hash(path, video['sha256'])
                return path
        files = [p for p in assets.glob('lecture.*') if p.suffix.lower() in MEDIA_EXTENSIONS]
        for path in files:
            if file_hash(path) == digest(video['sha256']):
                return path.resolve()
        if files:
            raise ValueError('Video cache does not match the registered hash')
    return None


def curl(url, target, duration=1800, *, known_size=None):
    if duration > 30:
        from http_resume import download
        return download(url, target, duration, known_size=known_size)
    preparation_run(['curl', '--location', '--fail', '--retry', '2', '--connect-timeout', '20',
        '--max-time', str(duration), '--proto', '=http,https', '--proto-redir', '=http,https',
        '--silent', '--show-error', '--output', str(target), '--', url], check=True, timeout=duration+30)


def fetch_yandex(source, staging):
    metadata_file = staging / 'metadata.json'
    curl('https://cloud-api.yandex.net/v1/disk/public/resources?public_key=' + quote(source, safe=''), metadata_file, 30)
    metadata = json.loads(metadata_file.read_text())
    url = metadata.get('file')
    if metadata.get('type') != 'file' or not isinstance(url, str) or source_kind(url) != 'remote':
        raise ValueError('Yandex URL must point to a downloadable public video file')
    expected = digest(metadata.get('sha256'), 'Yandex video SHA256')
    size = metadata.get('size')
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError('Yandex video size is invalid')
    destination = staging / 'lecture.mp4'
    ensure_download_space(staging, size)
    curl(url, destination, known_size=size)
    if destination.stat().st_size != size or file_hash(destination) != expected:
        raise ValueError('Downloaded video failed integrity check')
    return destination, 'yandex'


def fetch_direct(source, staging):
    suffix = Path(urlsplit(source).path).suffix.lower()
    destination = staging / ('lecture' + suffix)
    curl(source, destination)
    return destination, 'direct'


def site_options(fragments=4, timeout=1800):
    if type(fragments) is not int or not 1 <= fragments <= 8:
        raise ValueError('Download fragments must be an integer from 1 to 8')
    if type(timeout) is not int or not 120 <= timeout <= 7200:
        raise ValueError('Site timeout must be an integer from 120 to 7200 seconds')
    return fragments, timeout


def download_failure(error, detail):
    text = detail.casefold()
    if isinstance(error, subprocess.TimeoutExpired):
        return 'timeout', 'Site download timed out; increase --site-timeout or supply a local video file'
    if any(word in text for word in ('sign in', 'login', 'private video', 'cookies', 'confirm you', '403', '429')):
        return 'access_required', 'Site requires login or limits access; supply a local video file or accessible public link'
    if any(word in text for word in ('resolve', 'network is unreachable', 'connection', 'timed out')):
        return 'network', 'Site network request failed; check the connection and retry or supply a local video file'
    if 'unsupported url' in text:
        return 'unsupported', 'Site URL is unsupported; supply a direct video URL or local video file'
    return 'site_failed', 'Site download failed; it may require login, be unavailable or unsupported. Supply a local video file'


def run_site_download(command, timeout):
    # Capture diagnostics on disk; progress still uses yt-dlp stdout.
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as errors:
        try:
            result = preparation_run(command, check=True, timeout=timeout, stderr=errors)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            errors.seek(0, 2); size = errors.tell(); errors.seek(max(0, size-32768))
            error.stderr = errors.read()
            raise
        errors.seek(0)
        for line in errors:
            print(line, end='', file=sys.stderr)
        return result


def fetch_site(source, staging, fragments=4, timeout=1800, quality='best'):
    from download_planning import quality_format
    fmt = quality_format(quality)
    fragments, timeout = site_options(fragments, timeout)
    output = staging / 'completed.json'
    common = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
              '--no-plugin-dirs', '--socket-timeout', '30', '--retries', '3']
    if os.environ.get('DOSUTI_HOME'):
        from managed_storage import validate
        common += ['--cache-dir', str(validate(os.environ['DOSUTI_HOME']) / 'cache/yt-dlp')]
    try:
        probe = preparation_run(common + ['--playlist-end', '2', '--format', fmt,
            '--dump-single-json', '--skip-download', '--', source],
            check=True, capture_output=True, text=True, timeout=120)
        info = json.loads(probe.stdout)
        if not isinstance(info, dict) or info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
            raise ValueError('Use a recorded single video, not a live stream')
        # A generic page with one embedded video can also have playlist type.
        if info.get('_type') in ('playlist', 'multi_video'):
            entries = info.get('entries')
            if not isinstance(entries, list) or len(entries) != 1 or info.get('playlist_count', 1) != 1:
                raise ValueError('Provide one lecture URL, not a playlist or page with multiple videos')
        from download_planning import estimate
        selected = info['entries'][0] if info.get('_type') in ('playlist', 'multi_video') else info
        size, kind = estimate(selected if isinstance(selected, dict) else {})
        ensure_download_space(staging, size, merge=True)
        import imageio_ffmpeg
        run_site_download(common + ['--newline', '--progress', '--progress-delta', '5',
            '--concurrent-fragments', str(fragments), '--abort-on-unavailable-fragments', '--playlist-items', '1',
            '--format', fmt, '--fragment-retries', '3', '--match-filters', '!is_live',
            '--ffmpeg-location', imageio_ffmpeg.get_ffmpeg_exe(), '--merge-output-format', 'mkv',
            '--print-to-file', 'after_move:%(filepath)j', str(output),
            '-o', str(staging / 'lecture.%(ext)s'), '--', source], timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = getattr(error, 'stderr', '') or ''
        if isinstance(detail, bytes): detail = detail.decode('utf-8', errors='replace')
        category, message = download_failure(error, detail)
        record_download_status(staging.parent, source, status='failed', adapter='yt-dlp',
            category=category, message=message, fragments=fragments, timeout=timeout)
        raise ValueError(message) from error
    records = output.read_text().splitlines() if output.exists() else []
    if len(records) != 1:
        raise ValueError('URL must yield one completed video, not a playlist or live stream')
    name = json.loads(records[0])
    original = Path(name) if isinstance(name, str) else None
    path = original.resolve() if original else None
    if path is None or not path.is_relative_to(staging.resolve()) or not path.is_file() or original.is_symlink():
        raise ValueError('Downloader did not produce one completed video')
    return path, 'yt-dlp'


def acquire(directory, video, fragments=4, site_timeout=1800, quality='best'):
    from download_planning import quality_format
    quality_format(quality)
    site_options(fragments, site_timeout)
    source = video['source']
    if source_kind(source) == 'local':
        path = local_path(source)
        if not path.is_file():
            raise ValueError('Local video not found; use its path on this computer')
        return path
    assets = Path(directory) / 'video'
    assets.mkdir(exist_ok=True)
    with file_lock(assets / '.download.lock'):
        ready = cached(assets, video)
        if ready:
            return ready
        parsed = urlsplit(source)
        resumable = (parsed.hostname in ('disk.yandex.ru', 'disk.yandex.com', 'yadi.sk')
                     or Path(parsed.path).suffix.lower() in MEDIA_EXTENSIONS)
        staging = assets / ('.fetch-' + (hashlib.sha256(source.encode()).hexdigest()[:24] if resumable else uuid.uuid4().hex))
        if staging.is_symlink():
            raise ValueError('Download staging must not be a symbolic link')
        staging.mkdir(exist_ok=resumable)
        complete = False
        site = not resumable
        if site:
            record_download_status(assets, source, status='running',adapter='yt-dlp',fragments=fragments,timeout=site_timeout)
        try:
            with nullcontext():
                parsed = urlsplit(source)
                if parsed.hostname in ('disk.yandex.ru', 'disk.yandex.com', 'yadi.sk'):
                    downloaded, adapter = fetch_yandex(source, staging)
                elif Path(parsed.path).suffix.lower() in MEDIA_EXTENSIONS:
                    downloaded, adapter = fetch_direct(source, staging)
                else:
                    downloaded, adapter = fetch_site(source, staging, fragments, site_timeout, quality)
            checksum = check_hash(downloaded, video.get('sha256'))
            validate_media(downloaded)
            # Unique immutable asset prevents overwriting an older cached file.
            name = 'video-' + uuid.uuid4().hex + downloaded.suffix.lower()
            target = assets / name
            downloaded.replace(target)
            atomic_json(assets / 'download.json', dict(schema_version=1, source=source,
                adapter=adapter, file=name, size=target.stat().st_size, sha256=checksum))
            complete = True
            if site:
                record_download_status(assets, source, status='ready',adapter=adapter,fragments=fragments,timeout=site_timeout)
            return target.resolve()
        except (Exception, KeyboardInterrupt) as error:
            if site:
                marker = assets / 'download-status.json'
                info = json.loads(marker.read_text())
                if info.get('status') != 'failed':
                    record_download_status(assets, source, status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',
                        adapter='yt-dlp',category='interrupted' if isinstance(error,KeyboardInterrupt) else 'validation_or_download_failed',
                        fragments=fragments,timeout=site_timeout)
            raise
        finally:
            if complete or not resumable or not any(staging.glob('*.resume.json')):
                shutil.rmtree(staging, ignore_errors=True)
