"""Bounded OS file locks; the lock file must never be replaced or removed."""
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import os
import stat
from pathlib import Path
import time


_HELD_STREAMS = ContextVar('storage_lock_held_streams', default={})


def locked_stream(path):
    """Return this process's already locked stream for path, if any."""
    return _HELD_STREAMS.get().get(str(Path(path).absolute()))


@contextmanager
def file_lock(path, timeout=30, create_parent=True, *, directory_fd=None):
    path = Path(path)
    if directory_fd is None:
        if create_parent: path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open('a+b')
    else:
        try:
            fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
        except FileExistsError:
            fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
        stream = os.fdopen(fd, 'r+b')
    with stream:
        if directory_fd is not None:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Lock must be an ordinary, unlinked file')
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()

            def acquire():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'Course storage is busy: {path}') from error
                time.sleep(0.05)
        token = _HELD_STREAMS.set(_HELD_STREAMS.get() | {str(path.absolute()): stream})
        try:
            yield stream
        finally:
            _HELD_STREAMS.reset(token)
            release()
