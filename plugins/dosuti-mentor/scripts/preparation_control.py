"""Cooperative attempt cancellation; terminate only subprocesses launched here."""
from contextlib import ExitStack
from contextvars import ContextVar
import locale
import os
from pathlib import Path
import signal
import subprocess
import time
import tempfile

# Captured metadata/logs must fit this budget; never silently truncate evidence.
MAX_CAPTURE_BYTES = 16 * 1024 * 1024

REQUEST = ContextVar('preparation_cancel_request', default=None)


class PreparationCancelled(KeyboardInterrupt):
    def __str__(self):
        return 'Подготовка отменена пользователем; завершённые этапы сохранены. Незавершённое распознавание начнётся заново.'


def check_cancel():
    path = REQUEST.get()
    if path is not None and path.exists():
        raise PreparationCancelled()


def stop(child, *, process_group=True):
    if not process_group and os.name != 'nt':
        if child.poll() is None:
            child.kill()
        child.wait(timeout=15)
        return
    if os.name == 'nt':
        if child.poll() is not None: return
        # The live direct child owns this tree; never kill by executable name.
        tool = Path(os.environ['SystemRoot']) / 'System32/taskkill.exe'
        result = subprocess.run([str(tool), '/PID', str(child.pid), '/T', '/F'],
                                capture_output=True, timeout=15)
        if result.returncode and child.poll() is None:
            raise RuntimeError('Не удалось завершить дочерние процессы; проверь Диспетчер задач')
    else:
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    child.wait(timeout=15)


def run(command, *, check=False, timeout=None, capture_output=False, input=None, process_group=True, **kwargs):
    """Own the process tree, poll cancellation, and bound captured output in memory.

    Leaf FFmpeg calls use process_group=False to remain in their worker group.
    Temporary capture files close on every exit. Size checks also run after exit,
    so a fast noisy process cannot bypass the limit. No output is truncated.
    """
    check_cancel()
    if capture_output:
        if 'stdout' in kwargs or 'stderr' in kwargs:
            raise ValueError('capture_output conflicts with stdout/stderr')
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        if kwargs.get('stdin') is not None:
            raise ValueError('input conflicts with stdin')
        kwargs['stdin'] = subprocess.PIPE
    if os.name == 'nt':
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    elif process_group:
        kwargs['start_new_session'] = True
    text = kwargs.get('text') or kwargs.get('universal_newlines') or kwargs.get('encoding') or kwargs.get('errors')
    with ExitStack() as stack:
        captures = {}
        for name in ('stdout', 'stderr'):
            if kwargs.get(name) == subprocess.PIPE:
                captures[name] = stack.enter_context(tempfile.TemporaryFile())
                kwargs[name] = captures[name]

        def check_output():
            if any(os.fstat(stream.fileno()).st_size > MAX_CAPTURE_BYTES for stream in captures.values()):
                raise RuntimeError('External program output exceeded capture limit; no partial result accepted')

        started = time.monotonic()
        child = subprocess.Popen(command, **kwargs)
        try:
            try:
                while True:
                    check_cancel()
                    check_output()
                    remaining = None if timeout is None else timeout - (time.monotonic() - started)
                    if remaining is not None and remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    try:
                        child.communicate(input=input, timeout=min(.05, remaining) if remaining is not None else .05)
                        break
                    except subprocess.TimeoutExpired:
                        input = None
                check_output()
                if process_group and os.name != 'nt':
                    # Helpers are synchronous: a surviving owned group must not
                    # keep writing captured output after we accept the result.
                    try:
                        os.killpg(child.pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise RuntimeError('External program left running subprocesses; result not accepted')
            except BaseException:
                stop(child, process_group=process_group)
                raise
            output = {}
            for name, stream in captures.items():
                stream.seek(0)
                size=os.fstat(stream.fileno()).st_size
                if size>MAX_CAPTURE_BYTES:raise RuntimeError('External program output exceeded capture limit')
                raw = stream.read(size + 1)
                if len(raw)!=size or os.fstat(stream.fileno()).st_size!=size:
                    raise RuntimeError('External program output changed during collection; no partial result accepted')
                if len(raw) > MAX_CAPTURE_BYTES:
                    raise RuntimeError('External program output exceeded capture limit')
                output[name] = (raw.decode(kwargs.get('encoding') or locale.getpreferredencoding(False),
                                           kwargs.get('errors') or 'strict').replace('\r\n', '\n').replace('\r', '\n')
                                if text else raw)
            result = subprocess.CompletedProcess(command, child.returncode, output.get('stdout'), output.get('stderr'))
            if check:
                result.check_returncode()
            return result
        finally:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
