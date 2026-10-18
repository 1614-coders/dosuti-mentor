"""Owned storage, private subprocess caches and cleanup while all workers are idle."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import uuid
from atomic_files import atomic_json
from storage_lock import file_lock

MARKER = '.dosuti-home.json'
_ACTIVE = ContextVar('dosuti_storage', default=None)
_ENV_LOCK = threading.RLock()


def safe(path):
    path = Path(path).expanduser().absolute()
    for p in (path, *path.parents):
        if p.is_symlink() or (p.exists() and getattr(p.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Managed storage cannot use links or reparse points')
    return path.resolve()


def validate(home):
    home = safe(home)
    from course_lifecycle import active
    active(home)
    marker = safe(home / MARKER)
    if marker.stat().st_size > 4096:
        raise ValueError('Invalid managed home marker')
    value = json.loads(marker.read_text(encoding='utf-8-sig'))
    if (not isinstance(value, dict) or value.get('schema') != 'dosuti_home_v1'
            or not isinstance(value.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', value['id'])):
        raise ValueError('Invalid managed home marker')
    return home


def initialize(home, legacy_runtime=False):
    home = safe(home)
    if home == Path(home.anchor) or home == Path.home():
        raise ValueError('Choose a dedicated data folder')
    if (home / MARKER).exists():
        return validate(home)
    if home.exists() and any(home.iterdir()):
        if not (legacy_runtime and (home / 'venv/pyvenv.cfg').is_file()):
            raise ValueError('Existing folder is not managed; choose an empty folder')
    home.mkdir(parents=True, exist_ok=True)
    atomic_json(home / MARKER, dict(schema='dosuti_home_v1', id=uuid.uuid4().hex))
    return home


def find_home(scope):
    if os.environ.get('DOSUTI_HOME'):
        return validate(os.environ['DOSUTI_HOME'])
    path = Path(scope).expanduser().absolute()
    for p in (path, *path.parents):
        if (p / MARKER).exists():
            return validate(p)
    return None


def environment(home, temporary=None):
    home = validate(home)
    locations = dict(DOSUTI_HOME=home, PIP_CACHE_DIR=home / 'cache/pip',
                     HF_HOME=home / 'cache/huggingface', XDG_CACHE_HOME=home / 'cache',
                     IMAGEIO_USERDIR=home / 'cache/imageio')
    for p in (*locations.values(), home / 'temp'):
        safe(p).mkdir(parents=True, exist_ok=True)
    temporary = safe(temporary or home / 'temp')
    if not temporary.is_relative_to(home / 'temp'):
        raise ValueError('Temporary folder escapes managed home')
    locations.update(TEMP=temporary, TMP=temporary, TMPDIR=temporary)
    return dict({k: str(v) for k, v in locations.items()}, PYTHONDONTWRITEBYTECODE='1',
                PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1', HF_HUB_DISABLE_TELEMETRY='1')


@contextmanager
def session(home):
    if home is None or _ACTIVE.get() is not None:
        yield; return
    with _ENV_LOCK:
        with _session(home): yield


@contextmanager
def _session(home):
    if home is None or _ACTIVE.get() is not None:
        yield; return
    home = validate(home)
    env = environment(home)
    with file_lock(home / '.storage.lock'):
        temporary = safe(home / 'temp' / ('job-' + uuid.uuid4().hex))
        temporary.mkdir()
        lease = file_lock(temporary / '.lease.lock', timeout=0)
        lease.__enter__()
    env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
    old = {key: os.environ.get(key) for key in env}
    old_temp = tempfile.tempdir
    token = _ACTIVE.set(home)
    try:
        os.environ.update(env); tempfile.tempdir = None
        yield
    finally:
        for key, value in old.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        tempfile.tempdir = old_temp
        _ACTIVE.reset(token)
        with file_lock(home / '.storage.lock'):
            lease.__exit__(None, None, None)
            # Unique lease names are never reused; no waiter exists under the storage lock.
            shutil.rmtree(temporary)


def managed_operation(function):
    @wraps(function)
    def wrapped(args, *a, **kw):
        scope = next((getattr(args, name, None) for name in ('course', 'output', 'runtime')
                      if getattr(args, name, None) is not None), Path.cwd())
        home = find_home(scope)
        if home is None:
            return function(args, *a, **kw)
        with _ENV_LOCK:
            with session(home):
                result = function(args, *a, **kw)
        if home is not None:
            try: maintain(home, apply=True)
            except (OSError, ValueError) as error:
                print('Cache cleanup deferred: ' + str(error), file=sys.stderr)
        return result
    return wrapped


def files(folder):
    if not folder.exists(): return []
    result = []
    for path in safe(folder).iterdir():
        path = safe(path)
        if path.is_dir(): result.extend(files(path))
        elif path.is_file() and path.stat().st_nlink == 1: result.append(path)
        else: raise ValueError('Unsupported managed storage entry')
    return result


def usage(home):
    home = validate(home)
    totals = {}; skipped = 0; count = 0
    def visit(folder, group):
        nonlocal skipped, count
        for path in folder.iterdir():
            count += 1
            if count > 200000: raise ValueError('Storage inventory limit exceeded')
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                skipped += 1
            elif path.is_dir(): visit(path, group)
            elif path.is_file(): totals[group] = totals.get(group, 0) + info.st_size
    for path in home.iterdir():
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            skipped += 1
        elif path.is_dir(): visit(path, path.name)
        elif path.is_file(): totals['_root'] = totals.get('_root', 0) + info.st_size
    return dict(home=str(home), logical_bytes_by_folder=totals, links_skipped=skipped,
                total_logical_bytes=sum(totals.values()), includes_shared_external_files=False)


def maintain(home, apply=False, max_cache_bytes=1024**3):
    if type(max_cache_bytes) is not int or max_cache_bytes < 0:
        raise ValueError('Cache budget must be nonnegative')
    home = validate(home)
    with file_lock(home / '.storage.lock'), ExitStack() as locks:
        # Bootstrap uses a separate OS lock while Python is not yet available.
        locks.enter_context(file_lock(safe(home / '.bootstrap.lock'), timeout=0))
        stale = []
        for folder in safe(home / 'temp').iterdir() if (home / 'temp').exists() else ():
            if re.fullmatch('job-[a-f0-9]{32}', folder.name) and safe(folder).is_dir() and (folder / '.lease.lock').is_file():
                locks.enter_context(file_lock(safe(folder / '.lease.lock'), timeout=0))
                files(folder)  # Reject links/hard links before selecting any deletion.
                stale.append(folder)
        cached = files(safe(home / 'cache'))
        size = sum(p.stat().st_size for p in cached)
        selected, remaining = [], size
        for path in sorted(cached, key=lambda p: p.stat().st_mtime_ns):
            if remaining <= max_cache_bytes: break
            selected.append(path); remaining -= path.stat().st_size
        result = dict(home=str(home), cache_bytes=size, cache_after_bytes=remaining,
                      delete_cache_files=[str(p.relative_to(home)) for p in selected],
                      abandoned_temp=[str(p.relative_to(home)) for p in stale], applied=apply)
        if apply:
            for path in selected: path.unlink()
        # Close per-folder locks before Windows directory deletion; storage lock prevents new leases.
    if apply:
        with file_lock(home / '.storage.lock'):
            for folder in stale:
                # Unique job IDs are not reused by workers.
                shutil.rmtree(folder)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--cache-mib', type=int, default=1024)
    parser.add_argument('--usage', action='store_true')
    options = parser.parse_args()
    try:
        value = usage(options.home) if options.usage else maintain(options.home, options.apply, options.cache_mib * 1024**2)
        print(json.dumps(value, indent=2))
    except (OSError, ValueError, TimeoutError) as error:
        parser.exit(1, str(error) + '\n')
