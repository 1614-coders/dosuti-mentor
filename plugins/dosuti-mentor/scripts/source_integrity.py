"""Content identity and accidental-drift detection, not authenticated provenance."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from contextvars import ContextVar
from functools import wraps

_VERIFICATION=ContextVar("lecture_verification",default=None)

def identity(path):
    value=Path(path).stat()
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)

def drift(path,before,after,stage):
    names=('device','inode','size','mtime_ns','ctime_ns')
    changes={name:dict(before=old,after=new) for name,old,new in zip(names,before,after) if old!=new}
    return ValueError('Source changed during '+stage+': '+json.dumps(dict(file=str(Path(path).absolute()),changes=changes),ensure_ascii=False))


def verification_session(function):
    """Reuse hashes only within one read operation; recheck identities before return."""
    @wraps(function)
    def wrapped(*args,**kwargs):
        if _VERIFICATION.get() is not None:return function(*args,**kwargs)
        cache={};token=_VERIFICATION.set(cache)
        try:
            result=function(*args,**kwargs)
            for name,(before,_) in cache.items():
                after=identity(name)
                if after!=before:raise drift(name,before,after,'verification')
            return result
        finally:_VERIFICATION.reset(token)
    return wrapped


def file_hash(path):
    path=Path(path);name=str(path.absolute());before=identity(path);cache=_VERIFICATION.get()
    if cache is not None and name in cache:
        if cache[name][0]!=before:raise drift(path,cache[name][0],before,'verification')
        return cache[name][1]
    with path.open('rb') as stream:
        checksum=hashlib.file_digest(stream, 'sha256').hexdigest()
    after=identity(path)
    if after!=before:raise drift(path,before,after,'hashing')
    if cache is not None:cache[name]=(before,checksum)
    return checksum


def digest(value, label='SHA256'):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError(f'{label} is missing or invalid; prepare this source again')
    return value


def source_matches(manifest, expected):
    actual = digest(manifest.get('source_sha256'), 'Prepared video SHA256')
    if actual != digest(expected, 'Registered video SHA256'):
        raise ValueError('Prepared source does not match the registered video SHA256')
    return actual


def safe_asset(folder, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('Evidence path must be relative to its folder')
    original = folder / relative
    path = original.resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError(f'Missing evidence file or path outside source folder: {relative}')
    if original.is_symlink():
        raise ValueError(f'Evidence symbolic links are not supported: {relative}')
    return path


def evidence_hashes(folder, manifest, transcript=False):
    names = [f['file'] for f in manifest['frames']]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate evidence paths')
    if transcript:
        names.append('transcript.json')
    return {name: file_hash(safe_asset(folder, name)) for name in names}


def verify_evidence(folder, manifest, transcript=True, audio=False, frame_names=None):
    inventory = manifest.get('evidence_sha256')
    if not isinstance(inventory, dict):
        raise ValueError('Legacy evidence has no inventory; prepare the fragment again')
    names = [f['file'] for f in manifest.get('frames', [])] if frame_names is None else list(frame_names)
    if transcript:
        names.append('transcript.json')
    if len(names) != len(set(names)):
        raise ValueError('Duplicate evidence paths')
    for name in names:
        expected = digest(inventory.get(name), f'Evidence SHA256 for {name}')
        if file_hash(safe_asset(folder, name)) != expected:
            raise ValueError(f'Evidence integrity changed: {name}; prepare again')
    if audio:
        expected = digest(manifest.get('audio_sha256'), 'Audio SHA256')
        if file_hash(safe_asset(folder, 'audio.wav')) != expected:
            raise ValueError('Prepared audio integrity changed; prepare again')


def check_local_video(video):
    """Pin remains useful if original is unavailable; never bless replaced bytes."""
    expected = digest(video.get('sha256'), 'Registered video SHA256')
    source = video.get('path')
    if not source and not re.match(r'^https://', video.get('source', '')):
        source = video.get('source')
    if source and Path(source).expanduser().is_file():
        if file_hash(Path(source).expanduser()) != expected:
            raise ValueError('Video changed since registration; register it as a new lesson')
        return 'local_video_hash_matches'
    return 'original_unavailable_using_pinned_hash'


def snapshot_inventory(folder):
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Missing code snapshot or symbolic link at snapshot root')
    inventory = {}
    for base, directories, files in os.walk(folder, followlinks=False):
        for name in directories + files:
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError(f'Code symbolic links are not supported: {path.relative_to(folder)}')
            if not path.is_dir() and not path.is_file():
                raise ValueError(f'Unsupported code file type: {path.relative_to(folder)}')
        for name in files:
            path = Path(base) / name
            inventory[path.relative_to(folder).as_posix()] = {
                'sha256': file_hash(path), 'size': path.stat().st_size}
    return dict(sorted(inventory.items()))


def seal_snapshot(folder):
    inventory = snapshot_inventory(folder)
    for base, directories, files in os.walk(folder, topdown=False):
        for name in files + directories:
            path = Path(base) / name
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    folder.chmod(stat.S_IMODE(folder.stat().st_mode) & ~0o222)
    checksum = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
    return inventory, checksum


def verify_snapshot(code):
    if code.get('status') != 'provided':
        return 'not_provided'
    if not isinstance(code.get('inventory'), dict):
        raise ValueError('Legacy code snapshot has no inventory; attach the trusted source again')
    actual = snapshot_inventory(Path(code['snapshot']))
    if actual != code['inventory']:
        raise ValueError('Code snapshot integrity changed; attach the trusted source again')
    return 'snapshot_hashes_match'
