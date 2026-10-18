"""Cooperative attempt cancellation; terminate only subprocesses launched here."""
from contextvars import ContextVar
import os
from pathlib import Path
import signal
import subprocess
import time

REQUEST = ContextVar('preparation_cancel_request', default=None)


class PreparationCancelled(KeyboardInterrupt):
    def __str__(self):
        return 'Подготовка отменена пользователем; завершённые этапы сохранены. Незавершённое распознавание начнётся заново.'


def check_cancel():
    path = REQUEST.get()
    if path is not None and path.exists():
        raise PreparationCancelled()


def stop(child):
    if child.poll() is not None: return
    if os.name == 'nt':
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


def run(command, *, check=False, timeout=None, capture_output=False, input=None, **kwargs):
    """subprocess.run semantics, with bounded polling only inside tracked attempts."""
    if REQUEST.get() is None:
        return subprocess.run(command, check=check, timeout=timeout,
                              capture_output=capture_output, input=input, **kwargs)
    check_cancel()
    if capture_output:
        if 'stdout' in kwargs or 'stderr' in kwargs: raise ValueError('capture_output conflicts with stdout/stderr')
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None: kwargs['stdin'] = subprocess.PIPE
    if os.name == 'nt': kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else: kwargs['start_new_session'] = True
    started = time.monotonic()
    child = subprocess.Popen(command, **kwargs)
    try:
        try:
            while True:
                check_cancel()
                remaining = None if timeout is None else timeout - (time.monotonic() - started)
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = child.communicate(input=input, timeout=min(.5, remaining) if remaining is not None else .5)
                    break
                except subprocess.TimeoutExpired:
                    input = None
        except BaseException:
            stop(child)
            raise
        result = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
        if check: result.check_returncode()
        return result
    finally:
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None: stream.close()
