"""One explicit QA lifecycle root, checked again under the operation lock."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
from pathlib import Path
from profile_storage import checked
from storage_lock import file_lock

MARKER='.cleanup-pending.json'
_HELD=ContextVar('pilot_lifecycle_held',default=frozenset())


def pending(root):
    root=Path(root)
    candidates=[root/MARKER]
    # Only the documented setup/run layout, never an ancestor search.
    if root.name=='run': candidates.append(root.parent/MARKER)
    for path in candidates:
        checked(path)
        if path.exists():
            if path.stat().st_size>16000: raise ValueError('Oversized cleanup marker')
            value=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value,dict) or not all(isinstance(value.get(k),str) for k in ('seal','owner')):
                raise ValueError('Invalid pending-cleanup marker; preserve it')
            return dict(pending=True,root=str(path.parent),seal=value['seal'],
                        next_action='Resume cleanup-apply with the original external plan. Learning is frozen.')
    return None


def active(root):
    if pending(root): raise ValueError('Pilot cleanup is pending; resume cleanup instead of learning')


@contextmanager
def guard(root, timeout=30):
    root=Path(root);active(root)
    if not root.is_dir(): raise ValueError('Pilot directory missing')
    key=str(root.absolute())
    if key in _HELD.get():
        active(root);yield;return
    lock=root/'.pilot-use.lock';checked(lock)
    with file_lock(lock,timeout=timeout,create_parent=False):
        active(root)
        if not (root/'pilot.json').is_file(): raise ValueError('Pilot journal missing')
        token=_HELD.set(_HELD.get()|{key})
        try: yield
        finally: _HELD.reset(token)


def protected(function=None, *, timeout=30):
    if function is None: return lambda fn: protected(fn,timeout=timeout)
    @wraps(function)
    def wrapped(path,*args,**kwargs):
        from pilot import checked_root
        root=checked_root(path)
        with guard(root,timeout=timeout): return function(root,*args,**kwargs)
    return wrapped
