"""Bounded OS file locks; the lock file must never be replaced or removed."""
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import os
from pathlib import Path
import time


_HELD_STREAMS = ContextVar('storage_lock_held_streams', default={})


def locked_stream(path):
    """Return this process's already locked stream for path, if any."""
    return _HELD_STREAMS.get().get(str(Path(path).absolute()))


@contextmanager
def file_lock(path, timeout=30, create_parent=True):
    path = Path(path)
    if create_parent: path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
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
