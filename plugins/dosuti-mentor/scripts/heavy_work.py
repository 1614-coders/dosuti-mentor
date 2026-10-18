"""One heavy worker per managed home; no model imports while waiting."""
from contextlib import contextmanager
from contextvars import ContextVar
import sys
import time
from managed_storage import safe, validate
from storage_lock import file_lock

_HELD=ContextVar('dosuti_heavy_home',default=None)


@contextmanager
def slot(home,timeout=21600):
    if home is None or _HELD.get()==home:
        yield;return
    home=validate(home)
    path=safe(home/'.heavy.lock')
    started=time.monotonic();guard=None;notice=0
    while guard is None:
        candidate=file_lock(path,timeout=0)
        try: candidate.__enter__();guard=candidate
        except TimeoutError:
            if time.monotonic()-started>=timeout:
                raise TimeoutError('Heavy processing is busy; rerun to continue the saved job')
            if time.monotonic()-started>=notice:
                notice=time.monotonic()-started+30
                print('Waiting for another DoSuti processing job; model not loaded.',file=sys.stderr,flush=True)
            time.sleep(.25)
    token=_HELD.set(home)
    try: yield
    finally:
        _HELD.reset(token)
        guard.__exit__(None,None,None)
