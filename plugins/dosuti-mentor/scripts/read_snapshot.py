"""Request-local reads with optimistic validation; no storage or writer locks."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

_CURRENT = ContextVar('study_next_read_snapshot', default=None)
_OPERATION = ContextVar('learning_operation_reads', default=None)


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
    reuse = _OPERATION.get()
    if reuse is not None:
        key = Path(path).absolute()
        value = stamp(key)
        if key in reuse['files'] and reuse['files'][key] != value:
            reuse['cache'].clear()
            reuse['generation'] += 1
        reuse['files'][key] = value
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


@contextmanager
def operation():
    """Bound reuse to one operation; writes invalidate rather than retry mutations."""
    if _OPERATION.get() is not None:
        yield
        return
    token = _OPERATION.set(dict(files={}, cache={}, generation=0))
    try:
        yield
    finally:
        _OPERATION.reset(token)


def reuse_reads(function):
    @wraps(function)
    def run(*args, **kwargs):
        with operation():
            return function(*args, **kwargs)
    return run


def cached(function=None, *, limit=None):
    """Reuse a pure validated journal reader only inside the current request."""
    if function is None:
        return lambda reader: cached(reader, limit=limit)
    @wraps(function)
    def read(*args, **kwargs):
        state = _CURRENT.get()
        if state is None:
            state = _OPERATION.get()
            if state is not None:
                for path, before in list(state['files'].items()):
                    if stamp(path) != before:
                        state['cache'].clear()
                        state['generation'] += 1
                        state['files'].clear()
                        break
        if state is None:
            return function(*args, **kwargs)
        key = (function, args, tuple(sorted(kwargs.items())))
        if key not in state['cache']:
            if limit is not None:
                keys = [entry for entry in state['cache'] if entry[0] is function]
                for entry in keys[:max(0, len(keys) - limit + 1)]:
                    del state['cache'][entry]
            generation = state.get('generation', 0)
            result = function(*args, **kwargs)
            if state.get('generation', 0) == generation:
                state['cache'][key] = result
            return result
        return state['cache'][key]
    return read
