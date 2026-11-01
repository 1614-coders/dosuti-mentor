"""Read-only Darwin proof for one clean restart, never a ctime exception."""
import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path
import stat
import sys

UF_TRACKED = 0x40
MAX_SECURITY_BYTES = 256 * 1024


@lru_cache(maxsize=1)
def bindings():
    lib = ctypes.CDLL(None, use_errno=True)
    for name, args, result in [
        ('flistxattr', [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int], ctypes.c_ssize_t),
        ('fgetxattr', [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int], ctypes.c_ssize_t),
        ('acl_get_fd_np', [ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
        ('acl_to_text', [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)], ctypes.c_void_p),
        ('acl_free', [ctypes.c_void_p], ctypes.c_int)]:
        function = getattr(lib, name); function.argtypes = args; function.restype = result
    return lib


def buffer_size(value):
    if value < 0: raise OSError(ctypes.get_errno(), 'Cannot read source security metadata')
    if value > MAX_SECURITY_BYTES: raise ValueError('Source security metadata exceeds retry limit')
    return value


def attributes(fd):
    lib = bindings(); size = buffer_size(lib.flistxattr(fd, None, 0, 0))
    names = ctypes.create_string_buffer(size)
    count = buffer_size(lib.flistxattr(fd, names, size, 0)); rows = []; total = count
    for name in sorted(n for n in names.raw[:count].split(b'\0') if n):
        size = buffer_size(lib.fgetxattr(fd, name, None, 0, 0, 0)); total += size
        buffer_size(total)
        value = ctypes.create_string_buffer(size)
        count = buffer_size(lib.fgetxattr(fd, name, value, size, 0, 0))
        rows.append((name, value.raw[:count]))
    return tuple(rows)


def acl(fd):
    lib = bindings(); value = lib.acl_get_fd_np(fd, 0x100)
    if not value:
        if ctypes.get_errno() == errno.ENOENT: return None
        raise OSError(ctypes.get_errno(), 'Cannot read source ACL')
    text = None
    try:
        size = ctypes.c_ssize_t(); text = lib.acl_to_text(value, ctypes.byref(size))
        if not text: raise OSError(ctypes.get_errno(), 'Cannot read source ACL text')
        return ctypes.string_at(text, buffer_size(size.value))
    finally:
        if text: lib.acl_free(text)
        lib.acl_free(value)


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def capture(path):
    """Unknown/unstable metadata disables restart; it never bypasses verification."""
    if sys.platform != 'darwin': return None
    path = Path(path).absolute()
    try:
        if any(p.is_symlink() for p in (path, *path.parents)): return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1: return None
            security = (attributes(fd), acl(fd)); after = os.fstat(fd)
            if signature(before) != signature(after) or signature(after) != signature(path.lstat()): return None
            return dict(identity=signature(after), flags=after.st_flags, security=security,
                rights=(after.st_mode, after.st_uid, after.st_gid, after.st_nlink, after.st_birthtime))
        finally: os.close(fd)
    except (OSError, ValueError, AttributeError):
        return None


def require_same(path, proof, checksum):
    from source_integrity import file_hash
    if capture(path) != proof or file_hash(path) != checksum or capture(path) != proof:
        raise ValueError('Source changed between preparation attempts; nothing published')


def retry_proof(path, before, error):
    from source_integrity import SourceDrift
    if not isinstance(error, SourceDrift) or error.path != Path(path).absolute() or before is None:
        return None
    after = capture(path)
    if (after is None or before['identity'] != error.before or after['identity'] != error.after
            or before['identity'][:4] != after['identity'][:4]
            or before['identity'][4] == after['identity'][4]
            or before['rights'] != after['rights'] or before['security'] != after['security']
            or before['flags'] & UF_TRACKED or after['flags'] != before['flags'] | UF_TRACKED
            or not error.checksum):
        return None
    require_same(path, after, error.checksum)
    return after
