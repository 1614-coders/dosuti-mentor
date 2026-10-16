"""Download and repair complete Systran models with bounded, resumable transfers."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from atomic_files import atomic_json
from model_readiness import FILES, ModelNotReady, inspect_model, model_lock
from source_integrity import file_hash


def curl(url, dest, headers=None, byte_range=None):
    command = ['curl', '-sS', '--fail', '--connect-timeout', '10', '--max-time', '30',
               '--proto', '=https', '--proto-redir', '=https']
    if headers:
        command += ['-D', str(headers)]
    command += ['--range', byte_range] if byte_range else ['-L']
    return subprocess.run(command + [url, '-o', str(dest)], capture_output=True)


def metadata(model, dest):
    base = f'https://huggingface.co/Systran/faster-whisper-{model}/resolve/'
    token = uuid.uuid4().hex
    header, body = dest / (token + '.headers'), dest / (token + '.redirect')
    try:
        response = subprocess.run(['curl', '-sS', '--fail', '--max-time', '30',
            '--proto', '=https', '-D', str(header), '-o', str(body),
            base + f'main/model.bin?fresh={time.time_ns()}'], capture_output=True)
        if response.returncode:
            raise RuntimeError('Could not obtain model metadata; rerun when connection is available')
        values = {}
        for line in header.read_text().splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                values[key.lower()] = value.strip()
        url = values.get('location', '')
        checksum = values.get('x-linked-etag', '').strip('"')
        revision = values.get('x-repo-commit', '')
        size = int(values.get('x-linked-size', '0'))
        if not url.startswith('https://') or not re.fullmatch('[0-9a-f]{64}', checksum) or size <= 0:
            raise RuntimeError('Missing secure redirect, size or SHA256 for weights')
        if not re.fullmatch('[0-9a-f]{40}', revision):
            raise RuntimeError('Model revision is unavailable; no mixed-version model will be published')
        return dict(url=url, sha256=checksum, size=size, revision=revision,
                    base=base + revision + '/')
    finally:
        header.unlink(missing_ok=True)
        body.unlink(missing_ok=True)


def cleanup(dest):
    parts = dest / '.parts'
    if parts.exists() and not parts.is_symlink():
        shutil.rmtree(parts)
    for name in ['download.headers', 'redirect.txt', 'model.bin.tmp']:
        (dest / name).unlink(missing_ok=True)


def download(model, output):
    dest = Path(output).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with model_lock(dest):
        for folder in dest.glob('.bundle-*'):
            if re.fullmatch(r'\.bundle-[0-9a-f]{32}', folder.name) and folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)
        try:
            ready = inspect_model(dest)
        except ModelNotReady:
            ready = None
        if ready:
            atomic_json(dest / 'model-ready.json', ready)
            (dest / '.model-installing.json').unlink(missing_ok=True)
            cleanup(dest)
            print(f'Complete local model verified; no download needed: {dest}', flush=True)
            return ready
        if not shutil.which('curl'):
            raise RuntimeError('curl is required to repair this incomplete model')
        info = metadata(model, dest)
        parts = dest / '.parts' / info['sha256']
        parts.mkdir(parents=True, exist_ok=True)
        chunk = 2 * 1024 * 1024
        count = (info['size'] + chunk - 1) // chunk

        def fetch(i):
            start, end = i * chunk, min(info['size'] - 1, (i + 1) * chunk - 1)
            part, marker = parts / f'{i:04d}.part', parts / f'{i:04d}.json'
            if part.exists():
                try:
                    saved = json.loads(marker.read_text())
                    valid = (saved == dict(size=part.stat().st_size, sha256=file_hash(part))
                             and part.stat().st_size <= end - start + 1)
                except (OSError, ValueError):
                    valid = False
                if not valid:
                    part.unlink()
                    marker.unlink(missing_ok=True)
            failures = 0
            while (part.stat().st_size if part.exists() else 0) < end - start + 1:
                have = part.stat().st_size if part.exists() else 0
                first, last = start + have, min(end, start + have + 256 * 1024 - 1)
                token = uuid.uuid4().hex
                temporary, header = parts / (token + '.tmp'), parts / (token + '.headers')
                try:
                    result = curl(info['url'], temporary, header, f'{first}-{last}')
                    response_headers = header.read_text().lower() if header.exists() else ''
                    match = re.search(r'(?m)^content-range:\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$', response_headers)
                    correct = (match and tuple(map(int, match.groups())) == (first, last, info['size'])
                               and temporary.is_file() and 0 < temporary.stat().st_size <= last - first + 1)
                    if correct:
                        with part.open('ab') as stream:
                            stream.write(temporary.read_bytes())
                            stream.flush()
                            __import__('os').fsync(stream.fileno())
                        atomic_json(marker, dict(size=part.stat().st_size, sha256=file_hash(part)))
                        failures = 0
                    else:
                        failures += 1
                        if result.returncode == 0:
                            raise RuntimeError('Unexpected HTTP range response; cached bytes were not appended')
                        if failures >= 5:
                            raise RuntimeError(f'Model part {i} interrupted; rerun to resume verified parts')
                        time.sleep(0.2)
                finally:
                    temporary.unlink(missing_ok=True)
                    header.unlink(missing_ok=True)

        staging = dest / ('.bundle-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            existing = dest / 'model.bin'
            if existing.is_file() and existing.stat().st_size == info['size'] and file_hash(existing) == info['sha256']:
                shutil.copyfile(existing, staging / 'model.bin')
            else:
                for attempt in range(2):
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        futures = [pool.submit(fetch, i) for i in range(count)]
                        for done, future in enumerate(as_completed(futures), 1):
                            future.result()
                            print(f'Model parts verified locally: {done}/{count}', flush=True)
                    with (staging / 'model.bin').open('wb') as stream:
                        for i in range(count):
                            with (parts / f'{i:04d}.part').open('rb') as source:
                                shutil.copyfileobj(source, stream)
                    if file_hash(staging / 'model.bin') == info['sha256']:
                        break
                    shutil.rmtree(parts)
                    parts.mkdir()
                    if attempt:
                        raise RuntimeError('Model checksum still fails after a clean retry; no model activated')
                    print('Discarded corrupt part cache; retrying weights once', flush=True)
            for name in FILES[1:]:
                result = curl(info['base'] + name, staging / name)
                if result.returncode:
                    raise RuntimeError(f'Download interrupted: {name}; verified weight parts remain for retry')
            origin = dict(repository=info['base'], sha256=info['sha256'], size=info['size'], revision=info['revision'])
            atomic_json(staging / 'download.json', origin)
            ready = inspect_model(staging)
            # Cooperating loaders hold the same lock. The readiness marker is last.
            atomic_json(dest / '.model-installing.json', ready)
            (dest / 'model-ready.json').unlink(missing_ok=True)
            for name in [*FILES, 'download.json']:
                (staging / name).replace(dest / name)
            atomic_json(dest / 'model-ready.json', ready)
            (dest / '.model-installing.json').unlink(missing_ok=True)
            cleanup(dest)
            print(f'Complete model ready; weights SHA256 verified: {dest}', flush=True)
            return ready
        finally:
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', choices=['tiny', 'base', 'small', 'medium', 'large-v3'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        download(args.model, args.output)
    except (RuntimeError, ValueError, OSError) as error:
        parser.exit(1, f'Model not ready: {error}\n')


if __name__ == '__main__':
    main()
