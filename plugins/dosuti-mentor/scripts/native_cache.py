"""Private host cache for locally compiled OCR; never load binaries from a course."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import tempfile
from atomic_files import atomic_json
from profile_storage import checked
from source_integrity import file_hash
from storage_lock import file_lock

SCHEMA='native_ocr_build_v1'


def host_root():
    return Path(tempfile.gettempdir())/('dosuti-mentor-native-'+str(os.getuid()))


def identity_key(identity):
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()


def build_record(entry,path):
    """Verify a bounded build/eviction record without executing its binary."""
    from learning_tools import read_json
    from source_integrity import digest
    regular(path);data=read_json(path)
    if (not isinstance(data,dict) or set(data)!={'schema','identity','binary_sha256'} or data['schema']!=SCHEMA
            or not isinstance(data['identity'],dict) or set(data['identity'])!={'source_sha256','toolchain'}
            or not isinstance(data['identity']['toolchain'],dict) or identity_key(data['identity'])!=entry.name):
        raise ValueError('Invalid OCR build record')
    digest(data['identity']['source_sha256']);digest(data['binary_sha256'])
    return data


def finish_eviction(entry):
    """Caller holds build.lock. A tombstone makes interrupted pruning resumable."""
    marker=entry/'evicted.json';regular(marker)
    if not marker.exists():return
    if (entry/'build.json').exists():raise ValueError('Ambiguous OCR eviction; preserve host cache')
    data=build_record(entry,marker);binary=entry/'vision';regular(binary)
    if binary.exists():
        if not 0<binary.stat().st_size<=50*1024*1024 or file_hash(binary)!=data['binary_sha256']:
            raise ValueError('OCR eviction binary changed; preserve host cache')
        binary.unlink()
    marker.unlink()


def private_directory(path):
    path=Path(path)
    try:path.mkdir(mode=0o700)
    except FileExistsError:pass
    checked(path,True);info=path.stat()
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('OCR executable cache must be owned by this user and private (0700)')
    return path


def regular(path):
    checked(path)
    if path.exists():
        s=path.stat()
        if s.st_uid!=os.getuid() or s.st_mode&0o022:raise ValueError('OCR executable cache contains unsafe ownership/permissions')


def toolchain():
    result=subprocess.run(['/usr/bin/xcrun','--find','swiftc'],capture_output=True,text=True,timeout=10)
    if result.returncode:raise RuntimeError('Local Swift compiler unavailable: '+result.stderr[-1000:])
    compiler=Path(result.stdout.strip())  # Keep swiftc argv[0]; resolving its symlink changes driver mode.
    if not compiler.is_absolute() or not compiler.is_file():raise RuntimeError('Local Swift compiler unavailable')
    info=compiler.stat()
    return compiler,dict(driver='/usr/bin/swiftc',path=str(compiler),resolved_path=str(compiler.resolve()),size=info.st_size,mtime_ns=info.st_mtime_ns,
                         os_version=platform.mac_ver()[0],machine=platform.machine(),sdk_root=os.environ.get('SDKROOT'))


def build(source,binary,modules,compiler):
    result=subprocess.run(['/usr/bin/swiftc','-module-cache-path',str(modules),str(source),'-o',str(binary)],capture_output=True,text=True,timeout=60)
    if result.returncode:raise RuntimeError('Local OCR compiler unavailable: '+result.stderr[-1200:])


@contextmanager
def executable(source,cache_root=None):
    """Hold the per-build lock through execution; interrupted builds are not executable."""
    source=Path(source);source_sha=file_hash(source);compiler,environment=toolchain()
    identity=dict(source_sha256=source_sha,toolchain=environment)
    key=identity_key(identity)
    # Host temp cache is not configured by course metadata. No binary in the course.
    root=private_directory(Path(cache_root) if cache_root is not None else host_root())
    entry=private_directory(root/key);lock=entry/'build.lock';regular(lock)
    with file_lock(lock,timeout=100):
        regular(lock);private_directory(root);private_directory(entry)
        binary=entry/'vision';manifest=entry/'build.json'
        regular(binary);regular(manifest)
        finish_eviction(entry)
        if manifest.exists():
            from learning_tools import read_json
            data=read_json(manifest)
            if (not isinstance(data,dict) or set(data)!={'schema','identity','binary_sha256'} or data['schema']!=SCHEMA or data['identity']!=identity
                    or not binary.is_file() or not 0<binary.stat().st_size<=50*1024*1024
                    or file_hash(binary)!=data['binary_sha256'] or not os.access(binary,os.X_OK)):
                raise ValueError('OCR executable cache changed; preserve/remove this host cache before rebuilding')
        else:
            if binary.exists():raise ValueError('Incomplete OCR executable cache; remove this host cache before rebuilding')
            with tempfile.TemporaryDirectory(prefix='building-',dir=entry) as tmp:
                staging=Path(tmp)/'vision'
                build(source,staging,Path(tmp)/'modules',compiler)
                regular(staging)
                if not staging.is_file() or not 0<staging.stat().st_size<=50*1024*1024:raise ValueError('Invalid compiled OCR executable')
                if file_hash(source)!=source_sha:raise ValueError('OCR source changed during compilation')
                staging.chmod(0o700);checksum=file_hash(staging);staging.replace(binary)
                try:atomic_json(manifest,dict(schema=SCHEMA,identity=identity,binary_sha256=checksum))
                except BaseException:
                    # Never execute an unpublished or half-described build.
                    binary.unlink(missing_ok=True);raise
        if file_hash(source)!=source_sha:raise ValueError('OCR source changed before execution')
        yield binary
