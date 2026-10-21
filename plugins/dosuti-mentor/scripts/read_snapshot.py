"""Request-local reads with optimistic validation; no storage or writer locks."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

_CURRENT = ContextVar('study_next_read_snapshot', default=None)


class SnapshotChanged(Exception):
    """Not ValueError/OSError: partial-data readers must not swallow a conflict."""


def stamp(path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def watch(path):
    current = _CURRENT.get()
    if current is None:
        return
    path = Path(path).absolute()
    value = stamp(path)
    if path in current['files'] and current['files'][path] != value:
        raise SnapshotChanged('Learning data changed during study-next')
    current['files'][path] = value


@contextmanager
def snapshot():
    state = dict(files={}, cache={})
    token = _CURRENT.set(state)
    try:
        try:
            yield
        finally:
            for path, before in state['files'].items():
                if stamp(path) != before:
                    raise SnapshotChanged('Learning data changed during study-next')
    finally:
        _CURRENT.reset(token)


def observed(function):
    """Observe a file reader, including an absent file's default value."""
    @wraps(function)
    def read(path, *args, **kwargs):
        watch(path)
        result = function(path, *args, **kwargs)
        watch(path)
        return result
    return read


def cached(function):
    """Reuse a pure validated journal reader only inside the current request."""
    @wraps(function)
    def read(*args, **kwargs):
        state = _CURRENT.get()
        if state is None:
            return function(*args, **kwargs)
        key = (function, args, tuple(sorted(kwargs.items())))
        if key not in state['cache']:
            state['cache'][key] = function(*args, **kwargs)
        return state['cache'][key]
    return read
